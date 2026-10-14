# autoframe

Face-tracking auto-framing for any webcam on Linux, in the spirit of Apple's Center Stage.

autoframe reads your real webcam, finds the faces in each frame, and smoothly pans and zooms
a crop window to keep you framed. The result is published as a virtual camera called
**"Autoframe Camera"** that works in Zoom, Google Meet, Teams, Discord, OBS, Firefox, Chrome
and anything else that uses V4L2.

- **Smooth, calm framing.** A dead zone ignores small head movements, new framings must
  persist briefly before the camera moves, and pans and zooms ease in and out.
- **Groups.** Everyone in view is kept in the frame. Small background faces are ignored.
- **On demand.** The real camera is only switched on while an app is actually watching the
  virtual camera, so the camera light stays off otherwise.
- **Any camera, switchable live.** `autoframe use 2` changes camera without restarting your
  call. Cameras are remembered by their USB id, so the choice survives reboots.
- **Lightweight.** About 90% of one CPU core at 1080p30 in the Python version (see
  [docs/DEVLOG.md](docs/DEVLOG.md)), with a pure-CPU face detector. No GPU needed.

## How it works

```
real camera (e.g. 1920x1080 MJPEG)
   -> face detection on a small copy (YuNet, ~15 times a second)
   -> framing: target crop around the faces, dead zone, spring smoothing
   -> crop + scale to 1280x720
   -> v4l2loopback device "Autoframe Camera"  ->  your video app
```

autoframe moves a crop window inside the camera image; it does not move the camera. The
amount it can pan and zoom depends on how much wider and sharper your camera is than the
output. A 1080p camera feeding a 720p output pans and zooms with no loss of sharpness up to
1.5x. A 720p camera works too, but zooming in makes the picture softer.

## Requirements

