# Development log

A running record of decisions, measurements and problems, kept so that a faster
implementation (Go, Rust or C++) can start from what was learned here rather than from
scratch. Newest entries first. Benchmarks come from `autoframe bench --json` unless stated
otherwise; see [DESIGN.md](DESIGN.md) section 8 for the format.

## Reference machine

| | |
|---|---|
| CPU | AMD Ryzen 5 3550H (4 cores / 8 threads), laptop |
| GPU | NVIDIA GTX 1650 (unused) + Radeon Vega 8 iGPU (unused) |
| OS | Ubuntu 24.04.5, kernel 7.0.0-38-generic |
| v4l2loopback | 0.15.3 (Ubuntu signed module `linux-main-modules-v4l2loopback-*`) |
| Python / OpenCV / NumPy | 3.12.3 / opencv-python 5.0.0 / 2.5.4 |
| Camera A | Kreo Owl Camera (Sonix 0c45:636d): MJPEG up to 1920x1080 @ 60 fps |
| Camera B | Built-in Azurewave USB2.0 HD UVC WebCam (13d3:56a2): MJPEG up to 1280x720 @ 30 fps |

---

## 2026-10-14: Python MVP (v0.1.0)

### What was built

- Service: real camera -> YuNet face detection -> framing/smoothing -> crop/scale -> I420 ->
  v4l2loopback, run as a systemd user service.
- On-demand camera using v4l2loopback's client-usage event. No `/proc` scanning.
- Live camera switching (`autoframe use`), stored by stable `/dev/v4l/by-id` name.
- CLI: `run [--debug]`, `cameras`, `use`, `status`, `set/get/reset`, `config`, `tracking`,
  `bench`, `doctor`.
- `scripts/install.sh` / `uninstall.sh` with `--dry-run`. They configure the module via
  `/etc/modprobe.d` and `/etc/modules-load.d` and install the user unit.
- 17 unit tests: framing behaviour, config round trip, V4L2 struct sizes and ioctl numbers.

### Decisions

| Decision | Why | Alternatives considered |
|---|---|---|
| Digital crop, not PTZ | Neither test camera has motors; this is how Center Stage works too | UVC pan/tilt controls (future, for PTZ cameras) |
| YuNet via OpenCV `FaceDetectorYN` | 230 KB, MIT, ~5 ms at 320x180 on one core, finds multiple faces, no GPU | MediaPipe (heavier dependency), Haar cascades (jittery, many false positives) |
| Own V4L2 ioctls via ctypes | Needed for usage events and device discovery; no extra dependency | pyvirtualcam (no usage events), v4l2-python |
| I420 output | OpenCV converts BGR to I420 in one call (0.5-1 ms); Chrome, Firefox and ffmpeg accept it | YUYV (needs a manual pack step in Python) |
| Usage event instead of `/proc/*/fd` scan | Event-driven and free. A `/proc` scan costs tens of ms and only sees same-user processes | inotify on the device + `/proc` scan (kept as an idea for old v4l2loopback) |
| Placeholder frames while idle | With `exclusive_caps=1` the device only lists as a camera after a producer writes | Keep the real camera always on (camera light, CPU) |
| Store camera by by-id name | `/dev/videoN` numbers change with plug order | by-path (changes when moving USB port), VID:PID (ambiguous for identical cameras) |
| Loopback found by label, not number | Fixed `video_nr` collides on systems with many video nodes | `video_nr=10` |
| `min_zoom` default 1.25 | At desk distance a face already fills a 1080p frame, so "head and shoulders" was the full frame: no pan room and "nothing happens". Cropping to >= 720 rows of a 1080p source costs no sharpness for a 720p output | Default 1.0 (looked broken in the first test) |
| CLI writes config, service only reads | One writer, no races; edits survive restarts | Service-side `set` command |

### Benchmarks

Camera A, 1920x1080 MJPEG @ 30 -> 1280x720, detect 320 px @ 15 Hz, unless noted.

**Micro-benchmarks** (one OpenCV thread, one 1080p frame, 60 iterations):

| Operation | ms |
|---|---|
| JPEG decode 1080p (`cv2.imdecode`) | **14.56** |
| JPEG decode 1080p at 1/4 scale (`IMREAD_REDUCED_COLOR_4`) | 3.83 |
| Render: `warpAffine` bilinear (sub-pixel crop) | 7.85 |
| Render: integer crop + `resize` bilinear | **2.01** |
| Render: integer crop + `resize` area | 9.43 |
| Render: `getRectSubPix` + `resize` bilinear | 5.96 |
| Full 1920 -> 1280, area / bilinear | 9.64 / 2.29 |
| BGR -> I420 at 720p | 0.84 |
| Detector downscale 1920 -> 320: area / bilinear | 2.44 / 0.21 |
| YuNet at 320x180 | 5.37 |

**Whole pipeline: OpenCV threads vs CPU** (bench, 8 s; `cpu_percent` 100 = one core). These
runs were before the render and downscale changes below:

| `cv2.setNumThreads` | CPU % | pipeline mean / p95 ms | render ms | detect ms |
|---|---|---|---|---|
| default (8) | 199 | 8.3 / 19.0 | 3.7 | 10.9 |
| 0 | 99 | 16.7 / 36.7 | 10.8 | 12.7 |
| 1 | 98 | 17.0 / 37.1 | 10.9 | 12.9 |
| 2 | 128 | 14.1 / 30.0 | 8.6 | 11.6 |
| 4 | 157 | 10.2 / 21.0 | 5.5 | 10.8 |

