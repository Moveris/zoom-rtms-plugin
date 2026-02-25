"""Unit tests for src/video/frame_selector.py."""

import numpy as np
import pytest

from src.video.frame_selector import compute_sharpness, is_quality_frame


def _solid_frame(h: int = 100, w: int = 100) -> np.ndarray:
    """Return a solid gray BGR frame — zero Laplacian variance."""
    return np.full((h, w, 3), 128, dtype=np.uint8)


def _checkerboard_frame(h: int = 100, w: int = 100) -> np.ndarray:
    """Return a high-contrast checkerboard — high Laplacian variance."""
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    rows = np.arange(h)
    cols = np.arange(w)
    mask = (rows[:, None] + cols[None, :]) % 2 == 0
    frame[mask] = 255
    return frame


def test_compute_sharpness_solid_is_zero():
    assert compute_sharpness(_solid_frame()) == 0.0


def test_compute_sharpness_checkerboard_is_high():
    score = compute_sharpness(_checkerboard_frame())
    assert score > 100.0


def test_compute_sharpness_returns_float():
    result = compute_sharpness(_solid_frame())
    assert isinstance(result, float)


def test_is_quality_frame_solid_fails():
    assert not is_quality_frame(_solid_frame())


def test_is_quality_frame_checkerboard_passes():
    assert is_quality_frame(_checkerboard_frame())


def test_is_quality_frame_small_frame():
    """Sharpness check works on small frames."""
    small = _checkerboard_frame(h=8, w=8)
    assert is_quality_frame(small)


@pytest.mark.parametrize("h,w", [(480, 640), (360, 480), (224, 224)])
def test_compute_sharpness_various_sizes(h, w):
    """Sharpness is always zero for a solid frame regardless of size."""
    assert compute_sharpness(_solid_frame(h=h, w=w)) == 0.0
