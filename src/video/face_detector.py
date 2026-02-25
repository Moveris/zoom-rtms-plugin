"""Face detection and 224×224 crop extraction using MediaPipe.

Crops are expanded to 3× the detected face size so that the face occupies
approximately 30% of the image — matching the fast-check-crops framing
requirement in the Moveris API docs.
"""

import base64
import logging

import cv2
import mediapipe as mp
import numpy as np

logger = logging.getLogger(__name__)

_CROP_SIZE = 224  # Moveris fast-check-crops requires 224×224 px
_BOX_SCALE = 3.0  # expand bounding box to 3× face size
_NO_FACE_WARN_STREAK = 5  # warn after this many consecutive frameless results


class FaceDetector:
    """Detects the primary face in a BGR frame and returns a 224×224 PNG crop.

    Uses MediaPipe Face Detection with model_selection=1 (optimised for faces
    up to ~5m from the camera, suitable for video calls).

    Usage::

        detector = FaceDetector()
        image_b64 = detector.detect(frame)   # returns base64 PNG or None
        detector.close()
    """

    def __init__(self) -> None:
        self._detector = mp.solutions.face_detection.FaceDetection(
            model_selection=1,
            min_detection_confidence=0.7,
        )
        self._no_face_streak: int = 0

    def detect(self, frame: np.ndarray) -> str | None:
        """Detect the primary face in a BGR frame.

        Args:
            frame: BGR ndarray from the RTMS video callback.

        Returns:
            Base64-encoded 224×224 PNG string (no data-URI prefix), or None
            if no face was detected or the crop could not be encoded.
        """
        h, w = frame.shape[:2]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self._detector.process(rgb)

        if not results.detections:
            self._no_face_streak += 1
            if self._no_face_streak >= _NO_FACE_WARN_STREAK:
                logger.warning(
                    "No face detected for %d consecutive frames",
                    self._no_face_streak,
                )
            return None

        self._no_face_streak = 0
        detection = results.detections[0]
        bbox = detection.location_data.relative_bounding_box

        # Convert relative bbox to absolute pixel coordinates
        x = bbox.xmin * w
        y = bbox.ymin * h
        bw = bbox.width * w
        bh = bbox.height * h

        # Expand to 3× face size, centred on the face
        cx = x + bw / 2
        cy = y + bh / 2
        half = max(bw, bh) * _BOX_SCALE / 2

        x1 = max(0, int(cx - half))
        y1 = max(0, int(cy - half))
        x2 = min(w, int(cx + half))
        y2 = min(h, int(cy + half))

        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return None

        resized = cv2.resize(
            crop, (_CROP_SIZE, _CROP_SIZE), interpolation=cv2.INTER_AREA
        )

        ok, buf = cv2.imencode(".png", resized)
        if not ok:
            logger.warning("Failed to encode face crop as PNG")
            return None

        return base64.b64encode(buf.tobytes()).decode("ascii")

    def close(self) -> None:
        """Release MediaPipe resources."""
        self._detector.close()
