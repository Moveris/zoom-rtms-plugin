from contextlib import asynccontextmanager
import logging
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from src.results import InMemoryResultStore, SessionStatus
from src.webhook_handler import process_webhook

logger = logging.getLogger(__name__)

_result_store = InMemoryResultStore()


async def _stub_rtms_start(
    meeting_uuid: str, rtms_stream_id: str, server_urls: list[str]
) -> None:
    """Placeholder RTMS session starter — replaced by orchestrator in Phase 9."""
    logger.info(
        "RTMS started (stub): meeting=%s stream=%s", meeting_uuid, rtms_stream_id
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Startup — orchestrator wired in Phase 9
    yield
    # Shutdown


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
    return await process_webhook(request, _stub_rtms_start)


@app.get("/results/{meeting_uuid}")
async def get_results(meeting_uuid: str) -> SessionStatus:
    # Orchestrator wired in Phase 9; result store is populated by Phase 8
    status = await _result_store.get_session(meeting_uuid)
    if status is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return status


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "version": app.version,
        "active_sessions": 0,  # Updated in Phase 9 when orchestrator is wired
    }
