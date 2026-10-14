# autoframe design

This document specifies autoframe independently of the Python implementation, so that a
faster port (Go, Rust, C++) can reproduce its behaviour exactly. The Python code is the
reference implementation; file references point into `src/autoframe/`.

## 1. Components

```
                 +-------------------- service (one process) ---------------------+
 /dev/videoN --> | camera reader -> latest frame -> pipeline -> loopback writer    | --> /dev/videoM
 (real camera)   |   (thread)                     detect / frame / render / I420  |    (v4l2loopback)
                 |                                                                 |
                 | usage events from /dev/videoM -> camera on/off                 |
                 | control socket <- CLI (status, reload)                          |
                 +-----------------------------------------------------------------+
 ~/.config/autoframe/config.toml  <- written by the CLI, read by the service on start and on "reload"
```

| Python module | Responsibility |
|---|---|
| `v4l2.py` | ioctl structs, device discovery, camera selector resolution, loopback output, usage events |
| `camera.py` | capture thread; always exposes only the newest decoded frame; reconnects |
| `detector.py` | YuNet face detection on a downscaled copy |
| `framing.py` | pure framing and smoothing logic (no I/O) |
| `pipeline.py` | per-frame orchestration and timing; crop/scale; I420 conversion |
| `service.py` | main loop, on-demand camera lifecycle, placeholders, config reload |
| `control.py` | Unix socket protocol |
| `config.py` | settings schema, TOML load/save |
| `cli.py` | user commands |

## 2. Main loop

State: output device (open), camera (open or not), `last_active` time, last frame sequence.

Each iteration:

1. Apply a pending config reload, if any (see section 7).
2. Read usage events from the output device. Block for up to 1 s **only** when no camera is
   open and no consumer is streaming; otherwise poll without blocking.
3. `streaming = consumers > 0`. If streaming, `last_active = now`.
4. `want_camera = !on_demand || streaming || now - last_active < linger`.
5. If `want_camera` and no camera is open (and at least 2 s since the last attempt), resolve
   the selector and open the camera. If `!want_camera` and a camera is open, close it and
   write the idle placeholder.
6. No camera: write a placeholder frame every 5 frame periods if `want_camera` (the message
   says why there is no camera), otherwise once a second ("camera idle").
7. Camera open: wait up to 100 ms for a frame newer than the last one. On timeout, write a
   placeholder ("Starting camera..." or the camera error) at most every 100 ms. Otherwise
   run the pipeline and write the result.

Placeholder frames are needed because with `exclusive_caps=1` a loopback device only
advertises itself as a capture device after the producer has set a format and written a
frame. Writing a frame immediately on start makes "Autoframe Camera" visible to apps before
any of them has used it. Placeholders also stop consumers timing out while the real camera
starts (about 0.5 s).

## 3. Camera capture

- Open with V4L2, request `camera.format` (MJPG by default), `camera.width` x `camera.height`
  at `camera.fps`. The driver may choose a different size; use what it reports.
- Read and decode on a dedicated thread. Keep only the newest frame plus a sequence number;
  consumers wait on a condition variable for `seq > last_seq`. Never queue frames.
- **Do not** reduce the driver's buffer count to 1. With OpenCV's
  `CAP_PROP_BUFFERSIZE=1` the Kreo Owl dropped to 15 fps (every other frame missed).
- After 10 consecutive failed reads, close and reopen with exponential backoff
  (0.5 s doubling to 5 s). This handles unplug and replug.

### Camera selection

A selector string is resolved against the list of capture devices (nodes with
`V4L2_CAP_VIDEO_CAPTURE` in `device_caps`, excluding driver `"v4l2 loopback"`), sorted by
`/dev/videoN` number:

| Selector | Meaning |
|---|---|
| `auto` or empty | first camera |
| `1`, `2`, ... | position in that list (as printed by `autoframe cameras`) |
| `/dev/...` | that node, after resolving symlinks |
| exact name in `/dev/v4l/by-id/` or `/dev/v4l/by-path/` | that camera |
| any other text | case-insensitive substring of the card name; must match exactly one |

`autoframe use` stores the camera's **stable id**: its `/dev/v4l/by-id` name if it has one,
else the `/dev/v4l/by-path` name, else the `/dev/videoN` path. by-id survives reboots and
moving the camera to another port; by-path is a fallback for cameras without serials.

It also sets the capture mode to the camera's best mode: the largest frame size that
reaches at least 24 fps (MJPG preferred on ties), with fps capped at 30.

## 4. Face detection

