# Changelog

All notable Media Engine changes are documented here.

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