- Linux with the **v4l2loopback** kernel module:

  | Distro | Package |
  |---|---|
  | Debian / Ubuntu | `sudo apt install v4l2loopback-dkms` (Ubuntu's own kernels may already ship it) |
  | Fedora | `sudo dnf install akmod-v4l2loopback` (RPM Fusion) |
  | Arch | `sudo pacman -S v4l2loopback-dkms` |
  | openSUSE | `sudo zypper install v4l2loopback-kmp-default` |

  With Secure Boot enabled, DKMS modules must be signed; your distro's DKMS normally walks
  you through enrolling a key.
- Python 3.10 or newer with `venv` (Debian/Ubuntu: `sudo apt install python3-venv`).
- A systemd user session (optional; without it, run `autoframe run` yourself).

## Install

```sh
git clone https://github.com/bhavinpatel47/autoframe.git
cd autoframe
scripts/install.sh --dry-run   # see what it will do
scripts/install.sh
```

The installer:

1. creates a virtualenv in `~/.local/share/autoframe/venv` and links `~/.local/bin/autoframe`;
2. uses sudo to write `/etc/modprobe.d/autoframe.conf` and `/etc/modules-load.d/autoframe.conf`
   so a v4l2loopback device labelled "Autoframe Camera" is created at boot, and loads it now;
3. installs and starts the `autoframe` systemd user service;
4. runs `autoframe doctor` to check everything.

If v4l2loopback is already configured on your system (for example for OBS), the installer
leaves that configuration alone and tells you how to point autoframe at a device.

Remove everything with `scripts/uninstall.sh` (add `--purge` to also delete your settings).

### Manual setup

```sh
sudo modprobe v4l2loopback devices=1 card_label="Autoframe Camera" exclusive_caps=1
python3 -m venv ~/.local/share/autoframe/venv
~/.local/share/autoframe/venv/bin/pip install .
~/.local/share/autoframe/venv/bin/autoframe run
```

`exclusive_caps=1` matters: Chrome and Chromium-based apps only list loopback devices that
use it.

## Usage

Pick **Autoframe Camera** in your video app. Everything else is optional.

```sh
autoframe cameras            # list cameras; * marks the one in use
autoframe use 2              # switch camera (number, name, /dev path or id); applies live
autoframe status             # what the service is doing, with per-stage timings
autoframe tracking off       # freeze to the full camera view; 'on' to resume
autoframe set tracking.max_zoom 1.6   # change any setting; applies live
autoframe reset tracking.max_zoom     # back to the default
autoframe config             # show all settings
autoframe doctor             # diagnose setup problems
autoframe bench              # measure processing speed on your camera
```

To tune the framing visually, stop the service and run it in the foreground with a debug
window that shows faces (green), the target framing (yellow) and the current crop (red):

```sh
systemctl --user stop autoframe
autoframe run --debug        # press q in the window to quit
systemctl --user start autoframe
```

Service logs: `journalctl --user -u autoframe -f`.

## Settings

Stored in `~/.config/autoframe/config.toml` (only values you change are written). Use
`autoframe set <key> <value>` so the running service picks them up.

| Key | Default | Meaning |
|---|---|---|
| `camera.device` | `auto` | Camera to process. `autoframe use` sets this to the camera's stable id. |
| `camera.width`, `camera.height`, `camera.fps`, `camera.format` | 1920, 1080, 30, MJPG | Capture mode; `autoframe use` picks the camera's best mode. |
| `output.width`, `output.height`, `output.fps` | 1280, 720, 30 | Virtual camera format (restart the service after changing). |
| `output.device` / `output.label` | `auto` / `Autoframe Camera` | Which loopback device to write to. |
| `tracking.enabled` | `true` | Face tracking on/off. |
| `tracking.face_scale` | 3.2 | Crop height as a multiple of face height for one person. Lower = tighter. |
| `tracking.min_zoom` | 1.25 | With faces in view, always crop at least this much, leaving room to pan. 1.0 allows the full view. |
| `tracking.max_zoom` | 2.0 | Never zoom in further than this. |
| `tracking.eye_line` | 0.40 | Where faces sit vertically (0 = top, 1 = bottom). |
| `tracking.margin` | 1.0 | Space around a group, in face heights. |
| `tracking.dead_zone` | 0.06 | Ignore moves smaller than this fraction of the frame. |
| `tracking.zoom_dead_zone` | 0.10 | Ignore size changes smaller than this ratio. |
| `tracking.confirm_frames` | 3 | Detections a new framing must persist for before the camera moves. |
| `tracking.pan_speed` / `tracking.zoom_speed` | 3.5 / 2.2 | Spring stiffness; higher is snappier. |
| `tracking.hold_time` | 2.0 | Seconds to hold the framing after faces disappear. |
| `tracking.zoom_out_when_lost` | `true` | Return to the full view when nobody is visible. |
| `tracking.detect_width` | 320 | Detector input width. Raise (e.g. 480) to find faces further away. |
| `tracking.detect_fps` | 15 | Face detections per second. |
| `tracking.score_threshold` | 0.7 | Detector confidence needed to count as a face. |
| `tracking.min_face_ratio` | 0.45 | Ignore faces smaller than this fraction of the largest face. |
| `service.on_demand` | `true` | Only open the real camera while an app is watching. |
| `service.linger` | 3.0 | Seconds to keep the camera open after the last app stops. |
| `service.threads` | 2 | OpenCV worker threads. |

## Troubleshooting

- **The camera does not show up in Chrome / Meet / Teams.** The loopback device needs
  `exclusive_caps=1`; `autoframe doctor` checks this.
- **"Starting camera..." or "cannot open /dev/videoN".** Another app is using the real
  camera directly. Close it, or select "Autoframe Camera" in that app instead.
- **The picture is dark and the frame rate is low.** Many webcams lower their frame rate in
  dim light (auto exposure priority). More light helps; `v4l2-ctl -d /dev/videoN -L` lists
  the camera's controls, for example `exposure_dynamic_framerate`.
- **Only one app at a time.** Recent v4l2loopback versions allow a single app to stream from
  a loopback device at once.
- **v4l2loopback is configured for OBS already.** Either add a second device labelled
  "Autoframe Camera" to the existing options line (`devices=2 card_label="OBS Virtual
  Camera","Autoframe Camera" exclusive_caps=1,1`) or run `autoframe set output.device
  /dev/videoN`.

## Development

```sh
scripts/install.sh --editable    # service runs from this checkout; restart to pick up edits
~/.local/share/autoframe/venv/bin/pytest
```

- [docs/DESIGN.md](docs/DESIGN.md): language-neutral specification of the pipeline, the
  framing algorithm, the V4L2 details and the control protocol. Use this to write a port.
- [docs/DEVLOG.md](docs/DEVLOG.md): development log, benchmarks and the optimisation list
  for a faster implementation.

## Credits and license

autoframe is MIT licensed (see [LICENSE](LICENSE)). It bundles the
[YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) face
detection model by Shiqi Yu et al., MIT licensed (see
[src/autoframe/models/LICENSE-yunet](src/autoframe/models/LICENSE-yunet)). It depends on
OpenCV and NumPy, and on the [v4l2loopback](https://github.com/umlaeute/v4l2loopback)
kernel module. Not affiliated with Apple; "Center Stage" is Apple's name for their feature.
