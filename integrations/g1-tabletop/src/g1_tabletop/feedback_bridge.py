"""Read-only HTTP adapter for atomic, timestamped feedback records from a site sensor."""

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException

from g1_tabletop.backends.real import FeedbackDocument


def create_feedback_app(source_file: Path) -> FastAPI:
    app = FastAPI(title="G1 physical feedback bridge")

    @app.get("/buttons/{button_id}")
    async def read_button(button_id: str):
        try:
            records = json.loads(source_file.read_text(encoding="utf-8"))
            if not isinstance(records, dict) or button_id not in records:
                raise HTTPException(404, "button not present in feedback source")
            document = FeedbackDocument.model_validate(records[button_id])
            if document.button_id != button_id:
                raise ValueError("source record target mismatch")
            # Never refresh the timestamp here: a stopped upstream sensor must become stale.
            return document.model_dump()
        except (OSError, ValueError) as exc:
            raise HTTPException(503, f"feedback source unavailable: {exc}") from exc

    return app