After switching render to integer crop + bilinear and the detector to a two-step downscale:

| threads | fps | CPU % | pipeline mean / p95 ms | render | detect | convert |
|---|---|---|---|---|---|---|
| 1 | 29.8 | 87 | 12.6 / 25.3 | 4.9 | 14.4 | 2.1 |
| 2 (default) | 29.8 | 92 | 6.9 / 15.1 | 2.6 | 8.5 | 1.0 |

The running service, measured with `top` over 5 s while ffmpeg read the virtual camera:
**91% of one core**, output 30 fps. Status timings: render 2.6, detect 9.2, convert 0.95,
write 0.26, pipeline 6.3 ms.

Where the CPU goes at threads=2, roughly: MJPEG decode about 45% of a core (14.5 ms x 30 fps,
on the capture thread); detection about 15%; render, convert and write about 12%; the rest is
Python and thread overhead.

**Camera B** (720p, dim room): only 5-7.5 fps. The camera's auto exposure lengthens exposure
in low light; this is not autoframe. The pipeline itself took 14 ms per frame.

### Problems hit

1. **15 fps instead of 30.** `CAP_PROP_BUFFERSIZE=1` made the driver miss every other frame.
   Fix: leave the default buffer count; the reader thread already drops stale frames.
2. **200% CPU.** OpenCV's default thread pool uses all 8 threads and busy-waits. Fix:
   `service.threads = 2`.
3. **Zoom stuck at 1.0x.** The user sits close to a wide 1080p camera. Fix: `min_zoom`.
4. **`--json` output was not valid JSON.** A header line went to stdout. It now goes to
   stderr.
5. **Qt font warnings** from opencv-python's bundled Qt in `--debug`. Fixed by setting
   `QT_QPA_FONTDIR=/usr/share/fonts`.
6. **ffmpeg "non monotonically increasing dts"** when reading the virtual camera. The Kreo
   delivers about 32 fps when asked for 30, so frames occasionally share a 1/30 s tick.
   Harmless for real apps. A port could pace output to `output.fps` exactly.

### Verified behaviour of v4l2loopback 0.15.3

- The client-usage event (`0x10E00001`) works with `SEND_INITIAL`. It fires on stream start
  and stop, including when a consumer closes without `STREAMOFF`. It does **not** fire when a
  program merely opens the device and runs `QUERYCAP` (tested 5 times). Device probing by
  apps therefore does not wake the camera.
- With `exclusive_caps=1`, the device reports `device_caps = 0x05200001` (capture only) once
  autoframe has written a frame.
- **Only one consumer can stream at a time.** A second ffmpeg reader gets `EBUSY`. This
  matters if you want, for example, Zoom and an OBS preview simultaneously.

### Ideas for a faster implementation

Ordered by expected gain on the reference machine:

1. **Skip the BGR round trip.** MJPEG already decodes to YUV 4:2:0. Decode straight to
   planar YUV (libjpeg-turbo `tjDecompressToYUVPlanes`), crop and scale the planes
   (libyuv `I420Scale`, SIMD), and write I420. This removes the YUV->BGR in the decoder and the
   BGR->I420 at the end, and it scales 1.5 bytes per pixel instead of 3.
2. **Faster or offloaded MJPEG decode.** The pip OpenCV decode (14.5 ms) is slow for 1080p.
   Options: system libjpeg-turbo with SIMD (typically 4-6 ms), VA-API hardware JPEG decode on
   the AMD/Intel iGPU, or NVDEC. Decode is the largest single cost.
3. **Detect on a cheap low-resolution decode.** libjpeg-turbo can decode at 1/4 or 1/8 scale
   (3.8 ms at 1/4 here) using DCT scaling. A 1/4-scale decode (480x270) is a ready-made
   detector input and needs no resize.
4. **Detection off the frame path.** Run the detector on its own goroutine or thread with the
   newest frame. The frame loop then never waits on detection, which keeps p95 latency close
   to render time.
5. **Pace the output** to exactly `output.fps` with a ticker, decoupled from camera timing.
6. **Zero-copy write.** Use mmap streaming on the loopback output instead of `write()`
   (minor; write costs 0.26 ms).

Libraries for a Go port: `github.com/vladimirvivien/go4vl` (V4L2 capture), or raw ioctls via
`golang.org/x/sys/unix` (DESIGN.md section 6 has every struct size and number); libjpeg-turbo
and libyuv via cgo; YuNet through `github.com/yalue/onnxruntime_go` or GoCV's
`FaceDetectorYN`.

### Open items / next steps

- Real-world tuning of the defaults (`face_scale`, `dead_zone`, `pan_speed`) by moving around
  on an actual call.
- Tray icon or desktop toggle for tracking on/off.
- Support several simultaneous consumers (two loopback devices, or a PipeWire video source).
- PTZ mode: drive UVC `pan_absolute`/`tilt_absolute`/`zoom_absolute` on cameras that have
  motors, using the same framing logic.
- Fallback consumer detection for v4l2loopback older than 0.12.6.
- Packaging: AUR / .deb / Flatpak-free install. PyPI upload (check the `autoframe` name is
  free first).
