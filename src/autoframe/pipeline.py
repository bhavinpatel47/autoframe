"""Per-frame processing: detect -> frame -> crop/scale -> I420.

Shared by the service and `autoframe bench`, so benchmark numbers measure
exactly what the service does.
"""

from __future__ import annotations

import time

import cv2
import numpy as np

from .config import Config
from .detector import FaceDetector
from .framing import Framer


class Timings:
    """Exponential moving averages of per-stage durations in milliseconds."""

    def __init__(self, alpha: float = 0.05):
        self.alpha = alpha
        self.ms: dict[str, float] = {}
        self.samples: dict[str, list[float]] | None = None  # set by bench to keep raw samples

    def add(self, stage: str, seconds: float) -> None:
        ms = seconds * 1000
        prev = self.ms.get(stage)
        self.ms[stage] = ms if prev is None else prev + (ms - prev) * self.alpha
        if self.samples is not None:
            self.samples.setdefault(stage, []).append(ms)


class Pipeline:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.detector = FaceDetector(cfg.tracking.score_threshold)
        self.framer: Framer | None = None
        self.timings = Timings()
        self.last_detect = float("-inf")
        self.last_step: float | None = None
        self.faces = 0

    def set_config(self, cfg: Config) -> None:
        self.cfg = cfg
        if self.framer is not None:
            self.framer.p = cfg.tracking

    def reset(self) -> None:
        self.framer = None
        self.last_step = None
        self.last_detect = float("-inf")

    def process(self, frame: np.ndarray, now: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Returns (output BGR frame, output I420 frame)."""
        now = time.monotonic() if now is None else now
        out = self.cfg.output
        trk = self.cfg.tracking
        h, w = frame.shape[:2]
        f = self.framer
        if f is None or (f.src_w, f.src_h) != (w, h):
            f = self.framer = Framer(w, h, out.width / out.height, trk)

        if not trk.enabled:
            f.observe([], now)
        elif now - self.last_detect >= 1.0 / max(trk.detect_fps, 0.1):
            t0 = time.perf_counter()
            faces = self.detector.detect(frame, trk.detect_width, trk.score_threshold)
            self.timings.add("detect", time.perf_counter() - t0)
            f.observe(faces, now)
            self.faces = len(faces)
            self.last_detect = now

        dt = 0.0 if self.last_step is None else now - self.last_step
        self.last_step = now
        f.step(dt)

        t0 = time.perf_counter()
        bgr = render(frame, f.crop(), out.width, out.height)
        t1 = time.perf_counter()
        yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)
        t2 = time.perf_counter()
        self.timings.add("render", t1 - t0)
        self.timings.add("convert", t2 - t1)
        return bgr, yuv


def render(frame: np.ndarray, crop: tuple[float, float, float, float], ow: int, oh: int) -> np.ndarray:
    """Crop (x, y, w, h) out of frame and scale it to ow x oh.

    The crop is snapped to whole source pixels: a sub-pixel warpAffine was
    4x slower (docs/DEVLOG.md) and the steps are invisible once scaled.
    Bilinear is used down to 1.5x shrink; beyond that, area interpolation
    avoids aliasing.
    """
    x, y, cw, ch = crop
    hh, ww = frame.shape[:2]
    wi, hi = min(int(round(cw)), ww), min(int(round(ch)), hh)
    xi = max(0, min(int(round(x)), ww - wi))
    yi = max(0, min(int(round(y)), hh - hi))
    interp = cv2.INTER_LINEAR if ow / wi >= 0.66 else cv2.INTER_AREA
    return cv2.resize(frame[yi : yi + hi, xi : xi + wi], (ow, oh), interpolation=interp)


def placeholder(width: int, height: int, text: str) -> np.ndarray:
    """A dark I420 frame with a short centred message."""
    img = np.full((height, width, 3), 28, dtype=np.uint8)
    scale = height / 720
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw, th), _ = cv2.getTextSize(text, font, scale, max(1, int(2 * scale)))
    org = ((width - tw) // 2, (height + th) // 2)
    cv2.putText(img, text, org, font, scale, (170, 170, 170), max(1, int(2 * scale)), cv2.LINE_AA)
    return cv2.cvtColor(img, cv2.COLOR_BGR2YUV_I420)


def draw_debug(frame: np.ndarray, framer: Framer, out_bgr: np.ndarray, max_width: int = 960) -> np.ndarray:
    """Source frame with faces (green), target (yellow) and crop (red), beside the output."""
    s = min(1.0, max_width / frame.shape[1])
    img = cv2.resize(frame, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    for face in framer.faces:
        p0 = (int(face.x * s), int(face.y * s))
        p1 = (int((face.x + face.w) * s), int((face.y + face.h) * s))
        cv2.rectangle(img, p0, p1, (0, 220, 0), 2)
    t = framer.target
    tw = t.h * framer.aspect
    cv2.rectangle(
        img,
        (int((t.cx - tw / 2) * s), int((t.cy - t.h / 2) * s)),
        (int((t.cx + tw / 2) * s), int((t.cy + t.h / 2) * s)),
        (0, 220, 220),
        1,
    )
    x, y, w, h = framer.crop()
    cv2.rectangle(img, (int(x * s), int(y * s)), (int((x + w) * s), int((y + h) * s)), (0, 0, 255), 2)
    out = cv2.resize(out_bgr, (int(out_bgr.shape[1] * img.shape[0] / out_bgr.shape[0]), img.shape[0]))
    return np.hstack([img, out])
