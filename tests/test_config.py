import pytest

from autoframe import config


def test_roundtrip(tmp_path):
    path = tmp_path / "config.toml"
    cfg = config.Config()
    cfg.set("camera.device", "usb-Some_\"Cam\"-video-index0")
    cfg.set("tracking.max_zoom", "1.6")
    cfg.set("tracking.enabled", "off")
    cfg.set("output.fps", 25)
    config.save(cfg, path)
    loaded, warnings = config.load(path)
    assert warnings == []
    assert loaded == cfg
    assert "detect_width" not in path.read_text()  # defaults are not written


def test_bad_keys_and_values():
    cfg = config.Config()
    with pytest.raises(KeyError):
        cfg.set("tracking.nope", 1)
    with pytest.raises(KeyError):
        cfg.set("nope", 1)
    with pytest.raises(ValueError):
        cfg.set("tracking.enabled", "maybe")
    with pytest.raises(ValueError):
        cfg.set("output.width", "wide")


def test_unknown_keys_warn(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[tracking]\nmax_zoom = 1.5\nbogus = 1\n")
    cfg, warnings = config.load(path)
    assert cfg.tracking.max_zoom == 1.5
    assert len(warnings) == 1
