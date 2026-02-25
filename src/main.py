import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from src.config import get_settings
from src.orchestrator import SessionOrchestrator, TooManySessions
from src.results import InMemoryResultStore, SessionStatus
from src.webhook_handler import process_webhook

logger = logging.getLogger(__name__)

# Module-level singletons — initialised in lifespan, None before startup.
_orchestrator: SessionOrchestrator | None = None
_result_store: InMemoryResultStore | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _orchestrator, _result_store

    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO)
    )

    _result_store = InMemoryResultStore()
    _orchestrator = SessionOrchestrator(settings, _result_store)
    logger.info(
        "SessionOrchestrator ready (max_sessions=%d)", settings.max_concurrent_sessions
    )

    yield

    if _orchestrator is not None:
        await _orchestrator.close()
    _orchestrator = None
    _result_store = None


app = FastAPI(
    title="Moveris Zoom RTMS Plugin",
    description=(
        "Real-time liveness detection for Zoom meetings. "
        "Receives Zoom RTMS video streams and analyzes participant liveness "
        "using the Moveris API."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


@app.post("/zoom/webhook")
async def zoom_webhook(request: Request) -> JSONResponse:
    async def _start(meeting_uuid: str, rtms_stream_id: str, server_urls: str) -> None:
        if _orchestrator is not None:
            try:
                await _orchestrator.start_session(
                    meeting_uuid, rtms_stream_id, server_urls
                )
            except TooManySessions as exc:
                logger.warning("Rejected RTMS session (at capacity): %s", exc)

    async def _stop(meeting_uuid: str) -> None:
        if _orchestrator is not None:
            await _orchestrator.stop_session(meeting_uuid)

    return await process_webhook(request, _start, _stop)


@app.get("/results/{meeting_uuid}")
async def get_results(meeting_uuid: str) -> SessionStatus:
    store = _result_store
    if store is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    status = await store.get_session(meeting_uuid)
    if status is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return status


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "version": app.version,
        "active_sessions": _orchestrator.active_session_count if _orchestrator else 0,
    }
