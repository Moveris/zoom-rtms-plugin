"""Unit tests for the Zoom webhook handler."""

import asyncio
import hashlib
import hmac
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from src.main import app
from src.webhook_handler import handle_url_validation, process_webhook, validate_zoom_signature

# Matches the ZOOM_WEBHOOK_SECRET_TOKEN set by conftest.override_settings
SECRET = "test_webhook_secret"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_zoom_signature(secret: str, timestamp: str, body: str) -> str:
    """Build a valid ``v0=<hex>`` Zoom webhook signature."""
    message = f"v0:{timestamp}:{body}"
    hash_hex = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"v0={hash_hex}"


# ---------------------------------------------------------------------------
# Pure-function tests for validate_zoom_signature
# ---------------------------------------------------------------------------


def test_validate_signature_valid():
    body = b'{"event":"test"}'
    ts = "1700000000"
    sig = make_zoom_signature(SECRET, ts, body.decode())
    assert validate_zoom_signature(body, ts, sig, SECRET) is True


def test_validate_signature_invalid_hash():
    body = b'{"event":"test"}'
    assert validate_zoom_signature(body, "1700000000", "v0=deadbeef", SECRET) is False


def test_validate_signature_empty_inputs():
    # Empty timestamp/signature should never match a real signature
    assert validate_zoom_signature(b"{}", "", "", SECRET) is False


def test_validate_signature_tampered_body():
    body = b'{"event":"test"}'
    ts = "1700000000"
    sig = make_zoom_signature(SECRET, ts, body.decode())
    tampered = b'{"event":"tampered"}'
    assert validate_zoom_signature(tampered, ts, sig, SECRET) is False


# ---------------------------------------------------------------------------
# Pure-function test for handle_url_validation
# ---------------------------------------------------------------------------


def test_handle_url_validation_correct_tokens():
    """Response must echo plainToken and include its HMAC."""
    response = handle_url_validation({"plainToken": "mytoken123"})
    data = json.loads(response.body)
    assert data["plainToken"] == "mytoken123"
    expected = hmac.new(SECRET.encode(), "mytoken123".encode(), hashlib.sha256).hexdigest()
    assert data["encryptedToken"] == expected


# ---------------------------------------------------------------------------
# Endpoint integration tests via httpx AsyncClient
# ---------------------------------------------------------------------------


async def test_endpoint_url_validation_no_signature_needed():
    """URL validation must succeed without a Zoom signature header."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/zoom/webhook",
            json={
                "event": "endpoint.url_validation",
                "payload": {"plainToken": "abc123"},
            },
        )
    assert response.status_code == 200
    data = response.json()
    assert data["plainToken"] == "abc123"
    expected = hmac.new(SECRET.encode(), "abc123".encode(), hashlib.sha256).hexdigest()
    assert data["encryptedToken"] == expected


async def test_endpoint_invalid_signature_returns_401():
    body_str = json.dumps(
        {
            "event": "meeting.rtms_started",
            "payload": {
                "object": {
                    "meeting_uuid": "mtg-1",
                    "rtms_stream_id": "s-1",
                    "server_urls": [],
                }
            },
        }
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/zoom/webhook",
            content=body_str,
            headers={
                "Content-Type": "application/json",
                "X-Zoom-Request-Timestamp": "1700000000",
                "X-Zoom-Signature": "v0=badhash",
            },
        )
    assert response.status_code == 401


async def test_endpoint_rtms_started_valid_signature_returns_200():
    obj = {
        "meeting_uuid": "mtg-123",
        "rtms_stream_id": "stream-abc",
        "server_urls": ["wss://rtms.zoom.example.com"],
    }
    body_str = json.dumps({"event": "meeting.rtms_started", "payload": {"object": obj}})
    ts = "1700000000"
    sig = make_zoom_signature(SECRET, ts, body_str)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/zoom/webhook",
            content=body_str,
            headers={
                "Content-Type": "application/json",
                "X-Zoom-Request-Timestamp": ts,
                "X-Zoom-Signature": sig,
            },
        )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_endpoint_rtms_stopped_valid_signature_returns_200():
    obj = {"meeting_uuid": "mtg-123"}
    body_str = json.dumps({"event": "meeting.rtms_stopped", "payload": {"object": obj}})
    ts = "1700000000"
    sig = make_zoom_signature(SECRET, ts, body_str)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/zoom/webhook",
            content=body_str,
            headers={
                "Content-Type": "application/json",
                "X-Zoom-Request-Timestamp": ts,
                "X-Zoom-Signature": sig,
            },
        )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_endpoint_unknown_event_with_valid_signature_returns_200():
    body_str = json.dumps({"event": "meeting.ended", "payload": {"object": {}}})
    ts = "1700000000"
    sig = make_zoom_signature(SECRET, ts, body_str)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/zoom/webhook",
            content=body_str,
            headers={
                "Content-Type": "application/json",
                "X-Zoom-Request-Timestamp": ts,
                "X-Zoom-Signature": sig,
            },
        )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_endpoint_invalid_json_returns_400():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post(
            "/zoom/webhook",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Task dispatch test — verifies the callback is actually called
# ---------------------------------------------------------------------------


async def test_rtms_started_dispatches_callback():
    """The RTMS start callback must be invoked via asyncio.create_task."""
    calls: list[str] = []

    async def mock_rtms_start(
        meeting_uuid: str, rtms_stream_id: str, server_urls: list[str]
    ) -> None:
        calls.append(meeting_uuid)

    # Build a minimal test app that injects our mock callback
    test_app = FastAPI()

    @test_app.post("/zoom/webhook")
    async def webhook(request: Request) -> JSONResponse:
        return await process_webhook(request, mock_rtms_start)

    obj = {
        "meeting_uuid": "mtg-dispatch",
        "rtms_stream_id": "s-dispatch",
        "server_urls": ["wss://rtms.zoom.example.com"],
    }
    body_str = json.dumps({"event": "meeting.rtms_started", "payload": {"object": obj}})
    ts = "1700000000"
    sig = make_zoom_signature(SECRET, ts, body_str)

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
        response = await c.post(
            "/zoom/webhook",
            content=body_str,
            headers={
                "Content-Type": "application/json",
                "X-Zoom-Request-Timestamp": ts,
                "X-Zoom-Signature": sig,
            },
        )

    assert response.status_code == 200
    # Yield control so the background task can run
    await asyncio.sleep(0)
    assert "mtg-dispatch" in calls