- Model: YuNet `face_detection_yunet_2023mar.onnx` (OpenCV `FaceDetectorYN`), NMS 0.3,
  top-k 50, score threshold `tracking.score_threshold`.
- Input: the frame scaled to `tracking.detect_width` pixels wide, keeping aspect. Scaling is
  two steps: bilinear to 2x the target size, then 2x2 area averaging. This is anti-aliased
  and about 10x cheaper than one area resize from 1080p.
- Output: boxes `(x, y, w, h, score)` scaled back to source coordinates.
- Rate: at most `tracking.detect_fps` times per second (a time gate, not every Nth frame).
  Frames in between only advance the smoothing.

## 5. Framing algorithm (`framing.py`)

Notation: source `W x H`, output aspect `A = out_w / out_h`. A *view* is `(cx, cy, h)`, a
crop of width `h*A` and height `h` centred at `(cx, cy)`.

`h_max = min(H, W / A)`, the largest crop with the output aspect. The *full view* is
`(W/2, H/2, h_max)`.

### 5.1 Desired view for a set of faces

1. `fh = max face height`. Keep only faces with `h >= min_face_ratio * fh`.
2. Union box `x0, y0, x1, y1` of the kept faces; `m = margin * fh`.
3. Height:
   ```
   h = max( fh * face_scale,                       # single-person framing
            (x1 - x0 + 2m) / A,                    # group width fits
            (y1 - y0) + 2m,                        # group height fits
            ((y1 - y0)/2 + 0.3*fh) / eye_line )    # room above the top face
   lo = h_max / max(max_zoom, 1)
   hi = max(lo, h_max / max(min_zoom, 1))
   h = clamp(h, lo, hi)
   ```
4. Centre: `cx = (x0 + x1)/2`, `cy = (y0 + y1)/2 + h * (0.5 - eye_line)`, so the faces'
   centre sits `eye_line` of the way down the crop.
5. Clamp: `h` to `[1, h_max]`, then `cx` to `[h*A/2, W - h*A/2]` and `cy` to `[h/2, H - h/2]`.

### 5.2 Target update (on each detection)

State: `target` view, `pending` counter, `last_seen` time.

- Tracking disabled: `target = full view`, `pending = 0`.
- No faces: `pending = 0`; if `zoom_out_when_lost` and `now - last_seen > hold_time`,
  `target = full view`.
- Faces: `last_seen = now`; compute desired view `d`. It is *outside the dead zone* if any of
  - `|d.cx - target.cx| > dead_zone * target.h * A`
  - `|d.cy - target.cy| > dead_zone * target.h`
  - `|d.h / target.h - 1| > zoom_dead_zone`

  If outside: `pending += 1`, and once `pending >= confirm_frames`, `target = d` and
  `pending = 0`. If inside: `pending = 0`.

`confirm_frames` (3 detections, about 200 ms at 15 Hz) stops a single false detection or a
quick glance from moving the camera.

### 5.3 Smoothing (every output frame)

Each of `cx`, `cy`, `h` follows its target with a critically damped spring, stepped with
semi-implicit Euler (`dt` clamped to `[0, 0.1]` s):

```
a = w^2 * (target - x) - 2*w*v
v = v + a*dt
x = x + v*dt
```

`w = pan_speed` for `cx` and `cy`, `w = zoom_speed` for `h`. The resulting view is clamped
as in 5.1 step 5. Initial state: the full view with zero velocity. A new source resolution
creates a fresh framer.

### 5.4 Render

Crop `(x, y, w, h) = (cx - h*A/2, cy - h/2, h*A, h)`, clamped inside the frame and **rounded
to whole pixels**. Scale it to the output size with bilinear interpolation if
`out_w / crop_w >= 0.66`, otherwise with area interpolation. Convert BGR to planar I420
(YU12).

A sub-pixel affine warp was tried first and dropped: 7.9 ms against 2.0 ms per frame, with
no visible difference after scaling.

### 5.5 Defaults

See `TrackingConfig` in `config.py`; the README's settings table has the same values. Any
port should use identical defaults so configs are portable.

## 6. V4L2 details

All structs are from `linux/videodev2.h`; sizes are for 64-bit Linux (x86_64, arm64) and
are asserted in `tests/test_v4l2.py`.

