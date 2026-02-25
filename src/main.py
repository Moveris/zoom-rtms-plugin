import base64
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from src.config import get_settings
from src.orchestrator import SessionOrchestrator, TooManySessions
from src.results import InMemoryResultStore, SessionStatus
from src.webhook_handler import process_webhook

logger = logging.getLogger(__name__)

# Module-level singletons — initialised in lifespan, None before startup.
_orchestrator: SessionOrchestrator | None = None
_result_store: InMemoryResultStore | None = None

# OAuth token obtained after user installs the app.  Stored in memory for
# local dev; a production deployment should persist this in a database.
_zoom_token: dict | None = None


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


@app.get("/oauth/callback")
async def oauth_callback(request: Request) -> HTMLResponse:
    """Handle the Zoom OAuth redirect and exchange the code for an access token."""
    global _zoom_token

    code = request.query_params.get("code", "")
    if not code:
        return HTMLResponse(
            "<h2>Moveris Zoom RTMS Plugin</h2><p>App installed successfully.</p>",
            status_code=200,
        )

    # The redirect_uri sent during token exchange must exactly match what is
    # registered in the Zoom app.  We reconstruct it from the current request.
    redirect_uri = str(request.url).split("?")[0]
    settings = get_settings()
    creds = base64.b64encode(
        f"{settings.zoom_client_id}:{settings.zoom_client_secret}".encode()
    ).decode()

    async with httpx.AsyncClient() as http:
        resp = await http.post(
            "https://zoom.us/oauth/token",
            params={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Authorization": f"Basic {creds}"},
        )

    if resp.status_code == 200:
        _zoom_token = resp.json()
        logger.info("Zoom OAuth token obtained successfully")
    else:
        logger.warning("Token exchange failed: %d %s", resp.status_code, resp.text)

    return HTMLResponse(
        "<h2>Moveris Zoom RTMS Plugin</h2>"
        "<p>App authorized successfully. You can close this tab.</p>",
        status_code=200,
    )


@app.post("/dev/start-rtms/{meeting_id}")
async def dev_start_rtms(meeting_id: str) -> dict:
    """Dev endpoint: trigger Zoom to start an RTMS stream for a live meeting.

    Call this after starting a Zoom meeting.  Zoom will respond by sending
    ``meeting.rtms_started`` to the webhook endpoint, which kicks off the
    full liveness analysis pipeline.

    ``meeting_id`` is the numeric meeting number (e.g. 2350871635),
    not the UUID.
    """
    if _zoom_token is None:
        raise HTTPException(
            status_code=401,
            detail=(
                "No Zoom OAuth token. "
                "Re-authorize the app by visiting the Zoom Marketplace "
                "app page and clicking 'Add' again."
            ),
        )

    access_token = _zoom_token["access_token"]
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"https://api.zoom.us/v2/meetings/{meeting_id}/rtms/streams",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={},
        )

    body = resp.json() if resp.content else {}
    logger.info("RTMS start API → %d %s", resp.status_code, body)
    return {"status": resp.status_code, "response": body}


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
        "zoom_token": "present" if _zoom_token else "missing",
    }
