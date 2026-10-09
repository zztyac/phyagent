"""Durable invocation identity and exclusive ownership of one robot runtime."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from g1_tabletop.errors import ExecutionError

TERMINAL = {"succeeded", "failed", "cancelled", "unknown"}


class InvocationStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = path.with_suffix(".lock").open("a+b")
        self.lock.seek(0)
        if os.fstat(self.lock.fileno()).st_size == 0:
            self.lock.write(b"0")
            self.lock.flush()
        self.lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lock.close()
            raise ExecutionError(
                "RUNTIME_OWNED", "another process owns this runtime database"
            ) from exc
        self.db = None
        try:
            self._open(path)
        except BaseException:
            self.close()
            raise

    def _open(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS invocations (
              id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, caller_id TEXT UNIQUE NOT NULL,
              request_hash TEXT NOT NULL, tool_id TEXT NOT NULL, arguments TEXT NOT NULL,
              phase TEXT NOT NULL, stage TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
              result TEXT, created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, invocation_id TEXT NOT NULL,
              timestamp REAL NOT NULL, stage TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        # A crash cannot prove whether a physical command executed. Never replay it.
        with self.db:
            self.db.execute(
                """UPDATE invocations SET phase='unknown', stage='restart_uncertain',
                result=?, updated=? WHERE phase NOT IN ('succeeded','failed','cancelled','unknown')""",
                (
                    json.dumps(
                        {
                            "status": "unknown",
                            "outputs": {},
                            "error": {
                                "code": "RESTART_UNCERTAIN",
                                "message": "physical state requires reconciliation",
                            },
                        }
                    ),
                    time.time(),
                ),
            )

    @staticmethod
    def request_hash(tool_id: str, arguments: dict) -> str:
        value = json.dumps(
            [tool_id, arguments], sort_keys=True, allow_nan=False, separators=(",", ":")
        )
        return hashlib.sha256(value.encode()).hexdigest()

    def find_caller(self, caller_id: str, tool_id: str, arguments: dict) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM invocations WHERE caller_id=?", (caller_id,)
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != self.request_hash(tool_id, arguments):
            raise ExecutionError(
                "IDEMPOTENCY_CONFLICT", "caller_id was already used with different arguments"
            )
        return self.identity(dict(row))

    @staticmethod
    def identity(row: dict) -> dict:
        return {"invocation_id": row["id"], "attempt_id": row["attempt_id"]}

    def create(self, caller_id: str, tool_id: str, arguments: dict) -> dict:
        identity, attempt = "inv_" + uuid4().hex, "attempt_" + uuid4().hex
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO invocations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    attempt,
                    caller_id,
                    self.request_hash(tool_id, arguments),
                    tool_id,
                    json.dumps(arguments, allow_nan=False),
                    "accepted",
                    "accepted",
                    0,
                    None,
                    now,
                    now,
                ),
            )
        return {"invocation_id": identity, "attempt_id": attempt}

    def get(self, identity: str) -> dict:
        row = self.db.execute("SELECT * FROM invocations WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ExecutionError("INVOCATION_NOT_FOUND", identity, status=404)
        return dict(row)

    def stage(self, identity: str, stage: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE invocations SET phase='running',stage=?,updated=? WHERE id=?",
                (stage, time.time(), identity),
            )
            self.db.execute(
                "INSERT INTO events(invocation_id,timestamp,stage) VALUES(?,?,?)",
                (identity, time.time(), stage),
            )

    def finish(self, identity: str, result: dict) -> None:
        with self.db:
            self.db.execute(
                "UPDATE invocations SET phase=?,result=?,updated=? WHERE id=?",
                (result["status"], json.dumps(result, allow_nan=False), time.time(), identity),
            )

    def cancel(self, identity: str) -> None:
        with self.db:
            self.db.execute("UPDATE invocations SET cancel_requested=1 WHERE id=?", (identity,))

    def uncertain(self) -> bool:
        return (
            self.db.execute("SELECT 1 FROM invocations WHERE phase='unknown' LIMIT 1").fetchone()
            is not None
        )

    def state(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def set_state(self, key: str, value) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO state VALUES(?,?)",
                (key, json.dumps(value, allow_nan=False)),
            )

    def reconcile_empty(self, operator_note: str, evidence: dict) -> None:
        """Operator-only settlement after external stop and empty-hand checks, never automatic replay."""
        result = {
            "status": "failed",
            "outputs": {"operator_reconciliation": evidence},
            "error": {"code": "OPERATOR_RECONCILED", "message": operator_note, "retryable": False},
        }
        with self.db:
            self.db.execute(
                "UPDATE invocations SET phase='failed',result=?,updated=? WHERE phase='unknown'",
                (json.dumps(result, allow_nan=False), time.time()),
            )
            self.db.execute(
                "INSERT INTO events(invocation_id,timestamp,stage) VALUES(?,?,?)",
                (
                    "operator",
                    time.time(),
                    json.dumps({"note": operator_note, "evidence": evidence}),
                ),
            )
            self.db.execute("INSERT OR REPLACE INTO state VALUES('occupied','{}')")

    def close(self) -> None:
        if self.lock.closed:
            return
        if self.db is not None:
            self.db.close()
        self.lock.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self.lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.lock.fileno(), fcntl.LOCK_UN)
        self.lock.close()
