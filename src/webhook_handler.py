"""Zoom webhook validation and event dispatch."""

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from src.config import get_settings

logger = logging.getLogger(__name__)

# Type alias for the RTMS session starter injected by the orchestrator (Phase 9).
RTMSStartCallback = Callable[[str, str, str], Coroutine[Any, Any, None]]


def validate_zoom_signature(
    raw_body: bytes,
    timestamp: str,
    signature: str,
    secret: str,
) -> bool:
    """Validate the Zoom webhook HMAC-SHA256 signature.

    Zoom signs: ``v0:{X-Zoom-Request-Timestamp}:{raw_body_utf8}``
    using ``ZOOM_WEBHOOK_SECRET_TOKEN`` as the key, then prefixes ``v0=``.
    We use ``hmac.compare_digest`` to guard against timing attacks.
    """
    message = f"v0:{timestamp}:{raw_body.decode('utf-8')}"
    expected_hash = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    expected_signature = f"v0={expected_hash}"
    return hmac.compare_digest(expected_signature, signature)


def handle_url_validation(payload: dict) -> JSONResponse:
    """Respond to Zoom's one-time endpoint.url_validation challenge.

    Zoom sends this during webhook registration to verify that we own the URL.
    We must HMAC-SHA256 the ``plainToken`` with our webhook secret and return
    both the plain token and the hash.
    """
    settings = get_settings()
    plain_token: str = payload["plainToken"]
    encrypted_token = hmac.new(
        settings.zoom_webhook_secret_token.encode("utf-8"),
        plain_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return JSONResponse({"plainToken": plain_token, "encryptedToken": encrypted_token})


async def handle_rtms_started(
    obj: dict,
    on_rtms_start: RTMSStartCallback,
) -> JSONResponse:
    """Dispatch a background RTMS session task for ``meeting.rtms_started``.

    Zoom expects a quick 200 response; the actual RTMS work runs in a
    background asyncio task so we don't block the webhook reply.
    """
    meeting_uuid: str = obj["meeting_uuid"]
    rtms_stream_id: str = obj["rtms_stream_id"]
    server_urls: str = obj["server_urls"]

    asyncio.create_task(on_rtms_start(meeting_uuid, rtms_stream_id, server_urls))
    logger.info(
        "Spawned RTMS task — meeting=%s stream=%s", meeting_uuid, rtms_stream_id
    )
    return JSONResponse({"status": "ok"})


async def handle_rtms_stopped(obj: dict) -> JSONResponse:
    """Acknowledge ``meeting.rtms_stopped``; cleanup is handled by the orchestrator."""
    meeting_uuid = obj.get("meeting_uuid", "unknown")
    logger.info("RTMS stopped — meeting=%s", meeting_uuid)
    return JSONResponse({"status": "ok"})


async def process_webhook(
    request: Request,
    on_rtms_start: RTMSStartCallback,
) -> JSONResponse:
    """Main dispatcher: validate Zoom signature then route to the right handler.

    The raw body is read first and kept as bytes so the HMAC is computed over
    exactly what Zoom signed.  JSON parsing happens after that read.
    """
    raw_body = await request.body()

    try:
        body: dict = json.loads(raw_body)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event: str = body.get("event", "")

    # URL validation fires before the app is fully registered, so it does not
    # carry a usable signature — handle it first without signature check.
    if event == "endpoint.url_validation":
        return handle_url_validation(body.get("payload", {}))

    # Every real event must carry a valid Zoom signature.
    settings = get_settings()
    timestamp = request.headers.get("X-Zoom-Request-Timestamp", "")
    signature = request.headers.get("X-Zoom-Signature", "")

    if not validate_zoom_signature(
        raw_body, timestamp, signature, settings.zoom_webhook_secret_token
    ):
        logger.warning("Rejected webhook with invalid signature (event=%s)", event)
        raise HTTPException(status_code=401, detail="Invalid signature")

    obj = body.get("payload", {}).get("object", {})

    if event == "meeting.rtms_started":
        return await handle_rtms_started(obj, on_rtms_start)

    if event == "meeting.rtms_stopped":
        return await handle_rtms_stopped(obj)

    # Forward-compatible: silently acknowledge any event we don't handle yet.
    logger.debug("Unhandled webhook event: %s", event)
    return JSONResponse({"status": "ok"})
