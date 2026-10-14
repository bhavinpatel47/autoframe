"""Command line interface."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from . import config as configmod
from . import control, v4l2


def _pretty_card(card: str) -> str:
    # UVC cards often look like "Name: Name trunc" (32-char kernel limit).
    head, sep, _ = card.partition(": ")
    return head if sep else card


def _load(args) -> tuple[configmod.Config, Path]:
    path = Path(args.config) if args.config else configmod.config_path()
    cfg, warnings = configmod.load(path)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return cfg, path


def _notify_service(path: Path) -> None:
    """Ask a running service to reload; quiet if it is not running."""
    try:
        reply = control.request({"cmd": "reload"})
    except OSError:
        print("(service not running; the change applies when it starts)")
        return
    for w in reply.get("warnings", []):
        print(f"warning: {w}", file=sys.stderr)
    if reply.get("restart_required"):
        print("restart the service to apply output/service changes: systemctl --user restart autoframe")
    else:
        print("applied to the running service")


# --- commands ----------------------------------------------------------------------


def cmd_run(args) -> int:
    from .service import Service

    cfg, path = _load(args)
    if args.camera:
        cfg.camera.device = args.camera
    if args.debug:
        cfg.service.on_demand = False  # keep the camera open so the window always updates
        # opencv-python's bundled Qt ships no fonts; point it at the system ones.
        if Path("/usr/share/fonts").is_dir():
            os.environ.setdefault("QT_QPA_FONTDIR", "/usr/share/fonts")
    _set_threads(cfg)
    try:
        Service(cfg, path, debug_window=args.debug).run()
    except (v4l2.DeviceNotFound, control.AlreadyRunning, OSError) as e:
        logging.getLogger("autoframe").error("%s", e)
        return 1
    return 0


def cmd_cameras(args) -> int:
    cfg, _ = _load(args)
    cams = v4l2.list_cameras()
    if args.json:
        print(json.dumps([_cam_dict(i, c) for i, c in enumerate(cams, 1)], indent=2))
        return 0
    if not cams:
        print("no cameras found")
        return 1
    try:
        selected = v4l2.resolve_camera(cfg.camera.device, cams).path
    except v4l2.DeviceNotFound:
        selected = None
    for i, c in enumerate(cams, 1):
        mark = "*" if c.path == selected else " "
        best = c.best_mode()
        mode = f"{best.width}x{best.height} {best.fourcc} @{best.max_fps:g}fps" if best else "no modes"
        print(f"{mark} {i}  {_pretty_card(c.card):<28} {c.path:<12} best: {mode}")
        print(f"     id: {c.stable_id}")
        if args.verbose:
            for m in c.modes:
                print(f"       {m.fourcc} {m.width}x{m.height} @{m.max_fps:g}fps")
    print(f"\n* = selected (camera.device = {cfg.camera.device!r}). Change with: autoframe use <number|id|name>")
    return 0


def _cam_dict(i: int, c: v4l2.VideoDevice) -> dict:
    return {
        "number": i,
        "name": _pretty_card(c.card),
        "path": c.path,
        "id": c.stable_id,
        "modes": [m.__dict__ for m in c.modes],
    }


def cmd_use(args) -> int:
    cfg, path = _load(args)
    cams = v4l2.list_cameras()
    try:
        cam = v4l2.resolve_camera(args.camera, cams)
    except v4l2.DeviceNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    cfg.camera.device = "auto" if args.camera == "auto" else cam.stable_id
    # Pick the camera's best mode unless the user asked to keep the current one.
    best = cam.best_mode()
    if best and not args.keep_mode:
        cfg.camera.width, cfg.camera.height = best.width, best.height
        cfg.camera.fps = min(int(best.max_fps), 30) or 30
        cfg.camera.format = best.fourcc if best.fourcc in ("MJPG", "YUYV") else "MJPG"
    configmod.save(cfg, path)
    c = cfg.camera
    print(f"camera: {_pretty_card(cam.card)} ({cam.path}), capture {c.width}x{c.height} {c.format} @{c.fps}fps")
    _notify_service(path)
    return 0


def cmd_status(args) -> int:
    try:
        st = control.request({"cmd": "status"})
    except OSError:
        print("autoframe service is not running (start it: systemctl --user start autoframe)")
        return 1
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    cam, out, trk = st["camera"], st["output"], st["tracking"]
    print(f"output:    {out['device']}  {out['size'][0]}x{out['size'][1]}  {out['fps']} fps")
    print(f"streaming: {'yes' if st['streaming'] else 'no'}" + ("" if st["usage_events"] else " (consumer detection unavailable)"))
    name = _pretty_card(cam["name"]) if cam["name"] else cam["selector"]
    size = "x".join(map(str, cam["size"])) if cam["size"] else "-"
    print(f"camera:    {name}  {cam['device'] or ''}  {cam['state']}  {size}  {cam['fps']} fps")
    if cam["error"]:
        print(f"           {cam['error']}")
    print(f"tracking:  {'on' if trk['enabled'] else 'off'}  faces={trk['faces']}  zoom={trk['zoom']}x")
    if st["timing_ms"]:
        print("timing ms: " + "  ".join(f"{k}={v}" for k, v in sorted(st["timing_ms"].items())))
    return 0


def cmd_set(args) -> int:
    cfg, path = _load(args)
    try:
        cfg.set(args.key, args.value)
    except (KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    configmod.save(cfg, path)
    print(f"{args.key} = {cfg.get(args.key)!r}")
    _notify_service(path)
    return 0


def cmd_get(args) -> int:
    cfg, _ = _load(args)
    try:
        print(cfg.get(args.key))
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_reset(args) -> int:
    cfg, path = _load(args)
    try:
        cfg.set(args.key, configmod.Config().get(args.key))
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    configmod.save(cfg, path)
    print(f"{args.key} = {cfg.get(args.key)!r} (default)")
    _notify_service(path)
    return 0


def cmd_config(args) -> int:
    cfg, path = _load(args)
    if args.defaults:
        cfg = configmod.Config()
    print(f"# {path}{'' if path.exists() else ' (not created yet; using defaults)'}")
    for section, values in cfg.to_dict().items():
        print(f"\n[{section}]")
        for k, v in values.items():
            print(f"{k} = {configmod._toml_value(v)}")
    return 0


def cmd_tracking(args) -> int:
    args.key, args.value = "tracking.enabled", args.state
    return cmd_set(args)


def cmd_bench(args) -> int:
    import cv2

    from .camera import Camera
    from .pipeline import Pipeline

    cfg, _ = _load(args)
    if args.camera:
        cfg.camera.device = args.camera
    if args.threads is not None:
        cfg.service.threads = args.threads
    _set_threads(cfg)
    try:
        dev = v4l2.resolve_camera(cfg.camera.device)
    except v4l2.DeviceNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    c = cfg.camera
    if args.detect_every_frame:
        cfg.tracking.detect_fps = 1000.0
    print(
        f"camera {dev.path} ({_pretty_card(dev.card)}) requested {c.width}x{c.height} {c.format} @{c.fps}",
        file=sys.stderr if args.json else sys.stdout,
    )
    cam = Camera(dev.path, c.width, c.height, c.fps, c.format).start()
    pipe = Pipeline(cfg)
    pipe.timings.samples = {}
    seq, n, t_start = 0, 0, None
    try:
        deadline = time.monotonic() + args.seconds + 2
        while time.monotonic() < deadline:
            item = cam.wait_frame(seq, timeout=2.0)
            if item is None:
                print(f"error: no frames ({cam.error or cam.state})", file=sys.stderr)
                return 1
            seq, _, frame = item
            if t_start is None:  # skip warm-up frames
                if seq < 15:
                    continue
                t_start = time.monotonic()
                cpu_start = time.process_time()
                deadline = t_start + args.seconds
                pipe.timings.samples = {}
            t0 = time.perf_counter()
            pipe.process(frame)
            pipe.timings.add("pipeline", time.perf_counter() - t0)
            n += 1
    finally:
        cam.stop()
    cpu = time.process_time() - cpu_start if t_start else 0.0
    elapsed = time.monotonic() - (t_start or time.monotonic())
    result = {
        "version": __version__,
        "impl": "python",
        "opencv": cv2.__version__,
        "camera": _pretty_card(dev.card),
        "capture": list(cam.size or ()),
        "output": [cfg.output.width, cfg.output.height],
        "detect_width": cfg.tracking.detect_width,
        "detect_fps": cfg.tracking.detect_fps,
        "threads": cfg.service.threads,
        "frames": n,
        "fps": round(n / elapsed, 1) if elapsed else 0,
        "cpu_percent": round(100 * cpu / elapsed) if elapsed else 0,  # whole process, 100 = one core
        "camera_read_ms": round(cam.read_ms, 2),
        "stages_ms": {},
    }
    for stage, vals in sorted((pipe.timings.samples or {}).items()):
        vals = sorted(vals)
        result["stages_ms"][stage] = {
            "mean": round(statistics.fmean(vals), 2),
            "p95": round(vals[int(0.95 * (len(vals) - 1))], 2),
            "n": len(vals),
        }
    try:
        load = os.getloadavg()[0]
        result["loadavg_1m"] = round(load, 2)
    except OSError:
        pass
    if args.json:
        print(json.dumps(result))
    else:
        print(f"{n} frames in {elapsed:.1f}s = {result['fps']} fps  (capture {result['capture']}, output {result['output']})")
        print(f"  CPU: {result['cpu_percent']}% of one core (whole process, including JPEG decode)")
        for stage, s in result["stages_ms"].items():
            print(f"  {stage:<9} mean {s['mean']:6.2f} ms   p95 {s['p95']:6.2f} ms   n={s['n']}")
    return 0


def cmd_doctor(args) -> int:
    cfg, path = _load(args)
    problems = 0

    def report(ok: bool, msg: str, hint: str = "") -> None:
        nonlocal problems
        print(f"[{'ok' if ok else '!!'}] {msg}")
        if not ok:
            problems += 1
            if hint:
                for line in hint.splitlines():
                    print(f"     {line}")

    print(f"autoframe {__version__}, Python {sys.version.split()[0]}")
    try:
        import cv2

        report(hasattr(cv2, "FaceDetectorYN"), f"OpenCV {cv2.__version__} with YuNet face detector")
    except ImportError:
        report(False, "OpenCV is not installed", "pip install opencv-python")
    from .detector import model_path

    report(Path(model_path()).exists(), f"face model {model_path()}")

    loaded = Path("/sys/module/v4l2loopback").exists()
    available = loaded or subprocess.run(["modinfo", "v4l2loopback"], capture_output=True).returncode == 0
    report(
        available,
        "v4l2loopback kernel module is installed",
        "Debian/Ubuntu: sudo apt install v4l2loopback-dkms\n"
        "Fedora (RPM Fusion): sudo dnf install akmod-v4l2loopback\n"
        "Arch: sudo pacman -S v4l2loopback-dkms\n"
        "openSUSE: sudo zypper install v4l2loopback-kmp-default",
    )
    report(loaded, "v4l2loopback module is loaded", "run scripts/install.sh, or see README 'Manual setup'")
    try:
        out = v4l2.find_loopback(cfg.output.device, cfg.output.label)
        report(True, f"output device {out.path} ({out.card})")
        try:
            os.close(os.open(out.path, os.O_RDWR))
            report(True, f"{out.path} is writable")
        except OSError as e:
            report(False, f"cannot open {out.path}: {e.strerror}", "add yourself to the 'video' group and log in again")
    except v4l2.DeviceNotFound as e:
        report(False, str(e))
    exclusive = Path("/sys/module/v4l2loopback/parameters/exclusive_caps")
    if exclusive.exists():
        val = exclusive.read_text().strip()
        report(
            "Y" in val,
            f"exclusive_caps = {val}",
            "Chrome/Chromium-based apps need exclusive_caps=1 to list the virtual camera",
        )

    cams = v4l2.list_cameras()
    report(bool(cams), f"{len(cams)} camera(s) found")
    if cams:
        try:
            cam = v4l2.resolve_camera(cfg.camera.device, cams)
            report(True, f"selected camera: {_pretty_card(cam.card)} ({cam.path})")
        except v4l2.DeviceNotFound as e:
            report(False, f"selected camera {cfg.camera.device!r}: {e}", "pick one with: autoframe use <number>")

    if shutil.which("systemctl"):
        state = subprocess.run(["systemctl", "--user", "is-active", "autoframe"], capture_output=True, text=True).stdout.strip()
        report(state == "active", f"systemd user service: {state or 'unknown'}", "systemctl --user enable --now autoframe")
    try:
        st = control.request({"cmd": "status"})
        report(True, f"service responds on {control.socket_path()} (camera {st['camera']['state']})")
    except OSError:
        report(False, "service is not responding on its control socket")
    print(f"\nconfig: {path}{'' if path.exists() else ' (defaults)'}")
    return 1 if problems else 0


# --- entry point ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="autoframe", description="Face-tracking auto-framing virtual webcam for Linux.")
    p.add_argument("--version", action="version", version=f"autoframe {__version__}")
    p.add_argument("--config", help="config file (default: ~/.config/autoframe/config.toml)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    s = sub.add_parser("run", help="run the service in the foreground")
    s.add_argument("--camera", help="override camera.device for this run")
    s.add_argument("--debug", action="store_true", help="show a window with faces and the crop box")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("cameras", help="list cameras")
    s.add_argument("--json", action="store_true")
    s.add_argument("-a", "--all-modes", dest="verbose", action="store_true", help="show every capture mode")
    s.set_defaults(func=cmd_cameras)

    s = sub.add_parser("use", help="choose the camera to process (applies live)")
    s.add_argument("camera", help="number from 'autoframe cameras', id, /dev path, part of the name, or 'auto'")
    s.add_argument("--keep-mode", action="store_true", help="keep the configured capture resolution/fps")
    s.set_defaults(func=cmd_use)

    s = sub.add_parser("status", help="show what the running service is doing")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("set", help="change a setting, e.g. 'autoframe set tracking.max_zoom 1.6'")
    s.add_argument("key")
    s.add_argument("value")
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("get", help="print a setting")
    s.add_argument("key")
    s.set_defaults(func=cmd_get)

    s = sub.add_parser("reset", help="return a setting to its default")
    s.add_argument("key")
    s.set_defaults(func=cmd_reset)

    s = sub.add_parser("config", help="print the effective configuration")
    s.add_argument("--defaults", action="store_true", help="print the built-in defaults")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("tracking", help="turn face tracking on or off")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(func=cmd_tracking)

    s = sub.add_parser("bench", help="measure per-stage processing time on a camera")
    s.add_argument("--camera", help="camera to benchmark (default: configured camera)")
    s.add_argument("--seconds", type=float, default=10.0)
    s.add_argument("--threads", type=int, help="override service.threads")
    s.add_argument("--detect-every-frame", action="store_true", help="run detection on every frame")
    s.add_argument("--json", action="store_true", help="one-line JSON result (for docs/DEVLOG.md)")
    s.set_defaults(func=cmd_bench)

    s = sub.add_parser("doctor", help="check the setup and explain problems")
    s.set_defaults(func=cmd_doctor)

    args = p.parse_args(argv)
    under_systemd = "JOURNAL_STREAM" in os.environ
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s" if under_systemd else "%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _quiet_opencv()
    return args.func(args)


def _set_threads(cfg: configmod.Config) -> None:
    import cv2

    cv2.setNumThreads(max(0, cfg.service.threads))


def _quiet_opencv() -> None:
    os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
    try:
        import cv2

        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except Exception:
        pass
