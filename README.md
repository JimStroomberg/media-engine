# Media Engine

Media Engine is a product-neutral backend for durable media processing. Products such as [whY-Tee-WebDL](https://github.com/JimStroomberg/whY-Tee-WebDL) submit media; Media Engine owns content identity, S3 storage, reusable processing runs, retention, and hardware-aware execution.

The repository currently exposes two surfaces:

- **Platform v2** — PostgreSQL, S3-compatible storage, exact-byte deduplication, reusable multi-stage pipeline runs, leased workers, explicit expiry, signed artifacts, and AI-ready media-understanding pipelines.
- **Temporary legacy transcoder** — the existing in-process `/jobs` queue remains only to support controlled product migration. It may change or be removed before v2 is declared stable.

The locked design and delivery sequence live in [docs/architecture-v2.md](docs/architecture-v2.md) and [ROADMAP.md](ROADMAP.md).
Release notes are in [CHANGELOG.md](CHANGELOG.md), and the pre-stable compatibility and current-version rules are defined
in [docs/development-policy.md](docs/development-policy.md).
Production HTTPS, HTTP/3/QUIC, and large-upload considerations are documented in
[docs/reverse-proxy.md](docs/reverse-proxy.md), with reusable NGINX examples under
[`deploy/nginx`](deploy/nginx).

## Build status
[![Build & Push via Docker Build Cloud](https://github.com/JimStroomberg/media-engine/actions/workflows/build.yaml/badge.svg?branch=main)](https://github.com/JimStroomberg/media-engine/actions/workflows/build.yaml)
[![Build & Push via Docker Build Cloud](https://github.com/JimStroomberg/media-engine/actions/workflows/build.yaml/badge.svg?branch=dev)](https://github.com/JimStroomberg/media-engine/actions/workflows/build.yaml)

## Images
| Tag | Platform | Notes |
| --- | --- | --- |
| `jimstro/media-engine:latest` | linux/amd64, linux/arm64 | Generic CPU build (software decode + x264/x265) |
| `jimstro/media-engine:rk1-latest` | linux/arm64 | Rockchip RK1 build with ffmpeg from `ppa:jjriek/rockchip-multimedia` |
| `jimstro/media-engine:jetson-xavier-nx-latest` | linux/arm64 | JetPack 5 Xavier NX build with Python 3.13, NVIDIA GStreamer, CUDA 11.4, and TensorRT 8.5 |

The control plane and generic worker build on Ubuntu 26.04 LTS. The default Compose stack pins PostgreSQL 18.4 and MinIO `RELEASE.2025-09-07T16-13-09Z`, the current stable community image. Hardware images deliberately follow their vendor runtime: RK1 uses Ubuntu 24.04 for its multimedia packages, while Xavier NX uses NVIDIA's JetPack 5/L4T R35 TensorRT base. These are isolated worker compatibility requirements rather than the platform baseline.

## Highlights
- **S3-only durable media** – platform media is stored by content hash in S3-compatible object storage, including single-node deployments through MinIO.
- **Duplicate detection** – repeated exact bytes reuse one retained blob, while deterministic run keys single-flight and cache equivalent processing requests.
- **Automatic expiry** – PostgreSQL records retention state and an idempotent scheduler removes expired S3 objects.
- **Compose-first control plane** – API, PostgreSQL, MinIO, migrations, scheduler, and webhook dispatcher can run together now and separate cleanly later.
- **Standalone worker nodes** – one-service CPU, RK1, and Jetson Xavier NX Compose deployments use individually revocable worker tokens and short-lived S3 transfer URLs without receiving database, administrator, or object-store credentials.
- **Tenant API keys** – client projects receive scoped, revocable API keys; assets and job requests are isolated by client while retained processing results remain reusable.
- **Managed AI providers** – OpenAI and xAI connections are encrypted in PostgreSQL, configurable through administrator endpoints, and delivered to workers only for a claimed AI stage.
- **Durable AI accounting** – every provider response records model, tokens or duration, cost, latency, stage attempt, and outcome, including responses whose later validation fails.
- **Durable worker execution** – v2 jobs are leased to capability-advertising workers with heartbeats, retries, and abandoned-lease recovery.
- **Optional durable webhooks** – client-owned endpoints receive signed terminal job events through a PostgreSQL outbox with retries and delivery history; polling remains authoritative.
- **Built-in management dashboard** – operators can inspect jobs, stages, artifacts, workers, provider usage, and webhook delivery, then manage providers, clients, API keys, and webhook endpoints without direct database access.
- **Agent-ready outputs** – `ai_prepare` produces metadata, audio, subtitles, scenes, keyframes, and OCR; `understand` adds diarized transcription, visual descriptions, and a structured agent document.
- **Hardware-aware pipeline** – uses FFmpeg on CPU/RKMPP and NVIDIA GStreamer on Jetson for NVDEC, VIC transforms,
  and NVENC H.264/H.265 while retaining the same output and worker contracts.
- **Smart defaults** – omit quality/codec to let the service choose the closest preset (2160p/1080p/720p/480p) based on the source; if you request a higher preset than the input can support, it automatically downgrades to the best fit.
- **Audio-only path** – send `quality=audio_only` to extract AAC audio without video; this always runs on the CPU and returns an `.m4a`.
- **Single active job** – an asyncio worker guarantees only one transcode at a time; additional requests queue automatically.
- **Status + webhooks** – poll v2 job status at any time and optionally select a registered signed endpoint for terminal notifications.
- **Boot self-test** – confirms the media toolchain and performs a tiny encode; hardware profiles additionally require
  and exercise their actual accelerator before registering online.
- **Hardware-ready** – workers advertise only the encoders, decoders, transforms, and accelerator devices detected at
  runtime, so the dashboard reflects what each node can really execute.

## API surface
| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/healthz` | GET | Basic process heartbeat |
| `/readyz` | GET | PostgreSQL and S3 readiness |
| `/v2/assets` | POST (multipart) | Store media by SHA-256 and create an asset record |
| `/v2/assets/{id}` | GET | Read asset identity, provenance, retention, and storage state |
| `/v2/pipelines` | GET | Discover current pipeline options, DAG stages, capabilities, and output contracts |
| `/v2/pipelines/{name}` | GET | Read one current pipeline contract |
| `/v2/jobs` | POST (JSON) | Request a server-owned pipeline with single-flight reuse |
| `/v2/jobs/{id}` | GET | Read durable request and pipeline status |
| `/v2/jobs/{id}/artifacts` | GET | List outputs and short-lived signed download URLs |
| `/v2/jobs/{id}/manifest` | GET | Read source identity, stage state, provenance, and all output contracts |
| `/v2/admin/providers` | GET/POST/PATCH/DELETE | Manage, verify, enable, and select encrypted AI provider connections |
| `/v2/admin/overview` | GET | Read operational totals, active work, usage, and webhook health |
| `/v2/admin/workers` | GET/POST | Inspect workers or create a revocable worker identity and one-time token |
| `/v2/admin/workers/{id}` | PATCH/DELETE | Rename a worker or remove an already-revoked worker from the fleet |
| `/v2/admin/workers/{id}/drain` | POST | Stop new claims while allowing active work to finish |
| `/v2/admin/workers/{id}/activate` | POST | Resume claims for a drained worker |
| `/v2/admin/workers/{id}/revoke` | POST | Revoke worker access before retirement or removal |
| `/v2/admin/workers/{id}/rotate-token` | POST | Replace and return a worker token once |
| `/v2/admin/jobs` | GET | Filter job history and inspect stages, artifacts, usage, and errors |
| `/v2/admin/clients` | GET/POST | Manage API client projects |
| `/v2/admin/clients/{id}/api-keys` | GET/POST | List or create scoped client API keys |
| `/v2/admin/api-keys/{id}` | DELETE | Revoke a client API key |
| `/v2/admin/clients/{id}/webhook-endpoints` | GET/POST | List or register client webhook destinations |
| `/v2/admin/webhook-events` | GET | Inspect durable delivery state and attempt history |
| `/v2/admin/usage` | GET | Inspect per-attempt provider usage and cost |
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
- `quality` – `auto` (default), `uhd_2160p`, `fhd_1080p`, `hd_720p`, `sd_480p`, or `audio_only`. When a higher preset is requested than the source resolution supports, the engine transparently downgrades to the best matching profile (logged in the job history).
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
Audio-only jobs will download as `.m4a`.

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
| `MEDIA_ENGINE_MAX_UPLOAD_BYTES` | unset | Optional hard upload-size limit; requests above this return HTTP 413 |
| `MEDIA_ENGINE_JOB_RETENTION_MINUTES` | `120` | Minutes to keep completed job metadata and files |
| `MEDIA_ENGINE_CALLBACK_TIMEOUT_SECONDS` | `10` | Timeout per temporary legacy `/jobs` callback attempt |
| `MEDIA_ENGINE_CALLBACK_MAX_ATTEMPTS` | `3` | Temporary legacy `/jobs` callback retries |
| `MEDIA_ENGINE_ALLOW_CPU_FALLBACK` | `true` | Permit CPU video fallback when hardware encoding fails (hardware Compose profiles set `false`) |
| `MEDIA_ENGINE_REQUIRE_RK_ACCEL` | `false` | Fail startup when RKMPP hardware acceleration is expected but missing |
| `MEDIA_ENGINE_REQUIRE_JETSON_ACCEL` | `false` | Fail startup unless the Jetson GStreamer plugins, devices, and encode/decode probe succeed |
| `MEDIA_ENGINE_FFPROBE_TIMEOUT_SECONDS` | `30` | Maximum time allowed for media probing |
| `MEDIA_ENGINE_MEDIA_COMMAND_TIMEOUT_SECONDS` | `1800` | Hard runtime limit for one transcode/media command |
| `MEDIA_ENGINE_MEDIA_NO_PROGRESS_TIMEOUT_SECONDS` | `120` | Stop a transcode that produces no progress for this many seconds |
| `MEDIA_ENGINE_DATABASE_URL` | unset | Async PostgreSQL URL; required for platform v2 |
| `MEDIA_ENGINE_S3_ENDPOINT_URL` | AWS default | Optional S3-compatible endpoint such as MinIO |
| `MEDIA_ENGINE_S3_PUBLIC_ENDPOINT_URL` | internal endpoint | Client-reachable endpoint used when signing artifact URLs |
| `MEDIA_ENGINE_S3_WORKER_ENDPOINT_URL` | public endpoint | Worker-reachable endpoint used for signed stage downloads and uploads |
| `MEDIA_ENGINE_S3_ACCESS_KEY_ID` | AWS provider chain | S3 access key for custom endpoints |
| `MEDIA_ENGINE_S3_SECRET_ACCESS_KEY` | AWS provider chain | S3 secret key for custom endpoints |
| `MEDIA_ENGINE_S3_BUCKET` | unset | Durable media bucket; required for platform v2 |
| `MEDIA_ENGINE_S3_REGION` | `us-east-1` | S3 bucket region |
| `MEDIA_ENGINE_S3_FORCE_PATH_STYLE` | `false` | Enable path-style requests for MinIO-compatible endpoints |
| `MEDIA_ENGINE_ASSET_RETENTION_HOURS` | `24` | Default lifetime of uploaded media |
| `MEDIA_ENGINE_PIPELINE_RETENTION_HOURS` | `24` | Default lifetime of reusable pipeline runs and artifacts |
| `MEDIA_ENGINE_ARTIFACT_URL_EXPIRY_SECONDS` | `900` | Lifetime of signed artifact download URLs |
| `MEDIA_ENGINE_RETENTION_INTERVAL_SECONDS` | `60` | Seconds between expiry sweeps |
| `MEDIA_ENGINE_ADMIN_USERNAME` | unset | Required bootstrap administrator username for management endpoints |
| `MEDIA_ENGINE_ADMIN_PASSWORD` | unset | Required bootstrap administrator password for management endpoints |
| `MEDIA_ENGINE_ADMIN_SESSION_SECRET` | unset | Required random signing secret for management dashboard sessions; generate at least 32 characters |
| `MEDIA_ENGINE_ADMIN_SESSION_TTL_HOURS` | `12` | Maximum lifetime of a signed dashboard login session (1–168 hours) |
| `MEDIA_ENGINE_ADMIN_SESSION_COOKIE_SECURE` | `false` | Require HTTPS when sending the dashboard session cookie; set `true` in production |
| `MEDIA_ENGINE_CREDENTIAL_ENCRYPTION_KEY` | unset | Required stable Fernet key for provider credentials stored in PostgreSQL |
| `MEDIA_ENGINE_WEBHOOK_TIMEOUT_SECONDS` | `10` | Timeout for one v2 webhook delivery attempt |
| `MEDIA_ENGINE_WEBHOOK_MAX_ATTEMPTS` | `8` | Maximum v2 webhook delivery attempts |
| `MEDIA_ENGINE_WEBHOOK_ALLOW_PRIVATE_ADDRESSES` | `false` | Allow HTTP/private destinations for isolated local development only |
| `MEDIA_ENGINE_WORKER_API_URL` | `http://api:8080` | Control-plane URL used by a worker; standalone nodes must set an HTTPS or private-network URL |
| `MEDIA_ENGINE_WORKER_ADVERTISED_API_URL` | unset | Worker-facing URL included in deployment configuration generated by the dashboard |
| `MEDIA_ENGINE_WORKER_TOKEN` | unset | Individually revocable worker token returned once by the dashboard or management API |
| `MEDIA_ENGINE_WORKER_TOKEN_FILE` | unset | Optional mounted-secret file containing the worker token |
| `MEDIA_ENGINE_WORKER_BACKEND` | `cpu` | Worker backend capability; current Compose files use `cpu`, `rkmpp`, or `nvv4l2` |
| `MEDIA_ENGINE_WORKER_PROFILE` | `cpu` | Packaging profile reported by the worker, such as `cpu`, `rk1`, or `jetson-xavier-nx` |
| `MEDIA_ENGINE_WORKER_HOSTNAME` | Compose-specific | Descriptive runtime hostname reported by a standalone worker, such as `rknode-2` |
| `MEDIA_ENGINE_WORKER_LEASE_SECONDS` | `90` | Time a worker owns a stage without a heartbeat |
| `MEDIA_ENGINE_WORKER_HEARTBEAT_SECONDS` | `20` | Worker heartbeat interval during processing |
| `MEDIA_ENGINE_WORKER_POLL_SECONDS` | `2` | Worker delay between empty stage-claim attempts |
| `MEDIA_ENGINE_TRANSCODE_REQUIRED_BACKEND` | unset | Restrict new transcode stages to `cpu`, `rkmpp`, or a future backend |
| `OPENAI_API_KEY` | unset | Optional startup import into encrypted provider configuration; may be removed after import |
| `MEDIA_ENGINE_OPENAI_TIMEOUT_SECONDS` | `600` | Timeout for one OpenAI request |
| `MEDIA_ENGINE_OPENAI_MAX_RETRIES` | `2` | SDK retries for transient OpenAI failures |
| `XAI_API_KEY` | unset | Optional startup import into encrypted provider configuration; may be removed after import |
| `MEDIA_ENGINE_XAI_BASE_URL` | `https://api.x.ai/v1` | xAI-compatible API base URL |
| `MEDIA_ENGINE_XAI_TIMEOUT_SECONDS` | `600` | Timeout for one xAI request |
| `MEDIA_ENGINE_XAI_MAX_RETRIES` | `2` | SDK retries for transient xAI Responses failures |

Quality profiles live in `app/transcode/profiles.py`; adjust widths/bitrates or add new presets as needed.

## Run the platform locally

The default Compose deployment is the supported platform development path. It starts PostgreSQL, MinIO, schema migrations, the API, maintenance scheduler, webhook dispatcher, and a CPU worker.

```bash
./scripts/create-local-env.sh
# Optionally add OPENAI_API_KEY and/or XAI_API_KEY for a one-time encrypted import.
docker compose --env-file .env.local up -d --build
curl http://localhost:8080/readyz
```

`.env.local` is ignored by Git and excluded from Docker build contexts. The helper preserves existing values and fills
missing platform secrets. The API imports optional vendor keys into encrypted PostgreSQL records; worker containers do
not receive vendor-key environment variables. Interactive API documentation is available at `http://localhost:8080/docs`;
the machine-readable OpenAPI document is at `http://localhost:8080/openapi.json`. The management dashboard is available
at `http://localhost:8080/admin` and uses the administrator username and password from `.env.local`.

Create a client and API key through the dashboard or `/v2/admin` before calling protected asset or job endpoints.
Administrator API endpoints accept HTTP Basic bootstrap credentials from `.env.local`; the browser exchanges those
credentials for a short-lived signed, HTTP-only session cookie. A newly created client key is shown once and should be
stored by the calling product. Public API keys use `Authorization: Bearer <media-engine-api-key>` and enforce `assets:read`,
`assets:write`, `jobs:read`, and `jobs:write` scopes. See [docs/api-v2.md](docs/api-v2.md) for examples.

Upload media twice to verify content reuse:

```bash
curl -F "file=@sample.mp4" \
  -H "Authorization: Bearer $MEDIA_ENGINE_CLIENT_API_KEY" \
  -F 'source_metadata={"provider":"why-tee-webdl","source_id":"example"}' \
  http://localhost:8080/v2/assets
```

The first retained copy reports `duplicate_content: false`; another upload with the same exact bytes reports `true` and reuses the same S3 object. Source filename, URI, and metadata remain distinct asset provenance.

Request a server-owned pipeline using the returned asset ID:

```bash
curl -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MEDIA_ENGINE_CLIENT_API_KEY" \
  -H "Idempotency-Key: whytee-job-123" \
  -d '{
    "asset_id": "<asset-id>",
    "pipeline": "ai_prepare",
    "options": {"max_keyframes": 12},
    "client_job_id": "whytee-job-123"
  }' \
  http://localhost:8080/v2/jobs
```

Poll `/v2/jobs/<job-id>`. When it is `completed`, `/v2/jobs/<job-id>/manifest` returns the complete stage and artifact provenance, and `/v2/jobs/<job-id>/artifacts` provides short-lived signed download URLs. A later request for the same exact bytes and normalized pipeline options returns `cache_hit: true` without another worker attempt.

Use `pipeline: "understand"` to run the latest understanding contract. Version 2 uses the enabled default management
provider (preferring xAI for an imported local setup) and automatically classifies the media as
`meme`, `tutorial`, `talk`, `action`, or `general`, combines 12 coarse frames with up to 12 targeted frames, and returns
an evidence-linked `agent_document` with key claims, caveats, procedures, commands, warnings, and a timeline. Set
`analysis_profile` explicitly to override classification, or submit `pipeline_version: "1"` to reproduce the original
12-frame OpenAI baseline. If no enabled provider connection exists, an understanding request returns HTTP 503 with an
actionable configuration message instead of creating work that cannot finish.

Understand v2 also supports xAI independently for transcription, planning, vision, and summary. Set the corresponding
`*_provider` option to `xai`; omitted model names normalize to `grok-transcribe` for speech and `grok-4.5` for the
Responses stages. Providers may be mixed within one run. Provider and model choices are included in the deterministic
run key, so paired OpenAI/xAI evaluations never collide in the cache.

Pipeline discovery separates supported provider alternatives from the instance's current effective defaults. Dynamic
provider/model defaults appear in `effective_options`, while each selectable stage reports `provider_selection` and
`effective_required_capabilities`; the completed manifest remains authoritative for the accepted run.

The platform contract is documented in [docs/api-v2.md](docs/api-v2.md), and storage/cache semantics in [docs/artifact-and-cache.md](docs/artifact-and-cache.md).

## Run the temporary legacy transcoder

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

> **Migration note:** `/jobs` still stores its inputs and outputs under `/data`, but it is not part of the future stable contract. Tag a known-working legacy release before migrating a production client. Platform v2 only uses local disk as upload scratch; S3 is its durable media store.

## Rockchip RK1 acceleration (optional)

For an all-in-one RK1 deployment, layer the local RK1 worker definition over the default deployment:

```bash
docker compose --env-file .env.local -f compose.yaml -f compose.local-rk1.yaml up -d --build
```

The overlay requires `rkmpp` for newly queued transcode stages, moves the default CPU worker behind the optional `cpu-fallback` profile, and exposes Rockchip devices only to `worker-rk1`. The API and PostgreSQL remain hardware-neutral.

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
3. **Enable the startup guard** – set `MEDIA_ENGINE_REQUIRE_RK_ACCEL=true` (and optionally point `MEDIA_ENGINE_FFMPEG_COMMAND` to the hardware-enabled binary). Consider `MEDIA_ENGINE_ALLOW_CPU_FALLBACK=false` if you want hardware failures to abort instead of silently falling back to software. The self-test will fail if RKMPP decoders are missing so you catch misconfiguration early.

The default image continues to work on generic CPUs without this setup; the guard only triggers when you opt in via the environment variable.

## Standalone worker nodes

Worker nodes run independently from the control plane, open no inbound ports, and need no PostgreSQL, S3, administrator,
or provider credentials. Each node has its own token, so it can be drained, rotated, or revoked without affecting any
other worker.

### Add a worker in five minutes

1. Open **Dashboard → Workers → Add worker**.
2. Enter a display name and profile such as `cpu`, `rk1`, or `jetson-xavier-nx`.
3. Copy the generated `.env.worker` configuration. The token is shown only once.
4. On the worker host, download this repository or copy `compose.worker.yaml` and save the generated values as
   `.env.worker` with mode `600`.
5. Validate and start the container:

```bash
chmod 600 .env.worker
docker compose --env-file .env.worker -f compose.worker.yaml config
docker compose --env-file .env.worker -f compose.worker.yaml up -d
docker compose --env-file .env.worker -f compose.worker.yaml logs -f worker
```

6. Return to **Workers** and confirm the node is online and reporting its hostname, version, backend, and processors.
7. Submit a compatible job and confirm its active lease appears on that worker.

Workers publish their detected decoder capabilities. If a strict hardware worker discovers an unsupported source codec
after probing the downloaded input, it returns the lease without spending an attempt. The control plane adds the codec
as a stage requirement and routes the next claim to a compatible worker. A job stays queued with a clear
`unsupported_input_codec` message when no attached worker can decode it; the job detail shows the missing decoder.

Use `compose.worker-rk1.yaml` for an RK1 or `compose.worker-jetson-xavier-nx.yaml` for a Jetson Xavier NX. Every
definition contains exactly one worker service plus a scratch volume. Media moves directly between the node and S3
using short-lived signed URLs; the API does not proxy large stage files.

The control plane must set `MEDIA_ENGINE_S3_WORKER_ENDPOINT_URL` to an S3/MinIO address reachable from the worker. It
must expose `/v2/internal/*` on its HTTPS API origin. Set `MEDIA_ENGINE_WORKER_ADVERTISED_API_URL` to that same origin so
the dashboard can generate a ready-to-run worker environment. A separate private or IP-restricted worker hostname is an
optional advanced deployment, not a requirement.

Use `compose.control-plane.yaml` as an overlay when the central deployment should not start its bundled CPU worker:

```bash
docker compose --env-file .env.local \
  -f compose.yaml -f compose.control-plane.yaml \
  up -d --build
```

See [docs/worker-deployment.md](docs/worker-deployment.md) for CPU, RK1, and Jetson installation, Portainer,
worker-only NGINX, token rotation, draining, upgrades, custom hardware flavours, and troubleshooting.

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
- **Platform deployment**: `compose.yaml` – PostgreSQL, MinIO, migrations, API, scheduler, and CPU worker.
- **Control plane without a local worker**: `compose.yaml` plus `compose.control-plane.yaml`.
- **All-in-one platform with RK1**: `compose.yaml` plus `compose.local-rk1.yaml`.
- **Standalone CPU worker node**: `compose.worker.yaml` with `.env.worker`.
- **Standalone RK1 worker node**: `compose.worker-rk1.yaml` with `.env.worker`.
- **Standalone Jetson Xavier NX worker node**: `compose.worker-jetson-xavier-nx.yaml` with `.env.worker`.
- **Generic CPU**: `docker-compose.cpu.yml` – simple volume/port mapping.
- **Rockchip RK1**: `docker-compose.rockchip.yml` – includes device pass-through and enables RKMPP checks.

Run e.g.
```bash
docker compose -f docker-compose.cpu.yml up -d
```

## Building + pushing images
Use the helper in `scripts/dockerbuild.sh` to publish the generic, RK1, and Jetson Xavier NX variants.
```bash
./scripts/dockerbuild.sh v0.4.1
```
Environment knobs:
- `IMAGE_REPO` – Docker repository name.
- `BASE_PLATFORMS` – Comma-separated platforms for the generic image (default `linux/amd64,linux/arm64`).
- `RK_PLATFORMS` – Platforms for the RK1 image (default `linux/arm64`).
- `JETSON_PLATFORMS` – Platforms for the Xavier NX image (default `linux/arm64`).
- `PUSH` – Set to `false` to load images locally instead of pushing.

## Roadmap notes

The durable worker graph, scoped public API authentication, encrypted provider control plane, signed completion webhooks,
management dashboard, and first media-understanding pipelines are implemented. Next priorities are client limits, the
whY-Tee-WebDL v2 migration, semantic timestamp search, and more hardware backends.
