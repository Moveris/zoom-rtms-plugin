"""Unit tests for RTMSClient.

The Zoom RTMS SDK is a compiled C extension that requires Zoom credentials
and live network access.  All tests mock the ``rtms`` module at import time
so they run in any environment without the SDK installed.
"""

import asyncio
import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from src.config import Settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(
        zoom_client_id="test_id",
        zoom_client_secret="test_secret",
        zoom_webhook_secret_token="test_token",
        moveris_api_key="test_key",
    )


def _make_png_bytes(
    width: int = 16, height: int = 16, color: tuple = (0, 255, 0)
) -> bytes:
    """Create a minimal valid PNG as bytes using OpenCV."""
    frame = np.full((height, width, 3), color, dtype=np.uint8)
    ok, buf = cv2.imencode(".png", frame)
    assert ok
    return buf.tobytes()


def _make_mock_rtms_module() -> types.ModuleType:
    """Build a fake ``rtms`` module that satisfies RTMSClient's imports."""
    mod = types.ModuleType("rtms")

    mock_client = MagicMock()
    # Decorator-style registration: @client.onVideoData returns a no-op wrapper
    for event in ("onVideoData", "onJoinConfirm", "onLeave"):
        getattr(mock_client, event).side_effect = lambda fn: fn

    mod.Client = MagicMock(return_value=mock_client)
    mod.VideoParams = MagicMock
    mod.VideoCodec = {"PNG": 6, "H264": 7}
    mod.VideoResolution = {"HD": 2}
    mod.VideoDataOption = {"VIDEO_SINGLE_ACTIVE_STREAM": 3}

    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_start_creates_poll_thread(settings: Settings) -> None:
    """start() must launch the background polling thread."""
    mock_rtms = _make_mock_rtms_module()
    frames: list = []

    async def on_frame(frame: Any, uid: int, uname: str, ts: int) -> None:
        frames.append(frame)

    with patch.dict(sys.modules, {"rtms": mock_rtms}):
        from src.rtms.client import RTMSClient

        client = RTMSClient(
            settings, "uuid-1", "stream-1", "wss://example.com", on_frame
        )
        await client.start()

        assert client._poll_thread is not None
        assert client._poll_thread.is_alive()

        await client.close()


async def test_close_stops_poll_thread(settings: Settings) -> None:
    """close() must stop the polling thread and clear the SDK reference."""
    mock_rtms = _make_mock_rtms_module()

    async def on_frame(frame: Any, uid: int, uname: str, ts: int) -> None:
        pass

    with patch.dict(sys.modules, {"rtms": mock_rtms}):
        from src.rtms.client import RTMSClient

        client = RTMSClient(
            settings, "uuid-2", "stream-2", "wss://example.com", on_frame
        )
        await client.start()
        thread = client._poll_thread
        await client.close()

        assert not thread.is_alive()
        assert client._sdk is None


async def test_bridge_frame_decodes_png(settings: Settings) -> None:
    """_bridge_frame must decode PNG bytes and invoke the async callback."""
    mock_rtms = _make_mock_rtms_module()
    received: list = []

    async def on_frame(frame: np.ndarray, uid: int, uname: str, ts: int) -> None:
        received.append((frame, uid, uname, ts))

    with patch.dict(sys.modules, {"rtms": mock_rtms}):
        from src.rtms.client import RTMSClient

        client = RTMSClient(
            settings, "uuid-3", "stream-3", "wss://example.com", on_frame
        )
        loop = asyncio.get_event_loop()
        client._loop = loop

        metadata = MagicMock()
        metadata.userId = 42
        metadata.userName = "Alice"

        png = _make_png_bytes(color=(0, 128, 255))
        client._bridge_frame(png, 9000, metadata, loop)

        # Let the scheduled coroutine run
        await asyncio.sleep(0.05)

        assert len(received) == 1
        frame, uid, uname, ts = received[0]
        assert isinstance(frame, np.ndarray)
        assert frame.dtype == np.uint8
        assert frame.ndim == 3
        assert uid == 42
        assert uname == "Alice"
        assert ts == 9000


async def test_bridge_frame_invalid_bytes_does_not_raise(settings: Settings) -> None:
    """_bridge_frame must silently drop corrupt data, not raise."""
    mock_rtms = _make_mock_rtms_module()

    async def on_frame(frame: Any, uid: int, uname: str, ts: int) -> None:
        pass

    with patch.dict(sys.modules, {"rtms": mock_rtms}):
        from src.rtms.client import RTMSClient

        client = RTMSClient(
            settings, "uuid-4", "stream-4", "wss://example.com", on_frame
        )
        loop = asyncio.get_event_loop()
        client._loop = loop

        # Should not raise
        client._bridge_frame(b"not a png", 0, MagicMock(), loop)
        await asyncio.sleep(0.05)


async def test_server_urls_list_uses_first_entry(settings: Settings) -> None:
    """RTMSClient must use the first URL when server_urls is a list."""
    mock_rtms = _make_mock_rtms_module()

    async def on_frame(frame: Any, uid: int, uname: str, ts: int) -> None:
        pass

    with patch.dict(sys.modules, {"rtms": mock_rtms}):
        from src.rtms.client import RTMSClient

        client = RTMSClient(
            settings,
            "uuid-5",
            "stream-5",
            ["wss://primary.example.com", "wss://fallback.example.com"],
            on_frame,
        )
        assert client._server_urls == "wss://primary.example.com"


async def test_server_urls_string_used_directly(settings: Settings) -> None:
    """RTMSClient must use a string server_urls unchanged."""
    mock_rtms = _make_mock_rtms_module()

    async def on_frame(frame: Any, uid: int, uname: str, ts: int) -> None:
        pass

    with patch.dict(sys.modules, {"rtms": mock_rtms}):
        from src.rtms.client import RTMSClient

        client = RTMSClient(
            settings, "uuid-6", "stream-6", "wss://direct.example.com", on_frame
        )
        assert client._server_urls == "wss://direct.example.com"


async def test_close_before_start_does_not_raise(settings: Settings) -> None:
    """close() before start() must be a safe no-op."""
    mock_rtms = _make_mock_rtms_module()

    async def on_frame(frame: Any, uid: int, uname: str, ts: int) -> None:
        pass

    with patch.dict(sys.modules, {"rtms": mock_rtms}):
        from src.rtms.client import RTMSClient

        client = RTMSClient(
            settings, "uuid-7", "stream-7", "wss://example.com", on_frame
        )
        await client.close()  # must not raise
