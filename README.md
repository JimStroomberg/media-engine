# media-engine

Media Engine is a single-queue FastAPI service that accepts video uploads, probes the content with `ffprobe`, picks a quality preset, and produces an MP4 using an `ffmpeg` pipeline. The first release focuses on a portable CPU baseline (Ubuntu 24.04 container) so it can run on any host while we layer in hardware-specific backends (e.g. Rockchip RK1, NVIDIA Orin) later.

## Images
| Tag | Platform | Notes |
| --- | --- | --- |
| `jimstro/media-engine:latest` | linux/amd64, linux/arm64 | Generic CPU build (software decode + x264/x265) |
| `jimstro/media-engine:rk1-latest` | linux/arm64 | Rockchip RK1 build with ffmpeg from `ppa:jjriek/rockchip-multimedia` |

## Highlights
- **FFmpeg-first pipeline** – uses `ffmpeg` for probing, remuxing, and H.264/H.265 encoding by default; hardware integrations can be added behind the same interface.
- **Smart defaults** – omit quality/codec to let the service choose the closest preset (2160p/1080p/720p/480p) based on the source.
- **Single active job** – an asyncio worker guarantees only one transcode at a time; additional requests queue automatically.
- **Status + callbacks** – poll the REST API for job progress or supply a webhook to receive completion notifications.
- **Boot self-test** – confirms `ffmpeg`/`ffprobe` are available and performs a tiny encode before the API starts serving traffic.
- **Hardware-ready** – optional checks detect Rockchip RKMPP support and warn when the hardware ffmpeg build is missing.

## API surface
| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/healthz` | GET | Basic process heartbeat |
| `/jobs` | POST (multipart) | Upload a video and enqueue a transcode job |
| `/jobs` | GET | List known jobs (in-memory) |
| `/jobs/{id}` | GET | Inspect a single job |
| `/jobs/{id}/download` | GET | Retrieve the resulting MP4 once finished |
| `/jobs/{id}` | DELETE | Attempt to cancel a queued/processing job |

### Submit a job
```bash
curl -X POST \
  -F "file=@sample.mp4" \
  -F "quality=auto" \
  -F "codec=auto" \
  -F "callback_url=https://example.com/webhook" \
  http://localhost:8080/jobs