| ioctl | number | struct (size) |
|---|---|---|
| `VIDIOC_QUERYCAP` | `0x80685600` | `v4l2_capability` (104) |
| `VIDIOC_ENUM_FMT` | `0xC0405602` | `v4l2_fmtdesc` (64) |
| `VIDIOC_S_FMT` | `0xC0D05605` | `v4l2_format` (208; the union is 8-byte aligned, at offset 8) |
| `VIDIOC_S_PARM` | `0xC0CC5616` | `v4l2_streamparm` (204) |
| `VIDIOC_ENUM_FRAMESIZES` | `0xC02C564A` | `v4l2_frmsizeenum` (44) |
| `VIDIOC_ENUM_FRAMEINTERVALS` | `0xC034564B` | `v4l2_frmivalenum` (52) |
| `VIDIOC_DQEVENT` | `0x80885659` | `v4l2_event` (136; union at offset 8) |
| `VIDIOC_SUBSCRIBE_EVENT` | `0x4020565A` | `v4l2_event_subscription` (32) |

Device discovery: for each `/sys/class/video4linux/video*`, open `/dev/videoN`
`O_RDWR|O_NONBLOCK`, `QUERYCAP`, use `device_caps` when `capabilities` has
`V4L2_CAP_DEVICE_CAPS` (0x80000000). Capture = bit `0x1`. Loopback = driver `"v4l2 loopback"`.
The output device is the loopback whose card name equals `output.label`, or the only
loopback device.

### Loopback output

1. `open(O_RDWR|O_NONBLOCK)`.
2. `S_FMT` with type `V4L2_BUF_TYPE_VIDEO_OUTPUT` (2): width, height, pixelformat `YU12`
   (`0x32315559`), field `NONE` (1), bytesperline = width, sizeimage = `w*h*3/2`, colorspace
   `SRGB` (8). Check the returned size and format; another producer may have fixed them.
3. `S_PARM` output `timeperframe = 1/fps` (advisory; ignore errors).
4. `SUBSCRIBE_EVENT` type `V4L2_EVENT_PRI_CLIENT_USAGE` = `0x08000000 + 0x08E00000 + 1` =
   `0x10E00001`, flags `V4L2_EVENT_SUB_FL_SEND_INITIAL` (1). If this fails (v4l2loopback older
   than 0.12.6), treat the device as always in use.
5. Write each frame with `write()`; drop it on `EAGAIN`.

### Usage events

`poll()` the output fd for `POLLPRI`, then `DQEVENT` until `pending == 0`. For type
`0x10E00001` the first `__u32` of the event union is the consumer count. In v4l2loopback
0.15 it is 1 while a consumer holds the capture stream and 0 otherwise. It is sent on
`STREAMON`/`STREAMOFF` of a capture queue and on close, which releases the stream. Simply
opening the device to list it does not generate an event, so device probing by browsers does
not switch the camera on.

## 7. Configuration and control

Config file: `$XDG_CONFIG_HOME/autoframe/config.toml` (default `~/.config/...`). Sections
`camera`, `output`, `tracking`, `service`; keys as in `config.py`. Only non-default values are
written. Unknown keys produce warnings, not errors.

The CLI is the only writer. To change a setting it writes the file, then sends `reload` to
the service. On reload the service re-reads the file and applies it at the start of the next
loop iteration:

- `tracking.*`: applied immediately (the framer reads its parameters live).
- `camera.*`: if device, size, fps or format changed, the camera is closed; the loop reopens
  it with the new settings.
- `output.*`, `service.*`: reported as `restart_required`.

Control socket: `$XDG_RUNTIME_DIR/autoframe/control.sock`, mode 0600. One connection per
request: the client sends one line of JSON and receives one line of JSON.

| Request | Reply |
|---|---|
| `{"cmd":"status"}` | `{"ok":true, "uptime_s", "streaming", "usage_events", "output":{...}, "camera":{...}, "tracking":{...}, "timing_ms":{...}}` |
| `{"cmd":"reload"}` | `{"ok":true, "warnings":[...], "restart_required":bool}` |
| anything else | `{"ok":false, "error":"..."}` |

On start, an existing socket that answers means another instance is running (exit with an
error); one that refuses connections is stale and is removed. `SIGHUP` triggers a reload;
`SIGTERM`/`SIGINT` stop the service cleanly.

## 8. Benchmark format

`autoframe bench --json` prints one line that any port should reproduce, so implementations
can be compared on the same machine and camera:

```json
{"impl": "python", "camera": "...", "capture": [1920,1080], "output": [1280,720],
 "detect_width": 320, "detect_fps": 15.0, "threads": 2, "frames": 240, "fps": 29.8,
 "cpu_percent": 92, "camera_read_ms": 33.6,
 "stages_ms": {"detect": {"mean":..,"p95":..,"n":..}, "render": {...}, "convert": {...}, "pipeline": {...}}}
```

`cpu_percent` covers the whole process over the measured window, including JPEG decoding
on the capture thread (100 = one core). The first 15 frames are discarded as warm-up.
