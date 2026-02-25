"""Integration tests for src/orchestrator.py."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from src.config import get_settings
from src.orchestrator import SessionOrchestrator, TooManySessions
from src.results import InMemoryResultStore


def _checkerboard(h: int = 100, w: int = 100) -> np.ndarray:
    """Sharp frame — passes is_quality_frame threshold."""
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    rows = np.arange(h)
    cols = np.arange(w)
    mask = (rows[:, None] + cols[None, :]) % 2 == 0
    frame[mask] = 255
    return frame


_FAKE_B64 = "aVZCT1J3MEtHZ289"  # arbitrary base64 string standing in for a PNG


@pytest.fixture()
def result_store():
    return InMemoryResultStore()


@pytest.fixture()
def settings():
    return get_settings()


@pytest.fixture()
def orchestrator(settings, result_store):
    return SessionOrchestrator(settings, result_store)


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


async def test_start_session_creates_processing_session(orchestrator, result_store):
    with patch("src.orchestrator.RTMSClient") as MockRTMS:
        MockRTMS.return_value = AsyncMock()
        await orchestrator.start_session("mtg-1", "stream-1", "wss://example.com")

    session = await result_store.get_session("mtg-1")
    assert session is not None
    assert session.state == "processing"
    assert orchestrator.active_session_count == 1

    await orchestrator.close()


async def test_stop_session_marks_complete(orchestrator, result_store):
    with patch("src.orchestrator.RTMSClient") as MockRTMS:
        MockRTMS.return_value = AsyncMock()
        await orchestrator.start_session("mtg-2", "stream-2", "wss://example.com")
        await orchestrator.stop_session("mtg-2")

    session = await result_store.get_session("mtg-2")
    assert session.state == "complete"
    assert orchestrator.active_session_count == 0


async def test_stop_unknown_session_is_noop(orchestrator):
    # Should not raise
    await orchestrator.stop_session("nonexistent")


async def test_duplicate_start_is_ignored(orchestrator, result_store):
    with patch("src.orchestrator.RTMSClient") as MockRTMS:
        MockRTMS.return_value = AsyncMock()
        await orchestrator.start_session("mtg-3", "stream-3", "wss://example.com")
        # Second call with same UUID is silently ignored
        await orchestrator.start_session("mtg-3", "stream-3", "wss://example.com")

    assert orchestrator.active_session_count == 1
    await orchestrator.close()


async def test_too_many_sessions_raises(settings, result_store):
    from src.config import Settings

    limited = Settings(
        zoom_client_id="id",
        zoom_client_secret="secret",
        zoom_webhook_secret_token="tok",
        moveris_api_key="sk",
        max_concurrent_sessions=1,
    )
    orch = SessionOrchestrator(limited, result_store)

    with patch("src.orchestrator.RTMSClient") as MockRTMS:
        MockRTMS.return_value = AsyncMock()
        await orch.start_session("m1", "s1", "wss://example.com")
        with pytest.raises(TooManySessions):
            await orch.start_session("m2", "s2", "wss://example.com")

    await orch.close()


async def test_close_shuts_down_all_sessions(orchestrator, result_store):
    with patch("src.orchestrator.RTMSClient") as MockRTMS:
        MockRTMS.return_value = AsyncMock()
        await orchestrator.start_session("m1", "s1", "wss://example.com")
        await orchestrator.start_session("m2", "s2", "wss://example.com")

    assert orchestrator.active_session_count == 2
    await orchestrator.close()
    assert orchestrator.active_session_count == 0

    for uuid in ("m1", "m2"):
        session = await result_store.get_session(uuid)
        assert session.state == "complete"


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


async def test_full_pipeline_stores_live_result(orchestrator, result_store):
    """10 quality frames with detected faces → Moveris called → result stored."""
    captured_on_frame = None

    def capture_constructor(**kwargs):
        nonlocal captured_on_frame
        captured_on_frame = kwargs["on_frame"]
        mock = AsyncMock()
        return mock

    mock_response = MagicMock()
    mock_response.verdict = "live"
    mock_response.score = 88.0
    mock_response.real_score = 0.88
    mock_response.fake_score = 0.12
    mock_response.confidence = 0.95

    mock_moveris = AsyncMock()
    mock_moveris.check_crops = AsyncMock(return_value=mock_response)

    with (
        patch("src.orchestrator.RTMSClient", side_effect=capture_constructor),
        patch("src.orchestrator.is_quality_frame", return_value=True),
        patch("src.orchestrator.FaceDetector") as MockDetector,
        patch("src.orchestrator.MoverisClient") as MockMoveris,
    ):
        det_instance = MagicMock()
        det_instance.detect.return_value = _FAKE_B64
        MockDetector.return_value = det_instance

        MockMoveris.return_value.__aenter__ = AsyncMock(return_value=mock_moveris)
        MockMoveris.return_value.__aexit__ = AsyncMock(return_value=False)

        await orchestrator.start_session("mtg-full", "stream-x", "wss://example.com")

        assert captured_on_frame is not None
        for i in range(10):
            await captured_on_frame(_checkerboard(), 1001, "Alice", i * 200)

        # Yield control so the participant task can complete
        for _ in range(50):
            await asyncio.sleep(0)

    session = await result_store.get_session("mtg-full")
    assert "1001" in session.participants
    result = session.participants["1001"]
    assert result is not None
    assert result.verdict == "live"
    assert result.score == 88.0
    assert result.frames_processed == 10

    await orchestrator.close()


async def test_frames_with_user_id_zero_are_ignored(orchestrator, result_store):
    """Frames with user_id=0 (no metadata) must not create participant tasks."""
    captured_on_frame = None

    def capture_constructor(**kwargs):
        nonlocal captured_on_frame
        captured_on_frame = kwargs["on_frame"]
        return AsyncMock()

    with patch("src.orchestrator.RTMSClient", side_effect=capture_constructor):
        await orchestrator.start_session("mtg-zero", "stream-z", "wss://example.com")
        await captured_on_frame(_checkerboard(), 0, "", 0)
        await asyncio.sleep(0)

    session = await result_store.get_session("mtg-zero")
    assert len(session.participants) == 0

    await orchestrator.close()


async def test_insufficient_frames_stores_error(orchestrator, result_store):
    """Fewer than 10 quality crops → error result stored."""
    captured_on_frame = None

    def capture_constructor(**kwargs):
        nonlocal captured_on_frame
        captured_on_frame = kwargs["on_frame"]
        return AsyncMock()

    with (
        patch("src.orchestrator.RTMSClient", side_effect=capture_constructor),
        patch("src.orchestrator.is_quality_frame", return_value=True),
        patch("src.orchestrator.FaceDetector") as MockDetector,
        patch("src.orchestrator.MoverisClient") as MockMoveris,
    ):
        det_instance = MagicMock()
        # Only 5 frames return a detected face
        det_instance.detect.side_effect = [_FAKE_B64] * 5 + [None] * 100
        MockDetector.return_value = det_instance

        mock_moveris_ctx = AsyncMock()
        MockMoveris.return_value.__aenter__ = AsyncMock(return_value=mock_moveris_ctx)
        MockMoveris.return_value.__aexit__ = AsyncMock(return_value=False)

        await orchestrator.start_session("mtg-insuf", "stream-i", "wss://example.com")

        # Send only 5 frames (not enough after face detection gives None)
        for i in range(5):
            await captured_on_frame(_checkerboard(), 2002, "Bob", i * 200)

        # Wait for frame timeout (patch it out) by cancelling the session
        await orchestrator.stop_session("mtg-insuf")
        for _ in range(20):
            await asyncio.sleep(0)

    session = await result_store.get_session("mtg-insuf")
    # Session should be complete after stop
    assert session.state == "complete"
