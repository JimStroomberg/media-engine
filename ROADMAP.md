# Media Engine platform roadmap

## Phase 0: establish the v2 migration boundary

- [x] Record that pre-stable v2 may make breaking API, schema, and deployment changes.
- [ ] Tag the last known-working legacy release before migrating an existing production deployment.
- [ ] Add an end-to-end whY-Tee-WebDL to v2 contract test.
- [ ] Migrate whY-Tee-WebDL to documented v2 asset, job, and artifact endpoints.
- [ ] Remove the in-process `/jobs` implementation and legacy local-media execution path after that migration.
- [x] Add scoped public API authentication and client ownership.
- [ ] Add standardized errors and request correlation.
- [x] Add automated dependency update checks gated by tests and container builds.
- [x] Add optional signed job webhooks with registered client endpoints, durable retries, and delivery history.

## Phase 1: S3 and PostgreSQL asset foundation

- [x] Lock product, architecture, content identity, caching, and retention decisions.
- [x] Ship PostgreSQL and MinIO in the default Compose deployment.
- [x] Add versioned database migrations.
- [x] Implement content-addressed S3 blob storage.
- [x] Implement `/v2/assets` upload and exact-byte deduplication.
- [x] Record explicit asset and blob expiry.
- [x] Delete expired objects through an idempotent scheduler.

## Phase 2: durable pipeline execution

- [x] Separate client job requests from reusable pipeline runs.
- [x] Implement deterministic versioned run keys and single-flight execution.
- [x] Add durable stages, attempts, priorities, leases, heartbeats, and expired-lease recovery.
- [x] Add multi-stage dependency graphs.
- [x] Add authenticated worker registration and capability matching.
- [x] Execute v2 portable CPU transcoding outside the API process.
- [ ] Retire the legacy API's in-process transcoder after the whY-Tee-WebDL v2 migration.
- [x] Add an RKMPP worker deployment and route stages by backend capability.
- [x] Add standalone CPU/RK1 worker Compose deployments with minimal configuration and multi-host documentation.
- [ ] Validate the RKMPP worker end to end on an RK1 host.

## Phase 3: artifact pipelines

- [x] Produce multi-artifact manifests with stage provenance and signed downloads.
- [x] Add probe, audio, subtitle, scene, keyframe, and OCR stages.
- [ ] Retain sanitized source metadata and downloaded captions from clients such as whY-Tee.
- [ ] Add independently configurable retention policies for large media and small text artifacts.

## Phase 4: media understanding

- [x] Add provider-neutral transcription, vision, and summary interfaces plus local OCR processing.
- [x] Produce time-aligned transcript, scene, OCR, and visual-description artifacts.
- [x] Add automatic media profiles with coarse-plus-targeted frame sampling.
- [x] Produce evidence-linked procedures, commands, prerequisites, caveats, and warnings for tutorial media.
- [x] Add independently selectable OpenAI and xAI providers with provider/model usage in artifacts.
- [x] Persist every provider request's usage and cost at stage-attempt level, including failed validation attempts.
- [ ] Add provider-neutral embedding interfaces and implementations.
- [ ] Add semantic search and evidence retrieval by timestamp.
- [ ] Add an optional MCP adapter for agent clients.

## Phase 5: additional hardware

- [ ] Add NVIDIA NVENC/NVDEC worker images and capability probes.
- [ ] Evaluate Intel VAAPI and Apple VideoToolbox where deployment demand exists.
- [ ] Add scheduling policy based on capability, health, preference, and fallback cost.

## Phase 6: management dashboard

- [x] Add a management dashboard for operational visibility without requiring database access.
- [x] Add administrator API contracts for provider setup, verification/defaults, clients, API keys, and usage.
- [x] Add administrator API contracts for webhook endpoint setup, secret rotation, test events, and delivery history.
- [x] Add web forms for adding, editing, testing, and enabling provider connections and choosing default models.
- [x] Add web forms for client projects and scoped API-key creation/revocation.
- [ ] Add per-client operational limits and quota controls.
- [x] Add web forms for webhook endpoints, default selection, secret rotation, test delivery, and retry history.
- [x] Show workers with online/offline state, last heartbeat, capabilities, and active leases.
- [x] Show job history with status, pipeline, source asset, worker attempts, duration, cache reuse, and errors.
- [x] Show provider/model usage, cost, retries, and job-level usage detail.
- [ ] Add comparable-run views for formal quality and budget evaluations.
- [x] Add filtering and drill-down views for jobs, runs, stages, artifacts, and provider usage.
- [ ] Add dedicated retention-state and artifact filtering across jobs.
- [x] Keep work execution and job state read-only while limiting authenticated actions to explicit configuration management.
- [ ] Replace the shared worker bootstrap token with individually revocable worker credentials managed through the dashboard.
