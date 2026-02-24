"""Integration tests for RTMSSignalingClient.

Uses a local websockets mock server to exercise the full signaling protocol
without any real Zoom infrastructure.
"""

import asyncio
import hashlib
import hmac
import json

import pytest
import websockets

from src.config import get_settings
from src.rtms.signaling import RTMSSignalingClient, RTMSSignalingError

MEDIA_URL = "wss://media.rtms.example.com/media"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(
    server_urls: list[str],
    meeting_uuid: str = "mtg-test",
    rtms_stream_id: str = "stream-test",
) -> RTMSSignalingClient:
    return RTMSSignalingClient(
        settings=get_settings(),
        meeting_uuid=meeting_uuid,
        rtms_stream_id=rtms_stream_id,
        server_urls=server_urls,
    )


def _handshake_resp(status_code: int = 0, media_url: str = MEDIA_URL) -> str:
    return json.dumps(
        {
            "msg_type": 2,
            "status_code": status_code,
            "media_server": {"server_urls": [media_url]},
        }
    )


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


def test_signature_is_hmac_sha256():
    """Signature must be HMAC-SHA256(client_secret, 'client_id,meeting,stream')."""
    settings = get_settings()
    client = _make_client(
        server_urls=[],
        meeting_uuid="meeting-abc",
        rtms_stream_id="stream-xyz",
    )
    expected = hmac.new(
        settings.zoom_client_secret.encode(),
        f"{settings.zoom_client_id},meeting-abc,stream-xyz".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert client._signature() == expected


# ---------------------------------------------------------------------------
# Integration tests via local mock WS server
# ---------------------------------------------------------------------------


async def test_connect_returns_media_url():
    """Successful handshake → returns the media server URL."""

    async def handler(ws):
        await ws.recv()  # consume handshake request
        await ws.send(_handshake_resp())
        async for _ in ws:  # drain until client closes
            pass

    async with websockets.serve(handler, "localhost", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = _make_client([f"ws://localhost:{port}"])
        media_url = await client.connect()

    assert media_url == MEDIA_URL
    await client.close()


async def test_handshake_request_fields():
    """SIG_HANDSHAKE_REQ must include all required Zoom protocol fields."""
    received: list[dict] = []

    async def handler(ws):
        raw = await ws.recv()
        received.append(json.loads(raw))
        await ws.send(_handshake_resp())
        async for _ in ws:
            pass

    settings = get_settings()
    async with websockets.serve(handler, "localhost", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = RTMSSignalingClient(
            settings=settings,
            meeting_uuid="mtg-123",
            rtms_stream_id="str-456",
            server_urls=[f"ws://localhost:{port}"],
        )
        await client.connect()
        await client.close()

    msg = received[0]
    assert msg["msg_type"] == 1
    assert msg["protocol_version"] == 1
    assert msg["meeting_uuid"] == "mtg-123"
    assert msg["rtms_stream_id"] == "str-456"
    assert isinstance(msg["sequence"], int) and 0 <= msg["sequence"] <= 0xFFFFFFFF

    expected_sig = hmac.new(
        settings.zoom_client_secret.encode(),
        f"{settings.zoom_client_id},mtg-123,str-456".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert msg["signature"] == expected_sig


async def test_auth_failure_raises():
    """Non-zero status_code → RTMSSignalingError."""

    async def handler(ws):
        await ws.recv()
        await ws.send(_handshake_resp(status_code=4001))

    async with websockets.serve(handler, "localhost", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = _make_client([f"ws://localhost:{port}"])
        with pytest.raises(RTMSSignalingError, match="status_code=4001"):
            await client.connect()


async def test_fallback_to_second_url():
    """Client must try the next URL when the first is unavailable."""

    async def handler(ws):
        await ws.recv()
        await ws.send(_handshake_resp())
        async for _ in ws:
            pass

    async with websockets.serve(handler, "localhost", 0) as server:
        port = server.sockets[0].getsockname()[1]
        # Port 1 is reliably unused — connection refused immediately
        client = _make_client(["ws://localhost:1", f"ws://localhost:{port}"])
        media_url = await client.connect()

    assert media_url == MEDIA_URL
    await client.close()


async def test_all_urls_fail_raises():
    """When every URL is unreachable, RTMSSignalingError must propagate."""
    client = _make_client(["ws://localhost:1"])
    with pytest.raises(RTMSSignalingError):
        await client.connect()


async def test_keepalive_echoed():
    """Server KEEP_ALIVE_REQ (12) → client must respond with KEEP_ALIVE_RESP (13)
    containing the same timestamp."""
    keepalive_done = asyncio.Event()
    received_resp: list[dict] = []

    async def handler(ws):
        await ws.recv()  # handshake request
        await ws.send(_handshake_resp())
        # Send a keepalive request
        await ws.send(json.dumps({"msg_type": 12, "timestamp": 99999}))
        # Expect the echo back
        raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
        received_resp.append(json.loads(raw))
        keepalive_done.set()
        async for _ in ws:
            pass

    async with websockets.serve(handler, "localhost", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = _make_client([f"ws://localhost:{port}"])
        await client.connect()
        await asyncio.wait_for(keepalive_done.wait(), timeout=2.0)
        await client.close()

    resp = received_resp[0]
    assert resp["msg_type"] == 13
    assert resp["timestamp"] == 99999


async def test_close_cancels_keepalive():
    """close() must cancel the background keepalive task cleanly."""

    async def handler(ws):
        await ws.recv()
        await ws.send(_handshake_resp())
        try:
            async for _ in ws:
                pass
        except Exception:
            pass

    async with websockets.serve(handler, "localhost", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = _make_client([f"ws://localhost:{port}"])
        await client.connect()

        assert client._keepalive_task is not None
        assert not client._keepalive_task.done()

        await client.close()

        assert client._keepalive_task.done()
        assert client._ws is None


async def test_multiple_keepalives_all_echoed():
    """Multiple consecutive keepalive requests must all be echoed."""
    count = 3
    echoed: list[int] = []
    all_done = asyncio.Event()

    async def handler(ws):
        await ws.recv()
        await ws.send(_handshake_resp())
        for ts in range(count):
            await ws.send(json.dumps({"msg_type": 12, "timestamp": ts}))
        for _ in range(count):
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            echoed.append(json.loads(raw)["timestamp"])
        all_done.set()
        async for _ in ws:
            pass

    async with websockets.serve(handler, "localhost", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = _make_client([f"ws://localhost:{port}"])
        await client.connect()
        await asyncio.wait_for(all_done.wait(), timeout=3.0)
        await client.close()

    assert sorted(echoed) == list(range(count))
