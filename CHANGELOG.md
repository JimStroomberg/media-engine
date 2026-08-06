# Changelog

All notable Media Engine changes are documented here.

## Unreleased

### Added

- Individually revocable worker identities with one-time tokens, server-bound identity, expiry metadata, and dashboard
  controls for creation, rename, drain/resume, rotation, revocation, and audit-preserving removal.
- Short-lived signed S3 downloads and uploads for worker stage transfers, including stored size and SHA-256 metadata
  verification before artifact publication.
- Minimal one-container CPU and RK1 deployments that need only a worker-facing API URL and their own token.

### Changed

- Disabled unavailable local build-record uploads and Docker-specific job summaries for Docker Build Cloud while
  retaining SBOM and provenance attestations.
- Worker flavour is now a generic packaging profile plus advertised capabilities. The control plane schedules by
  capability rather than hardware-specific branches, leaving room for Jetson, NVENC, VA-API, and other images.
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
