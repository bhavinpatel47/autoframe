"""Auto-framing: turn face boxes into a smoothly moving crop window.

This module is pure Python with no OpenCV dependency so it can be unit
tested and ported directly. See docs/DESIGN.md for the algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import TrackingConfig


@dataclass
class Face:
    x: float
    y: float
    w: float
    h: float
    score: float = 1.0


@dataclass
class View:
    """A crop window: centre and height in source pixels (width follows the aspect)."""

    cx: float
    cy: float
    h: float


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


class Framer:
    def __init__(self, src_w: int, src_h: int, aspect: float, params: TrackingConfig):
        self.src_w = src_w
        self.src_h = src_h
        self.aspect = aspect
        self.p = params
        # Largest crop with the output aspect that fits in the source.
        self.h_max = min(float(src_h), src_w / aspect)
        self.reset()

    def reset(self) -> None:
        self.current = self.full_view()
        self.target = self.full_view()
        self.velocity = [0.0, 0.0, 0.0]
        self.last_seen = float("-inf")
        self.pending = 0
        self.faces: list[Face] = []

    def full_view(self) -> View:
        return View(self.src_w / 2, self.src_h / 2, self.h_max)

    def desired(self, faces: list[Face]) -> View | None:
        """Where the crop should be for these faces, ignoring smoothing."""
        if not faces:
            return None
        p = self.p
        biggest = max(f.h for f in faces)
        faces = [f for f in faces if f.h >= p.min_face_ratio * biggest]
        fh = biggest
        x0 = min(f.x for f in faces)
        y0 = min(f.y for f in faces)
        x1 = max(f.x + f.w for f in faces)
        y1 = max(f.y + f.h for f in faces)
        margin = p.margin * fh

        # Height needed: single-person framing, the group's width and
        # height with margins, and room above the top face given eye_line.
        h = max(
            fh * p.face_scale,
            (x1 - x0 + 2 * margin) / self.aspect,
            (y1 - y0) + 2 * margin,
            ((y1 - y0) / 2 + 0.3 * fh) / max(p.eye_line, 0.05),
        )
        lo = self.h_max / max(p.max_zoom, 1.0)
        h = _clamp(h, lo, max(lo, self.h_max / max(p.min_zoom, 1.0)))

        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2 + h * (0.5 - p.eye_line)
        return self._clamp_view(View(cx, cy, h))

    def _clamp_view(self, v: View) -> View:
        h = _clamp(v.h, 1.0, self.h_max)
        w = h * self.aspect
        return View(
            _clamp(v.cx, w / 2, self.src_w - w / 2),
            _clamp(v.cy, h / 2, self.src_h - h / 2),
            h,
        )

    def _outside_dead_zone(self, d: View) -> bool:
        t = self.target
        tw = t.h * self.aspect
        return (
            abs(d.cx - t.cx) > self.p.dead_zone * tw
            or abs(d.cy - t.cy) > self.p.dead_zone * t.h
            or abs(d.h / t.h - 1.0) > self.p.zoom_dead_zone
        )

    def observe(self, faces: list[Face], now: float) -> None:
        """Feed one detection result (possibly empty)."""
        self.faces = faces
        if not self.p.enabled:
            self.target = self.full_view()
            self.pending = 0
            return
        d = self.desired(faces)
        if d is None:
            self.pending = 0
            if self.p.zoom_out_when_lost and now - self.last_seen > self.p.hold_time:
                self.target = self.full_view()
            return
        self.last_seen = now
        if self._outside_dead_zone(d):
            self.pending += 1
            if self.pending >= max(1, self.p.confirm_frames):
                self.target = d
                self.pending = 0
        else:
            self.pending = 0

    def step(self, dt: float) -> View:
        """Advance the critically damped spring towards the target by dt seconds."""
        dt = _clamp(dt, 0.0, 0.1)
        cur = [self.current.cx, self.current.cy, self.current.h]
        tgt = [self.target.cx, self.target.cy, self.target.h]
        omegas = (self.p.pan_speed, self.p.pan_speed, self.p.zoom_speed)
        for i, w in enumerate(omegas):
            a = w * w * (tgt[i] - cur[i]) - 2.0 * w * self.velocity[i]
            self.velocity[i] += a * dt
            cur[i] += self.velocity[i] * dt
        self.current = self._clamp_view(View(*cur))
        return self.current

    def crop(self) -> tuple[float, float, float, float]:
        """Current crop as (x, y, w, h) in source pixels, inside the frame."""
        v = self.current
        w = v.h * self.aspect
        x = _clamp(v.cx - w / 2, 0.0, self.src_w - w)
        y = _clamp(v.cy - v.h / 2, 0.0, self.src_h - v.h)
        return x, y, w, v.h

    @property
    def zoom(self) -> float:
        return self.h_max / self.current.h
