"""The autoframe service: real camera -> pipeline -> v4l2loopback device."""

from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path

from . import config as configmod
from .camera import Camera
from .config import Config
from .control import ControlServer
from .pipeline import Pipeline, draw_debug, placeholder
from .v4l2 import DeviceNotFound, LoopbackOutput, VideoDevice, find_loopback, list_cameras, resolve_camera

log = logging.getLogger(__name__)

IDLE_FRAME_INTERVAL = 1.0  # seconds between placeholder frames while nobody watches
CAMERA_RETRY = 2.0


class Service:
    def __init__(self, cfg: Config, cfg_path: Path, debug_window: bool = False):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.debug_window = debug_window
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._pending_cfg: Config | None = None
        self.started = time.monotonic()

        self.out: LoopbackOutput | None = None
        self.cam: Camera | None = None
        self.cam_dev: VideoDevice | None = None
        self.cam_key: tuple | None = None
        self.cam_error = ""
        self.pipeline = Pipeline(cfg)
        self.frames_out = 0
        self.out_fps = 0.0
        self._placeholders: dict[str, bytes] = {}

    # --- control -----------------------------------------------------------------

    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "status":
            return {"ok": True, **self.status()}
        if cmd == "reload":
            cfg, warnings = configmod.load(self.cfg_path)
            restart = (cfg.output != self.cfg.output) or (cfg.service != self.cfg.service)
            with self._lock:
                self._pending_cfg = cfg
            return {"ok": True, "warnings": warnings, "restart_required": restart}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def status(self) -> dict:
        cam = self.cam
        f = self.pipeline.framer
        out = self.out
        return {
            "uptime_s": round(time.monotonic() - self.started, 1),
            "streaming": bool(out and out.consumers > 0),
            "usage_events": bool(out and out.usage_events),
            "output": {
                "device": out.path if out else None,
                "size": [self.cfg.output.width, self.cfg.output.height],
                "fps": round(self.out_fps, 1),
            },
            "camera": {
                "selector": self.cfg.camera.device,
                "device": self.cam_dev.path if self.cam_dev else None,
                "id": self.cam_dev.stable_id if self.cam_dev else None,
                "name": self.cam_dev.card if self.cam_dev else None,
                "state": cam.state if cam else ("error" if self.cam_error else "closed"),
                "error": (cam.error if cam else self.cam_error) or None,
                "size": list(cam.size) if cam and cam.size else None,
                "fps": round(cam.fps, 1) if cam else 0.0,
            },
            "tracking": {
                "enabled": self.cfg.tracking.enabled,
                "faces": self.pipeline.faces if cam else 0,
                "zoom": round(f.zoom, 2) if f and cam else 1.0,
                "crop": [round(v) for v in f.crop()] if f and cam else None,
            },
            "timing_ms": {
                **{k: round(v, 2) for k, v in self.pipeline.timings.ms.items()},
                **({"camera_read": round(cam.read_ms, 2)} if cam else {}),
            },
        }

    # --- camera lifecycle -----------------------------------------------------------

    def _camera_key(self) -> tuple:
        c = self.cfg.camera
        return (c.device, c.width, c.height, c.fps, c.format)

    def _start_camera(self) -> None:
        c = self.cfg.camera
        try:
            dev = resolve_camera(c.device, list_cameras(with_modes=False))
        except DeviceNotFound as e:
            if self.cam_error != str(e):
                log.warning("camera: %s", e)
            self.cam_error = str(e)
            return
        self.cam_error = ""
        self.cam_dev = dev
        self.cam_key = self._camera_key()
        log.info("using camera %s (%s)", dev.path, dev.card)
        self.pipeline.reset()
        self.cam = Camera(dev.path, c.width, c.height, c.fps, c.format).start()

    def _stop_camera(self) -> None:
        if self.cam is not None:
            log.info("releasing camera %s", self.cam.path)
            self.cam.stop()
            self.cam = None

    def _apply_pending_config(self) -> None:
        with self._lock:
            cfg, self._pending_cfg = self._pending_cfg, None
        if cfg is None:
            return
        self.cfg = cfg
        self.pipeline.set_config(cfg)
        log.info("configuration reloaded")
        if self.cam is not None and self._camera_key() != self.cam_key:
            self._stop_camera()  # the main loop reopens it with the new settings

    # --- main loop -------------------------------------------------------------------

    def _placeholder(self, text: str) -> bytes:
        img = self._placeholders.get(text)
        if img is None:
            img = self._placeholders[text] = placeholder(self.cfg.output.width, self.cfg.output.height, text).tobytes()
        return img

    def run(self) -> None:
        o = self.cfg.output
        dev = find_loopback(o.device, o.label)
        self.out = out = LoopbackOutput(dev.path, o.width, o.height, o.fps)
        log.info("writing %dx%d@%d to %s (%s)", o.width, o.height, o.fps, dev.path, dev.card)
        if not out.usage_events:
            log.warning("this v4l2loopback does not report consumers; the camera will stay open")
        control = ControlServer(self.handle).start()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop_event.set())
        signal.signal(signal.SIGHUP, lambda *_: self.handle({"cmd": "reload"}))

        last_active = float("-inf")
        last_write = 0.0
        last_cam_try = float("-inf")
        seq = 0
        was_streaming = False
        frame_period = 1.0 / o.fps
        try:
            out.write(self._placeholder("Autoframe - camera idle"))
            while not self.stop_event.is_set():
                self._apply_pending_config()
                svc = self.cfg.service
                # Block on usage events only while idle; otherwise just check.
                out.wait_usage(0 if self.cam or out.consumers else IDLE_FRAME_INTERVAL)
                now = time.monotonic()
                streaming = out.consumers > 0
                if streaming != was_streaming:
                    log.info("consumer %s", "started streaming" if streaming else "stopped streaming")
                    was_streaming = streaming
                if streaming:
                    last_active = now
                want = not svc.on_demand or streaming or now - last_active < svc.linger

                if want and self.cam is None and now - last_cam_try >= CAMERA_RETRY:
                    last_cam_try = now
                    self._start_camera()
                elif not want and self.cam is not None:
                    self._stop_camera()
                    out.write(self._placeholder("Autoframe - camera idle"))

                if self.cam is None:
                    if now - last_write >= (frame_period * 5 if want else IDLE_FRAME_INTERVAL):
                        msg = f"No camera: {self.cam_error}" if self.cam_error else "Autoframe - camera idle"
                        out.write(self._placeholder(msg if want else "Autoframe - camera idle"))
                        last_write = now
                    elif want:
                        time.sleep(frame_period)
                    continue

                item = self.cam.wait_frame(seq, timeout=0.1)
                now = time.monotonic()
                if item is None:
                    if now - last_write >= 0.1:
                        cam = self.cam
                        msg = cam.error if cam.state == "error" else "Starting camera..."
                        out.write(self._placeholder(msg))
                        last_write = now
                    continue
                seq, _, frame = item
                t0 = time.perf_counter()
                bgr, yuv = self.pipeline.process(frame, now)
                t1 = time.perf_counter()
                out.write(yuv)
                t2 = time.perf_counter()
                tm = self.pipeline.timings
                tm.add("write", t2 - t1)
                tm.add("pipeline", t1 - t0)
                if last_write:
                    self.out_fps += (1.0 / max(now - last_write, 1e-3) - self.out_fps) * 0.05
                last_write = now
                self.frames_out += 1
                if self.debug_window and self.pipeline.framer is not None:
                    self._show_debug(frame, bgr)
        finally:
            self._stop_camera()
            control.close()
            out.close()
            if self.debug_window:
                import cv2

                cv2.destroyAllWindows()
            log.info("stopped")

    def _show_debug(self, frame, bgr) -> None:
        import cv2

        cv2.imshow("autoframe debug (q to quit)", draw_debug(frame, self.pipeline.framer, bgr))
        if cv2.waitKey(1) & 0xFF == ord("q"):
            self.stop_event.set()
