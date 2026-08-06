# Changelog

All notable Media Engine changes are documented here.

## Unreleased

## 0.5.0 - 2026-08-06

### Added

- Transcode pipeline version 2 with independent `compact`, `balanced`, and `high` compression profiles plus 360p and
  1440p resolution targets.
- Documented rate-control contracts and cross-hardware VMAF, bitrate, file-size, and throughput verification for RK1 and
  Jetson Xavier NX workers.

### Changed

- CPU, RKMPP, and Jetson encoders now share codec-aware constrained-VBR targets. Jetson uses H.264 High, 10-tap VIC
  scaling, and an explicit VBR peak; RKMPP uses explicit VBR and H.264 High.
- The temporary multipart transcoder accepts and reports `quality_profile`, defaulting to `balanced`.

## 0.4.1 - 2026-08-06

### Added

- Codec-aware transcode routing: workers advertise detected input decoders, and a hardware worker can return an
  incompatible lease with a structured `unsupported_input_codec` reason without consuming a processing attempt.
- Hard media-command and no-progress watchdogs. Jetson pipelines use GStreamer's buffer watchdog, while every
  transcode subprocess has a bounded total runtime.

### Changed

- Worker heartbeats now begin before signed input downloads, so large transfers retain their lease.
- Job details in the management dashboard show the effective capability requirements learned during routing.

### Fixed

- Jetson Xavier NX workers reject AV1 and other unsupported NVDEC inputs before launching GStreamer instead of allowing
  `uridecodebin` to select `nvv4l2decoder` and stall indefinitely. The scheduler can then route AV1 to a compatible RK1
  or CPU worker.

## 0.4.0 - 2026-08-06

### Added

- Minimal one-container CPU, RK1, and Jetson Xavier NX deployments that need only a worker-facing API URL and their
  own token.
- Configurable standalone-worker hostnames so the dashboard reports recognizable physical node names instead of
  generated container IDs.
- A JetPack 5-compatible Xavier NX 16GB worker image and Compose profile using NVIDIA NVDEC, NVENC, VIC, CUDA,
  TensorRT, and detected DLA/PVA hardware. Its boot probe performs an actual hardware encode/decode round trip.

### Changed

- The recommended reverse-proxy deployment now serves the authenticated worker protocol on the normal API hostname,
  avoiding a separate worker DNS record while retaining an optional IP-restricted topology for advanced installations.
- Worker flavour is now a generic packaging profile plus advertised capabilities. The control plane schedules by
  capability rather than hardware-specific branches, allowing RKMPP, Jetson V4L2, and future NVENC or VA-API images
  to share the same worker protocol.
- RK1 Compose deployments now use the Rockchip multimedia libraries already bundled in the RK1 image instead of
  masking them with host-library bind mounts, allowing clean Ubuntu RK1 nodes to attach without package installation.

## 0.3.0 - 2026-08-06

### Added

- Individually revocable worker identities with one-time tokens, server-bound identity, expiry metadata, and dashboard
  controls for creation, rename, drain/resume, rotation, revocation, and audit-preserving removal.
- Short-lived signed S3 downloads and uploads for worker stage transfers, including stored size and SHA-256 metadata
  verification before artifact publication.
- Minimal one-container CPU and RK1 deployments that need only a worker-facing API URL and their own token.

### Changed

- Disabled unavailable local build-record uploads and Docker-specific job summaries for Docker Build Cloud while
  retaining SBOM and provenance attestations.
- Worker flavour became a generic packaging profile plus advertised capabilities, leaving room for additional
  hardware-specific images behind the same worker protocol.
- Existing shared-token worker rows are disabled by migration and must be re-enrolled with an individual credential.

### Fixed

- Worker HTTP logging and transfer exceptions no longer expose presigned S3 URLs.
- Restarting the control plane no longer undoes a bundled worker drain, revocation, expiry, or dashboard token rotation;
  stale bundled-token environment values now fail startup explicitly.

## 0.2.2 - 2026-07-25

### Changed

- Refreshed all Python packages with newer stable releases available at publication time, including FastAPI 0.140,
  OpenAI 2.48, Ruff 0.16, aiofiles 25.1, boto3 1.43.56, httpx2 2.9.1, and setuptools 83.
- Upgraded every GitHub and Docker build action to its current stable major/minor release and pinned each action to its
  immutable commit SHA.

### Fixed

- Pull-request and Dependabot image checks now use a secret-free local multi-platform build path instead of attempting
  Docker Hub authentication with credentials that GitHub intentionally withholds.

## 0.2.1 - 2026-07-25

### Added

- Production NGINX examples for separate public API and signed-artifact hostnames with HTTP/2 fallback and HTTP/3 over
  QUIC, plus an optional IP-restricted standalone-worker endpoint, a static no-backend landing page, and
  Cloudflare/direct-traffic deployment guidance.
- Linux QUIC tuning examples for eBPF connection routing, worker capacity, bounded socket buffers, VirtIO multiqueue
  verification, and staged UDP GSO enablement.

### Fixed

- Pipeline discovery now separates unconditional stage requirements from provider alternatives and reports
  management-resolved effective options, models, and worker capabilities for `understand` version 2.

## 0.2.0 - 2026-07-23

This is the first pre-stable platform-v2 development release. Its v2 API and schema may still change before the stable
compatibility boundary described in `docs/development-policy.md`.

### Added

- PostgreSQL-backed assets, clients, API keys, job requests, reusable pipeline runs, stages, artifacts, workers, provider
  usage, retention state, and durable webhook delivery.
- S3-only durable media storage with exact-byte deduplication, expiring assets and artifacts, and short-lived signed
  downloads.
- Versioned `transcode`, `ai_prepare`, and adaptive `understand` pipelines with OpenAI and xAI provider adapters.
- Capability-aware leased workers with heartbeats, retries, expired-lease recovery, and RKMPP routing.
- Encrypted provider configuration, per-client scoped API keys, provider usage/cost accounting, and optional signed job
  webhooks.
- A built-in management dashboard for operational visibility and provider, client, API-key, and webhook configuration.
- Standalone CPU and RK1 worker Compose deployments, a control-plane-only overlay, and multi-host deployment guidance.
- PostgreSQL 18.4 and the latest supported MinIO community release in the default Compose stack.

### Changed

- The default development platform now runs through `compose.yaml`; the old in-process `/jobs` transcoder remains only as
  temporary migration support for whY-Tee-WebDL.
- AI provider credentials are held by the control plane and supplied to a worker only for its claimed stage.
- Dashboard authentication uses a signed HTTP-only session and no longer triggers a second browser-native HTTP Basic
  dialog.
- Workers verify S3 access before registering online and drain active work during graceful shutdown.

### Upgrade notes

- Generate or complete `.env.local` with `./scripts/create-local-env.sh .env.local`; v2 requires PostgreSQL, S3, worker,
  administrator-session, and credential-encryption secrets.
- Run `docker compose --env-file .env.local up -d --build`; the migration service upgrades PostgreSQL to Alembic head
  before the API starts.
- Use `.env.worker.example` with `compose.worker.yaml` or `compose.worker-rk1.yaml` for independent worker nodes.
- Treat `/v2` as pre-stable. Tag a known-working legacy deployment before migrating whY-Tee-WebDL.
