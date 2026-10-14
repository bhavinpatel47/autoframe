"""Minimal V4L2 bindings: device discovery and v4l2loopback output.

Only the handful of ioctls autoframe needs are defined here. Struct layouts
and ioctl numbers match the Linux UAPI headers on 64-bit platforms; the sizes
are asserted in tests/test_v4l2.py and documented in docs/DESIGN.md so a port
to another language can reuse them.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import select
from dataclasses import dataclass, field
from pathlib import Path

u32 = ctypes.c_uint32

# --- ioctl number encoding (asm-generic/ioctl.h) -----------------------------

_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, nr: int, struct: type) -> int:
    return (direction << 30) | (ctypes.sizeof(struct) << 16) | (ord("V") << 8) | nr


# --- structs (linux/videodev2.h) ----------------------------------------------


class v4l2_capability(ctypes.Structure):
    _fields_ = [
        ("driver", ctypes.c_char * 16),
        ("card", ctypes.c_char * 32),
        ("bus_info", ctypes.c_char * 32),
        ("version", u32),
        ("capabilities", u32),
        ("device_caps", u32),
        ("reserved", u32 * 3),
    ]


class v4l2_pix_format(ctypes.Structure):
    _fields_ = [
        ("width", u32),
        ("height", u32),
        ("pixelformat", u32),
        ("field", u32),
        ("bytesperline", u32),
        ("sizeimage", u32),
        ("colorspace", u32),
        ("priv", u32),
        ("flags", u32),
        ("ycbcr_enc", u32),
        ("quantization", u32),
        ("xfer_func", u32),
    ]


class _v4l2_format_union(ctypes.Union):
    # The kernel union contains struct v4l2_window, which holds pointers, so
    # the union is 8-byte aligned. The c_void_p member reproduces that.
    _fields_ = [("pix", v4l2_pix_format), ("raw_data", ctypes.c_uint8 * 200), ("_align", ctypes.c_void_p)]


class v4l2_format(ctypes.Structure):
    _fields_ = [("type", u32), ("fmt", _v4l2_format_union)]


class v4l2_fmtdesc(ctypes.Structure):
    _fields_ = [
        ("index", u32),
        ("type", u32),
        ("flags", u32),
        ("description", ctypes.c_char * 32),
        ("pixelformat", u32),
        ("mbus_code", u32),
        ("reserved", u32 * 3),
    ]


class v4l2_frmsize_discrete(ctypes.Structure):
    _fields_ = [("width", u32), ("height", u32)]


class v4l2_frmsize_stepwise(ctypes.Structure):
    _fields_ = [
        ("min_width", u32),
        ("max_width", u32),
        ("step_width", u32),
        ("min_height", u32),
        ("max_height", u32),
        ("step_height", u32),
    ]


class _frmsize_union(ctypes.Union):
    _fields_ = [("discrete", v4l2_frmsize_discrete), ("stepwise", v4l2_frmsize_stepwise)]


class v4l2_frmsizeenum(ctypes.Structure):
    _fields_ = [
        ("index", u32),
        ("pixel_format", u32),
        ("type", u32),
        ("u", _frmsize_union),
        ("reserved", u32 * 2),
    ]


class v4l2_fract(ctypes.Structure):
    _fields_ = [("numerator", u32), ("denominator", u32)]


class v4l2_frmival_stepwise(ctypes.Structure):
    _fields_ = [("min", v4l2_fract), ("max", v4l2_fract), ("step", v4l2_fract)]


class _frmival_union(ctypes.Union):
    _fields_ = [("discrete", v4l2_fract), ("stepwise", v4l2_frmival_stepwise)]


class v4l2_frmivalenum(ctypes.Structure):
    _fields_ = [
        ("index", u32),
        ("pixel_format", u32),
        ("width", u32),
        ("height", u32),
        ("type", u32),
        ("u", _frmival_union),
        ("reserved", u32 * 2),
    ]


class v4l2_outputparm(ctypes.Structure):
    _fields_ = [
        ("capability", u32),
        ("outputmode", u32),
        ("timeperframe", v4l2_fract),
        ("extendedmode", u32),
        ("writebuffers", u32),
        ("reserved", u32 * 4),
    ]


class _streamparm_union(ctypes.Union):
    _fields_ = [("output", v4l2_outputparm), ("raw_data", ctypes.c_uint8 * 200)]


class v4l2_streamparm(ctypes.Structure):
    _fields_ = [("type", u32), ("parm", _streamparm_union)]


class v4l2_event_subscription(ctypes.Structure):
    _fields_ = [("type", u32), ("id", u32), ("flags", u32), ("reserved", u32 * 5)]


class _event_union(ctypes.Union):
    # struct v4l2_event_ctrl holds an __s64, so the union is 8-byte aligned.
    _fields_ = [("data", ctypes.c_uint8 * 64), ("_align", ctypes.c_int64)]


class timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_int64), ("tv_nsec", ctypes.c_long)]


class v4l2_event(ctypes.Structure):
    _fields_ = [
        ("type", u32),
        ("u", _event_union),
        ("pending", u32),
        ("sequence", u32),
        ("timestamp", timespec),
        ("id", u32),
        ("reserved", u32 * 8),
    ]


VIDIOC_QUERYCAP = _ioc(_IOC_READ, 0, v4l2_capability)
VIDIOC_ENUM_FMT = _ioc(_IOC_READ | _IOC_WRITE, 2, v4l2_fmtdesc)
VIDIOC_S_FMT = _ioc(_IOC_READ | _IOC_WRITE, 5, v4l2_format)
VIDIOC_S_PARM = _ioc(_IOC_READ | _IOC_WRITE, 22, v4l2_streamparm)
VIDIOC_ENUM_FRAMESIZES = _ioc(_IOC_READ | _IOC_WRITE, 74, v4l2_frmsizeenum)
VIDIOC_ENUM_FRAMEINTERVALS = _ioc(_IOC_READ | _IOC_WRITE, 75, v4l2_frmivalenum)
VIDIOC_DQEVENT = _ioc(_IOC_READ, 89, v4l2_event)
VIDIOC_SUBSCRIBE_EVENT = _ioc(_IOC_WRITE, 90, v4l2_event_subscription)

V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_VIDEO_OUTPUT = 0x00000002
V4L2_CAP_DEVICE_CAPS = 0x80000000

V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_BUF_TYPE_VIDEO_OUTPUT = 2
V4L2_FIELD_NONE = 1
V4L2_COLORSPACE_SRGB = 8
V4L2_FRMSIZE_TYPE_DISCRETE = 1
V4L2_FRMIVAL_TYPE_DISCRETE = 1

V4L2_EVENT_SUB_FL_SEND_INITIAL = 1
V4L2_EVENT_PRIVATE_START = 0x08000000
# v4l2loopback >= 0.12.6: sent when a consumer starts/stops streaming.
# Payload is a __u32 "count"; non-zero means a consumer is streaming.
V4L2_EVENT_PRI_CLIENT_USAGE = V4L2_EVENT_PRIVATE_START + 0x08E00000 + 1

LOOPBACK_DRIVER = "v4l2 loopback"


def fourcc(code: str) -> int:
    a, b, c, d = (ord(ch) for ch in code)
    return a | (b << 8) | (c << 16) | (d << 24)


def fourcc_str(value: int) -> str:
    return "".join(chr((value >> (8 * i)) & 0xFF) for i in range(4))


PIX_FMT_YUV420 = fourcc("YU12")  # planar I420


# --- device discovery ----------------------------------------------------------


@dataclass
class Mode:
    fourcc: str
    width: int
    height: int
    max_fps: float


@dataclass
class VideoDevice:
    path: str  # /dev/videoN
    card: str
    driver: str
    bus_info: str
    device_caps: int
    by_id: str | None = None  # name under /dev/v4l/by-id (stable across reboots)
    by_path: str | None = None  # name under /dev/v4l/by-path (stable per USB port)
    modes: list[Mode] = field(default_factory=list)

    @property
    def is_capture(self) -> bool:
        return bool(self.device_caps & V4L2_CAP_VIDEO_CAPTURE)

    @property
    def is_loopback(self) -> bool:
        return self.driver == LOOPBACK_DRIVER

    @property
    def stable_id(self) -> str:
        """Best identifier to store in config: survives re-enumeration."""
        return self.by_id or self.by_path or self.path

    def best_mode(self) -> Mode | None:
        """Largest mode that still runs at >= 24 fps, preferring MJPEG."""
        usable = [m for m in self.modes if m.max_fps >= 24] or self.modes
        if not usable:
            return None
        return max(usable, key=lambda m: (m.width * m.height, m.fourcc == "MJPG", m.max_fps))


def query_caps(fd: int) -> v4l2_capability:
    cap = v4l2_capability()
    fcntl.ioctl(fd, VIDIOC_QUERYCAP, cap)
    return cap


def _enum(fd: int, req: int, st) -> list:
    out = []
    while True:
        try:
            fcntl.ioctl(fd, req, st)
        except OSError as e:
            if e.errno in (errno.EINVAL, errno.ENOTTY):
                return out
            raise
        out.append(type(st).from_buffer_copy(st))
        st.index += 1


def list_modes(fd: int) -> list[Mode]:
    modes: list[Mode] = []
    desc = v4l2_fmtdesc(type=V4L2_BUF_TYPE_VIDEO_CAPTURE)
    for fmt in _enum(fd, VIDIOC_ENUM_FMT, desc):
        sizes = _enum(fd, VIDIOC_ENUM_FRAMESIZES, v4l2_frmsizeenum(pixel_format=fmt.pixelformat))
        for s in sizes:
            if s.type != V4L2_FRMSIZE_TYPE_DISCRETE:
                continue
            w, h = s.u.discrete.width, s.u.discrete.height
            ivals = _enum(
                fd,
                VIDIOC_ENUM_FRAMEINTERVALS,
                v4l2_frmivalenum(pixel_format=fmt.pixelformat, width=w, height=h),
            )
            fps = 0.0
            for iv in ivals:
                fr = iv.u.discrete if iv.type == V4L2_FRMIVAL_TYPE_DISCRETE else iv.u.stepwise.min
                if fr.numerator:
                    fps = max(fps, fr.denominator / fr.numerator)
            modes.append(Mode(fourcc_str(fmt.pixelformat), w, h, fps))
    return modes


def _stable_names() -> dict[str, tuple[str | None, str | None]]:
    names: dict[str, list[str | None]] = {}
    for idx, sub in enumerate(("by-id", "by-path")):
        d = Path("/dev/v4l") / sub
        if not d.is_dir():
            continue
        for link in sorted(d.iterdir()):
            target = os.path.realpath(link)
            entry = names.setdefault(target, [None, None])
            if entry[idx] is None:
                entry[idx] = link.name
    return {k: (v[0], v[1]) for k, v in names.items()}


def _video_index(path: str) -> int:
    try:
        return int(path.removeprefix("/dev/video"))
    except ValueError:
        return 1 << 30


def list_devices(with_modes: bool = True) -> list[VideoDevice]:
    """All V4L2 video nodes, including metadata and loopback nodes."""
    stable = _stable_names()
    devices: list[VideoDevice] = []
    for node in Path("/sys/class/video4linux").glob("video*"):
        path = f"/dev/{node.name}"
        try:
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            cap = query_caps(fd)
            caps = cap.device_caps if cap.capabilities & V4L2_CAP_DEVICE_CAPS else cap.capabilities
            dev = VideoDevice(
                path=path,
                card=cap.card.decode(errors="replace"),
                driver=cap.driver.decode(errors="replace"),
                bus_info=cap.bus_info.decode(errors="replace"),
                device_caps=caps,
            )
            dev.by_id, dev.by_path = stable.get(path, (None, None))
            if with_modes and dev.is_capture and not dev.is_loopback:
                dev.modes = list_modes(fd)
            devices.append(dev)
        except OSError:
            pass
        finally:
            os.close(fd)
    devices.sort(key=lambda d: _video_index(d.path))
    return devices


def list_cameras(with_modes: bool = True) -> list[VideoDevice]:
    """Physical capture devices (skips metadata nodes and loopback devices)."""
    return [d for d in list_devices(with_modes) if d.is_capture and not d.is_loopback]


class DeviceNotFound(LookupError):
    pass


def resolve_camera(selector: str, cameras: list[VideoDevice] | None = None) -> VideoDevice:
    """Find a camera by: "auto", list number, /dev path, by-id/by-path name, or name substring."""
    cams = cameras if cameras is not None else list_cameras()
    if not cams:
        raise DeviceNotFound("no cameras found")
    sel = selector.strip()
    if sel in ("", "auto"):
        return cams[0]
    if sel.isdigit():
        n = int(sel)
        if 1 <= n <= len(cams):
            return cams[n - 1]
        raise DeviceNotFound(f"camera number {n} out of range 1..{len(cams)}")
    if sel.startswith("/dev/"):
        real = os.path.realpath(sel)
        for c in cams:
            if c.path == real:
                return c
        raise DeviceNotFound(f"{sel} is not a camera (or is not connected)")
    for c in cams:
        if sel in (c.by_id, c.by_path):
            return c
    matches = [c for c in cams if sel.lower() in c.card.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise DeviceNotFound(f"{sel!r} matches several cameras; use its number or id from 'autoframe cameras'")
    raise DeviceNotFound(f"no connected camera matches {sel!r}")


def find_loopback(selector: str, label: str) -> VideoDevice:
    """Find the v4l2loopback device to write to.

    "auto" picks the loopback device whose card label equals `label`, or the
    only loopback device if there is exactly one.
    """
    loops = [d for d in list_devices(with_modes=False) if d.is_loopback]
    if selector and selector != "auto":
        real = os.path.realpath(selector)
        for d in loops:
            if d.path == real:
                return d
        raise DeviceNotFound(f"{selector} is not a v4l2loopback device")
    labelled = [d for d in loops if d.card == label]
    if labelled:
        return labelled[0]
    if len(loops) == 1:
        return loops[0]
    if not loops:
        raise DeviceNotFound("no v4l2loopback device found (is the v4l2loopback module loaded? see 'autoframe doctor')")
    raise DeviceNotFound(
        f"several v4l2loopback devices and none is labelled {label!r}; set output.device in the config"
    )


# --- loopback output -------------------------------------------------------------


class LoopbackOutput:
    """Writes I420 frames to a v4l2loopback device and reports consumer usage."""

    def __init__(self, path: str, width: int, height: int, fps: int):
        self.path = path
        self.width = width
        self.height = height
        self.frame_size = width * height * 3 // 2
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            self._set_format(fps)
            self.usage_events = self._subscribe_usage()
        except BaseException:
            os.close(self.fd)
            raise
        self._poll = select.poll()
        self._poll.register(self.fd, select.POLLPRI)
        self.consumers = 0 if self.usage_events else 1

    def _set_format(self, fps: int) -> None:
        fmt = v4l2_format(type=V4L2_BUF_TYPE_VIDEO_OUTPUT)
        pix = fmt.fmt.pix
        pix.width = self.width
        pix.height = self.height
        pix.pixelformat = PIX_FMT_YUV420
        pix.field = V4L2_FIELD_NONE
        pix.bytesperline = self.width
        pix.sizeimage = self.frame_size
        pix.colorspace = V4L2_COLORSPACE_SRGB
        fcntl.ioctl(self.fd, VIDIOC_S_FMT, fmt)
        if (pix.width, pix.height, pix.pixelformat) != (self.width, self.height, PIX_FMT_YUV420):
            raise OSError(
                f"{self.path} refused {self.width}x{self.height} I420 "
                f"(got {pix.width}x{pix.height} {fourcc_str(pix.pixelformat)}); "
                "is another program writing to it?"
            )
        parm = v4l2_streamparm(type=V4L2_BUF_TYPE_VIDEO_OUTPUT)
        parm.parm.output.timeperframe = v4l2_fract(1, fps)
        try:
            fcntl.ioctl(self.fd, VIDIOC_S_PARM, parm)
        except OSError:
            pass  # frame rate is advisory only

    def _subscribe_usage(self) -> bool:
        sub = v4l2_event_subscription(type=V4L2_EVENT_PRI_CLIENT_USAGE, flags=V4L2_EVENT_SUB_FL_SEND_INITIAL)
        try:
            fcntl.ioctl(self.fd, VIDIOC_SUBSCRIBE_EVENT, sub)
            return True
        except OSError:
            return False

    def write(self, frame) -> None:
        """Write one I420 frame (any object supporting the buffer protocol)."""
        try:
            os.write(self.fd, frame)
        except BlockingIOError:
            pass  # device buffer full; dropping a frame is fine

    def wait_usage(self, timeout: float) -> int | None:
        """Wait up to `timeout` seconds for a usage event.

        Returns the new consumer count, or None if nothing changed.
        """
        if not self.usage_events:
            return None
        if not self._poll.poll(max(0, int(timeout * 1000))):
            return None
        latest = None
        ev = v4l2_event()
        while True:
            try:
                fcntl.ioctl(self.fd, VIDIOC_DQEVENT, ev)
            except OSError:
                break
            if ev.type == V4L2_EVENT_PRI_CLIENT_USAGE:
                latest = ctypes.c_uint32.from_buffer(ev.u.data).value
            if ev.pending == 0:
                break
        if latest is not None:
            self.consumers = latest
        return latest

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
