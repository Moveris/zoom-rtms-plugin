"""Frame quality filtering via Laplacian variance sharpness scoring."""

import cv2
import numpy as np

_SHARPNESS_THRESHOLD = 50.0


def compute_sharpness(frame: np.ndarray) -> float:
    """Return the Laplacian variance of a BGR frame.

    Higher values indicate a sharper frame.  Blurry or low-contrast frames
    score near zero and should be discarded before face detection.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def is_quality_frame(frame: np.ndarray) -> bool:
    """Return True if the frame is sharp enough for face detection.

    Frames below the threshold are typically motion-blurred or heavily
    compressed and produce unreliable face detections.
    """
    return compute_sharpness(frame) > _SHARPNESS_THRESHOLD
