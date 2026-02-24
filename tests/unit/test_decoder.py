"""Unit tests for H264Decoder.

All tests use a mock Python subprocess instead of a real FFmpeg binary, so
they run without FFmpeg installed locally.  The mock writes pre-crafted PPM
frames to stdout, letting us exercise the parser and queue logic in isolation.
"""

import asyncio
import textwrap

import numpy as np

from src.rtms.decoder import H264Decoder


# ---------------------------------------------------------------------------
# Mock subprocess helpers
# ---------------------------------------------------------------------------


def _mock_cmd(script: str) -> list[str]:
    """Return a command that runs a Python inline script as the subprocess."""
    return ["python3", "-c", script]


def _ppm_script(frames: list[tuple[int, int, int]], width: int = 32, height: int = 32) -> str:
    """
    Build a Python script that writes PPM frames to stdout.

    Each entry in *frames* is an (R, G, B) tuple for a solid-colour frame.
    After outputting all frames the script drains stdin (avoids BrokenPipeError
    in the parent when the writer is still active).
    """
    frame_exprs = repr(frames)
    return textwrap.dedent(f"""\
        import sys
        W, H = {width}, {height}
        for r, g, b in {frame_exprs}:
            pixels = bytes([r, g, b] * (W * H))
            sys.stdout.buffer.write(f"P6\\n{{W}} {{H}}\\n255\\n".encode() + pixels)
            sys.stdout.buffer.flush()
        try:
            sys.stdin.buffer.read()
        except Exception:
            pass
    """)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_start_and_close_clean():
    """start() and close() must complete without errors."""
    decoder = H264Decoder(_cmd=_mock_cmd(_ppm_script([])))
    await decoder.start()
    assert decoder._process is not None
    assert decoder._reader_task is not None
    await decoder.close()
    assert decoder._reader_task.done()


async def test_get_frame_returns_bgr_ndarray():
    """Decoder must return a uint8 BGR ndarray for each frame produced."""
    decoder = H264Decoder(_cmd=_mock_cmd(_ppm_script([(0, 0, 0)])))
    await decoder.start()
    await decoder.feed(b"\x00")  # dummy NAL — ignored by mock

    frame = await decoder.get_frame(timeout=3.0)

    assert frame is not None
    assert isinstance(frame, np.ndarray)
    assert frame.dtype == np.uint8
    assert frame.ndim == 3
    assert frame.shape == (32, 32, 3)  # (H, W, channels)

    await decoder.close()


async def test_rgb_to_bgr_conversion():
    """PPM RGB output must be converted to BGR (channel axis reversed)."""
    # Mock outputs a pure-red frame: R=255, G=0, B=0 in PPM (RGB)
    decoder = H264Decoder(_cmd=_mock_cmd(_ppm_script([(255, 0, 0)])))
    await decoder.start()
    await decoder.feed(b"\x00")

    frame = await decoder.get_frame(timeout=3.0)
    assert frame is not None

    # After RGB→BGR flip:
    #   channel 0 (B) = original B = 0
    #   channel 1 (G) = original G = 0
    #   channel 2 (R) = original R = 255
    assert int(frame[0, 0, 0]) == 0    # B
    assert int(frame[0, 0, 1]) == 0    # G
    assert int(frame[0, 0, 2]) == 255  # R

    await decoder.close()


async def test_all_frames_queued():
    """Every frame the subprocess outputs must be retrievable via get_frame."""
    n_frames = 5
    frames_spec = [(i * 40, 0, 0) for i in range(n_frames)]
    decoder = H264Decoder(_cmd=_mock_cmd(_ppm_script(frames_spec)))
    await decoder.start()
    await decoder.feed(b"\x00")

    retrieved = []
    for _ in range(n_frames):
        f = await decoder.get_frame(timeout=3.0)
        if f is not None:
            retrieved.append(f)

    assert len(retrieved) == n_frames
    await decoder.close()


async def test_get_frame_timeout_returns_none():
    """get_frame returns None when no frames arrive before the timeout."""
    # Empty frame list — mock produces no output
    decoder = H264Decoder(_cmd=_mock_cmd(_ppm_script([])))
    await decoder.start()

    result = await decoder.get_frame(timeout=0.1)
    assert result is None

    await decoder.close()


async def test_queue_full_drops_frames():
    """Frames beyond queue capacity must be silently dropped, not block."""
    # Queue holds only 1 frame; mock produces 3
    decoder = H264Decoder(queue_size=1, _cmd=_mock_cmd(_ppm_script([(0, 0, 0)] * 3)))
    await decoder.start()
    await decoder.feed(b"\x00")

    # Allow the reader to run and fill the queue
    await asyncio.sleep(0.3)

    assert decoder._queue.qsize() <= 1   # at most 1 frame buffered

    await decoder.close()


async def test_feed_after_close_does_not_raise():
    """feed() after close() must silently no-op."""
    decoder = H264Decoder(_cmd=_mock_cmd(_ppm_script([])))
    await decoder.start()
    await decoder.close()

    # Must not raise
    await decoder.feed(b"\x00" * 100)


async def test_frame_dimensions_match_ppm_header():
    """Decoder must produce frames whose shape matches the PPM header dimensions."""
    # Use a non-square resolution to verify H and W are not swapped
    decoder = H264Decoder(_cmd=_mock_cmd(_ppm_script([(128, 64, 32)], width=48, height=16)))
    await decoder.start()
    await decoder.feed(b"\x00")

    frame = await decoder.get_frame(timeout=3.0)
    assert frame is not None
    assert frame.shape == (16, 48, 3)  # (H=16, W=48, channels)

    await decoder.close()
