from autoframe.config import TrackingConfig
from autoframe.framing import Face, Framer

W, H, ASPECT = 1920, 1080, 16 / 9


def settle(f, faces, seconds=4.0, fps=30, detect_every=2):
    t = 0.0
    for i in range(int(seconds * fps)):
        if i % detect_every == 0:
            f.observe(faces, t)
        f.step(1 / fps)
        t += 1 / fps
    return t


def test_starts_full_frame():
    f = Framer(W, H, ASPECT, TrackingConfig())
    assert f.crop() == (0.0, 0.0, 1920.0, 1080.0)


def test_zooms_onto_single_face():
    p = TrackingConfig()
    f = Framer(W, H, ASPECT, p)
    face = Face(1300, 300, 150, 180)
    settle(f, [face])
    x, y, w, h = f.crop()
    assert abs(h - 180 * p.face_scale) < 20
    assert x <= face.x and face.x + face.w <= x + w  # face inside the crop
    # face centre sits near eye_line from the top
    assert abs((face.y + face.h / 2 - y) / h - p.eye_line) < 0.05


def test_respects_max_zoom():
    p = TrackingConfig(max_zoom=1.5)
    f = Framer(W, H, ASPECT, p)
    settle(f, [Face(900, 400, 40, 50)])
    assert f.crop()[3] >= H / 1.5 - 1


def test_group_fits_everyone():
    f = Framer(W, H, ASPECT, TrackingConfig())
    faces = [Face(200, 300, 150, 180), Face(1500, 350, 150, 180)]
    settle(f, faces)
    x, y, w, h = f.crop()
    for face in faces:
        assert x <= face.x and face.x + face.w <= x + w


def test_crop_stays_inside_frame():
    f = Framer(W, H, ASPECT, TrackingConfig())
    settle(f, [Face(1850, 5, 60, 70)])
    x, y, w, h = f.crop()
    assert x >= 0 and y >= 0 and x + w <= W + 1e-6 and y + h <= H + 1e-6


def test_dead_zone_ignores_jitter():
    f = Framer(W, H, ASPECT, TrackingConfig())
    settle(f, [Face(900, 400, 150, 180)])
    target = f.target
    settle(f, [Face(905, 402, 152, 178)], seconds=1)
    assert f.target == target


def test_single_false_detection_is_ignored():
    f = Framer(W, H, ASPECT, TrackingConfig(confirm_frames=3))
    settle(f, [Face(900, 400, 150, 180)])
    target = f.target
    f.observe([Face(100, 100, 150, 180)], 10.0)
    assert f.target == target


def test_zooms_out_after_losing_face():
    p = TrackingConfig(hold_time=1.0)
    f = Framer(W, H, ASPECT, p)
    t = settle(f, [Face(900, 400, 150, 180)])
    f.observe([], t + 0.5)
    assert f.target.h < f.h_max  # still holding
    f.observe([], t + 1.5)
    assert f.target == f.full_view()


def test_tracking_disabled_shows_full_frame():
    p = TrackingConfig(enabled=False)
    f = Framer(W, H, ASPECT, p)
    settle(f, [Face(900, 400, 150, 180)])
    x, y, w, h = f.crop()
    assert (round(w), round(h)) == (1920, 1080)


def test_min_zoom_leaves_room_to_pan():
    f = Framer(W, H, ASPECT, TrackingConfig(min_zoom=1.25))
    settle(f, [Face(700, 300, 380, 420)])  # big, close face
    assert abs(f.crop()[3] - H / 1.25) < 1
