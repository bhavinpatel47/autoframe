"""Face detection with OpenCV's YuNet model."""

from __future__ import annotations

from importlib import resources

import cv2
import numpy as np

from .framing import Face

MODEL_NAME = "face_detection_yunet_2023mar.onnx"


def model_path() -> str:
    return str(resources.files("autoframe") / "models" / MODEL_NAME)


def _shrink(frame: np.ndarray, dw: int, dh: int) -> np.ndarray:
    """Cheap anti-aliased downscale: bilinear to 2x the target, then 2x2 area.

    About 10x faster than a single INTER_AREA resize from 1080p (docs/DEVLOG.md).
    """
    h, w = frame.shape[:2]
    if w > dw * 2:
        frame = cv2.resize(frame, (dw * 2, dh * 2), interpolation=cv2.INTER_LINEAR)
    return cv2.resize(frame, (dw, dh), interpolation=cv2.INTER_AREA)


class FaceDetector:
    def __init__(self, score_threshold: float = 0.7, model: str | None = None):
        self._det = cv2.FaceDetectorYN.create(model or model_path(), "", (320, 180), score_threshold, 0.3, 50)
        self._size: tuple[int, int] | None = None
        self._threshold = score_threshold

    def detect(self, frame: np.ndarray, detect_width: int, score_threshold: float) -> list[Face]:
        """Detect faces in a BGR frame; boxes are returned in frame coordinates."""
        h, w = frame.shape[:2]
        dw = min(detect_width, w)
        scale = dw / w
        dh = max(1, round(h * scale))
        small = frame if dw == w else _shrink(frame, dw, dh)
        if self._size != (dw, dh):
            self._det.setInputSize((dw, dh))
            self._size = (dw, dh)
        if score_threshold != self._threshold:
            self._det.setScoreThreshold(score_threshold)
            self._threshold = score_threshold
        _, faces = self._det.detect(small)
        if faces is None:
            return []
        inv = 1.0 / scale
        return [Face(float(f[0]) * inv, float(f[1]) * inv, float(f[2]) * inv, float(f[3]) * inv, float(f[-1])) for f in faces]
