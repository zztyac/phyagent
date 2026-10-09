"""Forge-compatible local HTTP and evidence API. All motion uses one Runtime arbiter."""

import asyncio
import base64
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field, StrictInt

from g1_tabletop.config import StrictModel
from g1_tabletop.errors import ExecutionError
from g1_tabletop.runtime import Runtime
from g1_tabletop.specs import ACTIONS


class Invoke(StrictModel):
    arguments: dict = Field(default_factory=dict)
    caller_id: str = Field(min_length=1, max_length=256)
    timeout_ms: StrictInt | None = Field(default=None, gt=0)


def envelope(data):
    return {"ok": True, "data": data}


def create_app(runtime: Runtime, *, manage_lifespan: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        if manage_lifespan:
            await runtime.start()
        try:
            yield
        finally:
            if manage_lifespan:
                await runtime.close()

    app = FastAPI(title="G1 Tabletop Forge Gateway", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(ExecutionError)
    async def runtime_error(request: Request, exc: ExecutionError):
        return JSONResponse({"ok": False, "error": exc.payload()}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            {
                "ok": False,
                "error": {"code": "INVALID_ARGUMENTS", "message": str(exc), "retryable": False},
            },
            status_code=422,
        )

    @app.get("/health")
    async def health():
        contexts = [runtime.context(name) for name in runtime.specs]
        return envelope({"ready": all(c["ready"] for c in contexts), "mode": runtime.config.mode})

    @app.get("/tools")
    async def tools():
        return envelope({"tools": list(runtime.specs.values())})

    @app.get("/tools/{tool_id}/context")
    async def context(tool_id: str):
        return envelope(runtime.context(tool_id))

    @app.get("/tools/{tool_id}")
    async def spec(tool_id: str):
        return envelope(runtime._spec(tool_id))

    @app.post("/tools/{endpoint_id}/{operation}:invoke")
    async def query(endpoint_id: str, operation: str, payload: Invoke):
        tool_id = f"{endpoint_id}.{operation}"
        duration = min((payload.timeout_ms or 10000) / 1000, 10)
        try:
            async with asyncio.timeout(duration):
                result = await runtime.query(tool_id, payload.arguments)
        except TimeoutError as exc:
            raise ExecutionError(
                "QUERY_TIMEOUT", "read-only observation timed out", status=504
            ) from exc
        return envelope(result)

    @app.post("/tools/{tool_id}:invoke", status_code=202)
    async def action(tool_id: str, payload: Invoke):
        if tool_id not in ACTIONS:
            runtime._spec(tool_id)
            raise ExecutionError("WRONG_SEMANTICS", "use the Query route", status=400)
        result = await runtime.submit(
            tool_id, payload.arguments, payload.caller_id, payload.timeout_ms
        )
        return envelope(result)

    @app.get("/invocations/{identity}")
    async def status(identity: str):
        return envelope(runtime.status(identity))

    @app.get("/invocations/{identity}/result")
    async def result(identity: str):
        value = runtime.result(identity)
        return JSONResponse(
            envelope(value), status_code=202 if value["status"] == "pending" else 200
        )

    @app.post("/invocations/{identity}/cancel")
    async def cancel(identity: str):
        return envelope(await runtime.cancel(identity))

    @app.websocket("/ws/images")
    async def images(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                for source in ("head", "left_wrist", "right_wrist"):
                    data, timestamp = await runtime.backend.image(source)
                    await ws.send_json(
                        {
                            "type": "image",
                            "id": source,
                            "seq": time.time_ns(),
                            "timestamp": timestamp,
                            "content_type": "image/png",
                            "data": base64.b64encode(data).decode(),
                        }
                    )
                await asyncio.sleep(0.2)
        except (WebSocketDisconnect, ExecutionError):
            return

    @app.websocket("/ws/state")
    async def state(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await ws.send_json(
                    {
                        "type": "state",
                        "timestamp": time.time(),
                        "mode": runtime.config.mode,
                        "active_invocation": runtime.active_id,
                        "hand_occupied": runtime.store.state("occupied", {}),
                        "calibration_id": runtime.config.calibration_id,
                    }
                )
                await asyncio.sleep(0.1)
        except WebSocketDisconnect:
            return

    return app
