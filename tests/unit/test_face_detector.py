"""Unit tests for src/video/face_detector.py."""

import base64
from unittest.mock import MagicMock, patch

import numpy as np

from src.video.face_detector import FaceDetector


def _blank_frame(h: int = 480, w: int = 640) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def _make_mock_detection(
    xmin: float = 0.3,
    ymin: float = 0.2,
    width: float = 0.2,
    height: float = 0.3,
) -> MagicMock:
    """Build a fake MediaPipe Detection object."""
    bbox = MagicMock()
    bbox.xmin = xmin
    bbox.ymin = ymin
    bbox.width = width
    bbox.height = height
    loc = MagicMock()
    loc.relative_bounding_box = bbox
    det = MagicMock()
    det.location_data = loc
    return det


def _make_mp_mock(detections):
    """Patch mediapipe so no real model is loaded."""
    mp_mock = MagicMock()
    detector_instance = MagicMock()
    detector_instance.process.return_value = MagicMock(detections=detections)
    mp_mock.solutions.face_detection.FaceDetection.return_value = detector_instance
    return mp_mock, detector_instance


def test_detect_returns_none_when_no_detections():
    mp_mock, _ = _make_mp_mock(detections=None)
    with patch("src.video.face_detector.mp", mp_mock):
        detector = FaceDetector()
        result = detector.detect(_blank_frame())
    assert result is None
    detector.close()


def test_detect_returns_none_when_empty_detections():
    mp_mock, _ = _make_mp_mock(detections=[])
    with patch("src.video.face_detector.mp", mp_mock):
        detector = FaceDetector()
        result = detector.detect(_blank_frame())
    assert result is None
    detector.close()


def test_detect_returns_base64_png_on_face():
    mp_mock, _ = _make_mp_mock(detections=[_make_mock_detection()])
    with patch("src.video.face_detector.mp", mp_mock):
        detector = FaceDetector()
        result = detector.detect(_blank_frame())

    assert result is not None
    decoded = base64.b64decode(result)
    assert decoded[:4] == b"\x89PNG"
    detector.close()


def test_detect_increments_streak_on_no_face():
    mp_mock, inst = _make_mp_mock(detections=None)
    with patch("src.video.face_detector.mp", mp_mock):
        detector = FaceDetector()
        for _ in range(3):
            detector.detect(_blank_frame())
        assert detector._no_face_streak == 3
    detector.close()


def test_detect_resets_streak_on_face():
    mp_mock, inst = _make_mp_mock(detections=None)
    no_face = MagicMock(detections=None)
    with_face = MagicMock(detections=[_make_mock_detection()])
    inst.process.side_effect = [no_face, no_face, with_face]
    with patch("src.video.face_detector.mp", mp_mock):
        detector = FaceDetector()
        detector.detect(_blank_frame())
        detector.detect(_blank_frame())
        assert detector._no_face_streak == 2
        detector.detect(_blank_frame())
        assert detector._no_face_streak == 0
    detector.close()


def test_detect_returns_none_for_zero_area_crop():
    """Detection bbox that maps to a zero-area crop returns None."""
    mp_mock, _ = _make_mp_mock(
        detections=[_make_mock_detection(xmin=1.0, ymin=1.0, width=0.0, height=0.0)]
    )
    with patch("src.video.face_detector.mp", mp_mock):
        detector = FaceDetector()
        result = detector.detect(_blank_frame(h=10, w=10))
    assert result is None
    detector.close()


def test_close_calls_mediapipe_close():
    mp_mock, inst = _make_mp_mock(detections=None)
    with patch("src.video.face_detector.mp", mp_mock):
        detector = FaceDetector()
        detector.close()
    inst.close.assert_called_once()