```
Fields:
- `quality` – `auto` (default), `uhd_2160p`, `fhd_1080p`, `hd_720p`, or `sd_480p`.
- `codec` – `auto` (default), `h264`, or `h265`.
- `callback_url` – optional HTTPS endpoint receiving `{ job_id, status, output_path, message }`.

### Poll job status
```bash
curl http://localhost:8080/jobs/<job-id>
```
When `status` becomes `completed`, download the file:
```bash
curl -L -o output.mp4 http://localhost:8080/jobs/<job-id>/download
```

## Startup self-test
With `MEDIA_ENGINE_SELF_TEST_ON_STARTUP=true` (default) the container will:
1. Ensure `ffmpeg` and `ffprobe` exist in `$PATH`.
2. Run a miniature test pattern encode with `ffmpeg` to verify the toolchain.

Any failure aborts startup so your orchestrator (Docker, Kubernetes, etc.) can restart the container.

## Configuration
Environment variables (prefixed with `MEDIA_ENGINE_`):

| Variable | Default | Description |
| --- | --- | --- |
| `MEDIA_ENGINE_APP_NAME` | `media-engine` | Logical application name |
| `MEDIA_ENGINE_INPUT_DIR` | `/data/input` | Upload storage (persist between runs via volume) |
| `MEDIA_ENGINE_WORK_DIR` | `/data/work` | Scratch space for active jobs |
| `MEDIA_ENGINE_OUTPUT_DIR` | `/data/output` | Completed artifacts |
| `MEDIA_ENGINE_SELF_TEST_ON_STARTUP` | `true` | Toggle the boot self-test |
| `MEDIA_ENGINE_MAX_QUEUE_SIZE` | `50` | Maximum queued jobs |
| `MEDIA_ENGINE_JOB_RETENTION_MINUTES` | `120` | Minutes to keep completed job metadata and files |
| `MEDIA_ENGINE_CALLBACK_TIMEOUT_SECONDS` | `10` | Timeout per webhook attempt |
| `MEDIA_ENGINE_CALLBACK_MAX_ATTEMPTS` | `3` | Delivery retries |
| `MEDIA_ENGINE_REQUIRE_RKMPP` | `false` | Fail startup when RKMPP hardware acceleration is expected but missing |

Quality profiles live in `app/transcode/profiles.py`; adjust widths/bitrates or add new presets as needed.

## Running locally (generic host)
1. Install Docker Engine 24+.
2. Clone this repository.
3. Build or pull the generic image (`jimstro/media-engine:latest`), then run:
   ```bash
   docker compose -f docker-compose.cpu.yml up -d
   ```
   _or_ run directly:
   ```bash
   docker build -t media-engine:dev .
   docker run --rm -it \
     -p 8080:8080 \
     -v $(pwd)/data:/data \
     media-engine:dev
   ```
4. Exercise the API using the curl examples above (or the smoke-test script below).

> **Tip:** The container stores uploads and outputs under `/data`. Mount this path to persistent storage and monitor disk usage.

## Rockchip RK1 acceleration (optional)
### RK1 performance notes
A 10s sample (3840x2160 AV1) produced the following timings on an RK1 module:

| Pipeline | Command highlights | Encode time | Video bitrate | Output size |
| --- | --- | --- | --- | --- |
| CPU (`libx264`) | software decode + `libx264 -preset veryfast` | ~20.3 s | ~12.5 Mbps | ~16 MB |
| RKMPP encode | cpu decode → `hevc_rkmpp` | ~3.9 s | ~7.0 Mbps | ~8.5 MB |
| RKMPP encode (HW decode) | `-hwaccel rkmpp -c:v av1_rkmpp` → `hevc_rkmpp` | ~3.3 s | ~7.7 Mbps | ~9.4 MB |
| RKMPP encode (HW decode, low bitrate) | same as above with `-b:v 5M` | ~3.5 s | ~4.8 Mbps | ~6.0 MB |

The default quality profile now targets H.265 at 8 Mbps for 4K content; adjust `MEDIA_ENGINE_AV1_THREADS` is no longer needed. Tune bitrates via `MEDIA_ENGINE_FFMPEG_COMMAND` overrides or custom profiles if you want smaller files.

If you deploy on an RK1 (RK3588) host and want hardware decode/encode, install the Rockchip multimedia ffmpeg build inside the container and enable the RKMPP startup guard.

1. **Expose devices and libraries** – use `docker-compose.rockchip.yml` (ships with the repo) or mirror its device/volume mounts on your orchestrator.
2. **Install the RK multimedia ffmpeg** – the `rk1-latest` image already includes the Rockchip multimedia ffmpeg build. If you derive your own image, run `./scripts/install_ffmpeg_rk1.sh` inside the container to install it manually.
3. **Enable the startup guard** – set `MEDIA_ENGINE_REQUIRE_RKMPP=true` (and optionally point `MEDIA_ENGINE_FFMPEG_COMMAND` to the hardware-enabled binary). The self-test will fail if RKMPP decoders are missing so you catch misconfiguration early.

The default image continues to work on generic CPUs without this setup; the guard only triggers when you opt in via the environment variable.

## Smoke test script
Run `scripts/test_transcode.sh` to submit a file against a running instance and watch it progress through the queue. Minimum example:
```bash
./scripts/test_transcode.sh \
  --input sample.webm \
  --media-engine http://localhost:8080 \
  --download
```
Adjust `--quality`, `--codec`, or `--poll-interval` to experiment with different profiles. The script polls `/jobs/{id}` until the job settles and optionally downloads the finished artifact into the current directory.

## Docker Compose examples
- **Generic CPU**: `docker-compose.cpu.yml` – simple volume/port mapping.
- **Rockchip RK1**: `docker-compose.rockchip.yml` – includes device pass-through and enables RKMPP checks.

Run e.g.
```bash
docker compose -f docker-compose.cpu.yml up -d
```

## Building + pushing images
Use the helper in `scripts/dockerbuild.sh` to publish both the generic and RK1 variants.
```bash
./scripts/dockerbuild.sh v0.1.0
```
Environment knobs:
- `IMAGE_REPO` – Docker repository name.
- `BASE_PLATFORMS` – Comma-separated platforms for the generic image (default `linux/amd64,linux/arm64`).
- `RK_PLATFORMS` – Platforms for the RK1 image (default `linux/arm64`).
- `PUSH` – Set to `false` to load images locally instead of pushing.

## Roadmap notes
- Add modular backends (Rockchip RK1 via GStreamer/MPP, NVIDIA Orin via NVENC/NVDEC) behind the current ffmpeg orchestration layer.
- Persist job state across restarts (SQLite or Redis) once multi-instance deployments are in scope.
- Expose Prometheus-compatible metrics and structured logs.
