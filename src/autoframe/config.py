"""Configuration: defaults, TOML load/save, and dotted-key access."""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


@dataclass
class CameraConfig:
    # "auto", a number from `autoframe cameras`, a /dev/videoN path, a
    # /dev/v4l/by-id name, or part of the camera's name.
    device: str = "auto"
    width: int = 1920
    height: int = 1080
    fps: int = 30
    format: str = "MJPG"


@dataclass
class OutputConfig:
    # "auto" finds the v4l2loopback device labelled `label`.
    device: str = "auto"
    label: str = "Autoframe Camera"
    width: int = 1280
    height: int = 720
    fps: int = 30


@dataclass
class TrackingConfig:
    enabled: bool = True
    detect_width: int = 320  # detector input width; larger finds smaller faces
    detect_fps: float = 15.0
    score_threshold: float = 0.7
    min_face_ratio: float = 0.45  # ignore faces smaller than this fraction of the largest
    face_scale: float = 3.2  # crop height as a multiple of face height (single person)
    margin: float = 1.0  # extra space around a group, in face heights
    eye_line: float = 0.40  # faces sit this far down the crop (0 = top, 1 = bottom)
    min_zoom: float = 1.25  # with faces in view, crop at least this much (leaves room to pan)
    max_zoom: float = 2.0  # never crop smaller than source/max_zoom
    dead_zone: float = 0.06  # ignore moves smaller than this fraction of the crop
    zoom_dead_zone: float = 0.10  # ignore size changes smaller than this ratio
    confirm_frames: int = 3  # detections a new framing must persist before moving
    pan_speed: float = 3.5  # spring stiffness for panning (higher = snappier)
    zoom_speed: float = 2.2  # spring stiffness for zooming
    hold_time: float = 2.0  # seconds to hold framing after faces disappear
    zoom_out_when_lost: bool = True


@dataclass
class ServiceConfig:
    on_demand: bool = True  # only open the real camera while an app is watching
    linger: float = 3.0  # keep the camera open this long after the last app stops
    threads: int = 2  # OpenCV worker threads; more lowers latency but costs much more CPU


@dataclass
class Config:
    camera: CameraConfig = field(default_factory=CameraConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)

    def get(self, key: str):
        section, name = _split(key)
        return getattr(getattr(self, section), name)

    def set(self, key: str, value) -> None:
        """Set a dotted key, converting strings to the field's type."""
        section, name = _split(key)
        sect = getattr(self, section)
        current = getattr(sect, name)
        setattr(sect, name, _coerce(value, type(current), key))

    def to_dict(self) -> dict:
        return asdict(self)


def _split(key: str) -> tuple[str, str]:
    section, _, name = key.partition(".")
    sect_types = {f.name: f.default_factory for f in fields(Config)}
    if section not in sect_types or not name:
        raise KeyError(f"unknown setting {key!r}")
    if name not in {f.name for f in fields(sect_types[section]())}:
        raise KeyError(f"unknown setting {key!r}")
    return section, name


def _coerce(value, typ: type, key: str):
    if isinstance(value, typ) and not (typ is int and isinstance(value, bool)):
        return value
    if typ is bool:
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("1", "true", "yes", "on"):
                return True
            if low in ("0", "false", "no", "off"):
                return False
        raise ValueError(f"{key} must be true or false")
    if typ is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    try:
        return typ(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be {typ.__name__}") from None


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "autoframe" / "config.toml"


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/autoframe-{os.getuid()}"
    return Path(base) / "autoframe"


def load(path: Path | None = None) -> tuple[Config, list[str]]:
    """Load config; returns (config, warnings). A missing file means defaults."""
    path = path or config_path()
    cfg = Config()
    warnings: list[str] = []
    if not path.exists():
        return cfg, warnings
    with open(path, "rb") as f:
        data = tomllib.load(f)
    for section, values in data.items():
        if not isinstance(values, dict):
            warnings.append(f"ignoring top-level key {section!r}")
            continue
        for name, value in values.items():
            key = f"{section}.{name}"
            try:
                cfg.set(key, value)
            except (KeyError, ValueError) as e:
                warnings.append(f"{path}: {e}")
    return cfg, warnings


def save(cfg: Config, path: Path | None = None) -> Path:
    """Write only the settings that differ from the defaults."""
    path = path or config_path()
    defaults = Config().to_dict()
    lines = ["# autoframe configuration - see 'autoframe config --defaults' for all settings", ""]
    for section, values in cfg.to_dict().items():
        changed = {k: v for k, v in values.items() if v != defaults[section][k]}
        if not changed:
            continue
        lines.append(f"[{section}]")
        lines.extend(f"{k} = {_toml_value(v)}" for k, v in changed.items())
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines))
    tmp.replace(path)
    return path


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'
