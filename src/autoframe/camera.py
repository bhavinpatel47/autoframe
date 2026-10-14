"""Threaded camera capture that always hands out the newest frame."""

from __future__ import annotations

import logging
import threading
import time

import cv2
import numpy as np

log = logging.getLogger(__name__)


class Camera:
    """Reads frames on a background thread and reconnects if the camera drops.

    Decoding runs on the reader thread (OpenCV releases the GIL), so it
    overlaps with detection and scaling on the main thread. Old frames are
    dropped rather than queued to keep latency at one frame.
    """

    def __init__(self, path: str, width: int, height: int, fps: int, fourcc: str = "MJPG"):
        self.path = path
        self.req = (width, height, fps, fourcc)
        self.state = "opening"  # opening | running | error | stopped
        self.error = ""
        self.size: tuple[int, int] | None = None
        self.fps = 0.0
        self.read_ms = 0.0  # time spent in cap.read() (wait + decode)
        self._frame: np.ndarray | None = None
        self._seq = 0
        self._stamp = 0.0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)

    def start(self) -> Camera:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        self._thread.join(timeout=3)
        self.state = "stopped"

    def wait_frame(self, after_seq: int, timeout: float) -> tuple[int, float, np.ndarray] | None:
        """Return (seq, timestamp, frame) newer than after_seq, or None on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._seq <= after_seq or self._frame is None:
                left = deadline - time.monotonic()
                if left <= 0 or self._stop.is_set():
                    return None
                self._cond.wait(left)
            return self._seq, self._stamp, self._frame

    def _open(self) -> cv2.VideoCapture | None:
        w, h, fps, fourcc = self.req
        cap = cv2.VideoCapture(self.path, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            return None
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, fps)
        return cap

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            self.state = "opening"
            cap = self._open()
            if cap is None:
                self.state = "error"
                self.error = f"cannot open {self.path} (unplugged, or in use by another app?)"
                log.warning(self.error)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 5.0)
                continue
            self.size = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
            log.info("camera %s opened at %dx%d", self.path, *self.size)
            failures = 0
            last = 0.0
            try:
                while not self._stop.is_set():
                    t0 = time.monotonic()
                    ok, frame = cap.read()
                    t1 = time.monotonic()
                    if not ok or frame is None:
                        failures += 1
                        if failures >= 10:
                            self.state = "error"
                            self.error = f"{self.path} stopped delivering frames"
                            log.warning(self.error)
                            break
                        continue
                    failures = 0
                    backoff = 0.5
                    self.state = "running"
                    self.error = ""
                    self.read_ms += ((t1 - t0) * 1000 - self.read_ms) * 0.05
                    if last:
                        self.fps += (1.0 / max(t1 - last, 1e-3) - self.fps) * 0.05
                    last = t1
                    with self._cond:
                        self._frame = frame
                        self._seq += 1
                        self._stamp = t1
                        self._cond.notify_all()
            finally:
                cap.release()
            if not self._stop.is_set():
                self._stop.wait(backoff)
