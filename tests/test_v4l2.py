import ctypes

from autoframe import v4l2

# Sizes and ioctl numbers from the x86_64/arm64 kernel UAPI (see docs/DESIGN.md).
SIZES = {
    v4l2.v4l2_capability: 104,
    v4l2.v4l2_format: 208,
    v4l2.v4l2_fmtdesc: 64,
    v4l2.v4l2_frmsizeenum: 44,
    v4l2.v4l2_frmivalenum: 52,
    v4l2.v4l2_streamparm: 204,
    v4l2.v4l2_event_subscription: 32,
    v4l2.v4l2_event: 136,
}


def test_struct_sizes():
    for struct, size in SIZES.items():
        assert ctypes.sizeof(struct) == size, struct.__name__


def test_ioctl_numbers():
    assert v4l2.VIDIOC_QUERYCAP == 0x80685600
    assert v4l2.VIDIOC_S_FMT == 0xC0D05605
    assert v4l2.VIDIOC_DQEVENT == 0x80885659
    assert v4l2.VIDIOC_SUBSCRIBE_EVENT == 0x4020565A
    assert v4l2.V4L2_EVENT_PRI_CLIENT_USAGE == 0x10E00001


def test_fourcc_roundtrip():
    assert v4l2.fourcc_str(v4l2.fourcc("MJPG")) == "MJPG"
    assert v4l2.PIX_FMT_YUV420 == 0x32315559


def _cam(path, card, by_id):
    return v4l2.VideoDevice(path, card, "uvcvideo", "usb", v4l2.V4L2_CAP_VIDEO_CAPTURE, by_id=by_id)


def test_resolve_camera():
    cams = [_cam("/dev/video0", "Kreo Owl Camera", "usb-kreo"), _cam("/dev/video2", "HD UVC WebCam", "usb-hd")]
    assert v4l2.resolve_camera("auto", cams).path == "/dev/video0"
    assert v4l2.resolve_camera("2", cams).path == "/dev/video2"
    assert v4l2.resolve_camera("usb-hd", cams).path == "/dev/video2"
    assert v4l2.resolve_camera("kreo", cams).path == "/dev/video0"
    for bad in ("3", "nothing"):
        try:
            v4l2.resolve_camera(bad, cams)
        except v4l2.DeviceNotFound:
            pass
        else:
            raise AssertionError(bad)
