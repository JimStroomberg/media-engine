# Media Engine API v2

The v2 API is the target public contract. The legacy `/jobs` endpoints remain only as temporary migration scaffolding and carry no pre-v2 compatibility guarantee. Until the stable-v2 threshold in [development-policy.md](development-policy.md) is met, documented breaking changes are allowed when they simplify or strengthen the public design.

## Authentication and ownership

Asset and job endpoints require a Media Engine client API key as `Authorization: Bearer <key>`. API keys belong to one
client project, are stored as hashes, can expire or be revoked, and grant one or more scopes:

- `assets:write`: `POST /v2/assets`;
- `assets:read`: `GET /v2/assets/{asset_id}`;
- `jobs:write`: `POST /v2/jobs`;
- `jobs:read`: job status, artifacts, and manifests.

A client cannot read or submit work for another client's asset or job. Exact media bytes and completed pipeline runs can
still be reused across clients; the API only exposes shared artifacts through a job request owned by the caller.
Pipeline discovery, health, readiness, and generated API documentation remain public.

Missing or invalid credentials return HTTP 401 with `WWW-Authenticate: Bearer`; a valid key without the required scope
returns HTTP 403.

## Initial asset endpoints

### `POST /v2/assets`

Accept a multipart media upload plus optional source metadata. The API stages the upload, calculates SHA-256, stores or reuses the content-addressed S3 blob, creates an asset record, and returns whether the blob was deduplicated.

### `GET /v2/assets/{asset_id}`

Return asset metadata, exact content identity, storage state, and expiry without exposing S3 administrator credentials.

## Durable job endpoints

- `POST /v2/jobs`: request a named, server-versioned pipeline for an asset;
- `GET /v2/jobs/{job_id}`: read client-specific durable status;
- `GET /v2/jobs/{job_id}/artifacts`: list available artifacts and signed download URLs;
- `GET /v2/jobs/{job_id}/manifest`: retrieve the source, exact stage graph, provenance, and all outputs.

Pipeline contracts are discoverable through:

- `GET /v2/pipelines`: list current server-owned pipeline versions;
- `GET /v2/pipelines/{name}`: return options JSON Schema, effective instance defaults, dependency graph, capability
  requirements, provider alternatives, and output contract.

`options_schema` describes accepted input but deliberately omits defaults for provider and model fields whose values are
resolved from management configuration. `effective_options` contains the fully normalized values that an empty options
object would use on this instance at discovery time. `available_providers` lists enabled provider connections without
exposing credentials.

For every stage, `required_capabilities` contains only unconditional requirements. Provider-selectable stages add a
`provider_selection` object with the option names, all supported provider adapters, and the effective provider/model
defaults. `effective_required_capabilities` is the concrete worker requirement produced by `effective_options`. For
example:

```json
{
  "required_capabilities": {},
  "effective_required_capabilities": {
    "providers": ["xai"]
  },
  "provider_selection": {
    "provider_option": "vision_provider",
    "model_option": "vision_model",
    "supported_providers": ["openai", "xai"],
    "effective_defaults": {
      "provider": "xai",
      "model": "grok-4.5"
    }
  }
}
```

Provider management changes can alter effective defaults between discovery and submission. The accepted job manifest
remains authoritative for the exact normalized options and stage requirements of that run.

Defaults are validated and normalized before the run key is calculated. New client requests receive HTTP 201. Replaying the same `Idempotency-Key` and payload returns the original job with HTTP 200; reusing a key for a different payload returns HTTP 409.

`run_reused=true` means the request attached to an existing queued, running, or completed pipeline run. `cache_hit=true` is narrower: the run was completed and every required artifact was still present and unexpired.

## Optional job webhooks

Polling `GET /v2/jobs/{job_id}` is always supported and remains the authoritative status contract. Webhooks are an
optional notification channel. Destinations belong to a client project rather than to individual API keys, and must be
registered through the administrator API before a job can use them.

For `POST /v2/jobs`, the `webhook` field has three forms:

- omit it or send `null` to use the client's enabled default endpoint, when one exists;
- send `{"endpoint_id":"<registered-endpoint-uuid>"}` to select another enabled endpoint owned by the client;
- send `false` to disable delivery for this job, including when the client has a default.

Arbitrary destination URLs are not accepted on job requests. The resolved `webhook_endpoint_id` is returned with job
status and is part of the idempotent request contract. A cache-hit request still creates its own completion event, even
when it shares a prior pipeline run.

Terminal events are `job.completed` and `job.failed`. Delivery is at least once: receivers must deduplicate by the stable
`event_id`. A failed or abandoned delivery never changes the job status, manifest, or artifact availability. The
dispatcher retries network failures, HTTP 408, HTTP 429, and 5xx responses with bounded exponential backoff. Redirects
are not followed; other 3xx and 4xx responses are permanent failures.

Each request contains:

- `X-Media-Engine-Event-Id`: the stable event UUID;
- `X-Media-Engine-Timestamp`: Unix seconds used in the signature;
- `X-Media-Engine-Signature`: `v1=<hex HMAC-SHA256>`;
- a deterministic JSON body containing `api_version`, `event_id`, `event_type`, `created_at`, and job data.

Verify the signature over the exact received bytes using
`HMAC-SHA256(endpoint_secret, timestamp + "." + raw_body)`. Reject timestamps outside the receiver's replay window and
compare signatures with a constant-time function. Every endpoint has its own signing secret, returned only when the
endpoint is created or its secret is rotated.

## Pipelines

### `transcode` version 1

One durable stage producing `transcoded_media`. Options are `quality` and `codec`.

### `ai_prepare` version 1

Runs a six-stage DAG and produces reusable, provider-independent evidence:

1. `probe` produces `media_metadata`;
2. `audio_extract` produces `audio_metadata` and optional mono MP3 `audio`;
3. `subtitle_extract` produces normalized `subtitles`;
4. `scene_detect` produces timestamped `scenes`;
5. `keyframe_extract` produces a deterministic `keyframes` ZIP and `keyframe_index`;
6. `ocr` produces timestamped `ocr` evidence.

Audio is optional because valid media may have no audio stream. Every other declared required artifact must be present before its stage can complete.

### `understand` version 1 (frozen OpenAI baseline)

Extends `ai_prepare` with three OpenAI-capable stages:

1. `transcribe` produces a timestamped, speaker-aware `transcript`;
2. `visual_describe` produces timestamped `visual_descriptions` from sampled frames;
3. `summarize` combines sanitized evidence into the final `agent_document`.

Provider names and model identifiers are normalized pipeline options and are included in deterministic run identity. The
version-1 contract remains addressable for reproducibility; new integrations should use version 2 for provider choice and
adaptive sampling.

### `understand` version 2 (latest)

Version 2 retains the baseline contract and adds content-aware two-pass sampling:

1. `coarse_keyframe_extract` and `coarse_ocr` create an inexpensive 12-frame overview;
2. `transcribe` produces timestamped speech evidence in parallel with the coarse local stages;
3. `content_plan` selects `meme`, `tutorial`, `talk`, `action`, or `general` and identifies high-value timestamps;
4. `keyframe_extract` combines scene/interval coverage with up to 12 targeted timestamps, capped at 24 frames by default;
5. final `ocr` and `visual_describe` analyze the adaptive frame set;
6. `summarize` publishes `agent_document` schema version 2.

The schema-2 agent document retains the v1 summary fields and adds `analysis_profile`, evidence-linked `key_claims`,
structured `caveats`, and `procedures`. Procedure steps contain prerequisites, exact commands when evidenced, warnings,
timestamps, confidence, and references to real transcript segments, OCR frames, visual frames, subtitles, scenes, or
metadata. Invalid model-generated reference IDs are rejected instead of being published.

The primary v2 options are:

- `analysis_profile`: `auto` by default, or an explicit supported profile;
- `coarse_max_keyframes`: initial local overview, default `12`;
- `targeted_keyframes`: maximum planner-selected moments, default `12`;
- `max_keyframes`: final combined frame cap, default `24`;
- `planning_model`, `vision_model`, and `summary_model`: provider model identities included in the run key.

Each AI stage has an independent provider option: `transcription_provider`, `planning_provider`, `vision_provider`, and
`summary_provider`. Understand v2 currently accepts `openai` and `xai`. When `xai` is selected and a model is omitted,
the normalized defaults are `grok-transcribe` for transcription and `grok-4.5` for planning, vision, and summary. Mixed
provider runs are supported, and every artifact records the actual provider, model, and usage. xAI Responses usage also
records provider-reported `cost_in_usd_ticks` plus normalized `cost_usd`; xAI speech-to-text records duration and its
execution-time cost estimate. Workers advertise supported provider adapters while the scheduler only claims stages for
enabled provider connections. Billable usage is persisted immediately after each response, before structured-output and
evidence validation, so failed stage attempts remain visible even when no artifact is published. xAI is the default for
new imported local setups; otherwise the management-selected enabled default is used. Pipeline discovery resolves that
same management default and its configured model overrides into `effective_options`.

Clients may send `pipeline_version` when they require an exact contract. Omitting it selects the latest registered version.
`GET /v2/pipelines/{name}?version=1` retrieves an older contract that remains registered.

## Job manifest

The manifest is the recommended integration response for agent clients. It contains:

- exact source SHA-256, media type, client provenance, retention, and current availability;
- the normalized options and versioned processor/model identity used for caching;
- every stage, dependency, attempt, progress/error state, output contract, and produced artifact;
- every artifact's schema version, SHA-256, producer stage, expiry, availability, and signed download URL.

Artifacts use short-lived URLs. Clients should fetch a new manifest instead of storing an expired URL.

## Request example

```http
POST /v2/jobs HTTP/1.1
Content-Type: application/json
Authorization: Bearer me_<prefix>_<secret>
Idempotency-Key: telegram-update-12345

{
  "asset_id": "9fb87e77-2fc2-4d92-96ac-6bbefa4b106f",
  "pipeline": "understand",
  "options": {
    "analysis_profile": "auto",
    "max_keyframes": 24,
    "vision_detail": "high"
  },
  "client_job_id": "telegram-update-12345",
  "webhook": {"endpoint_id": "36605bda-4e54-47c8-89f6-046d358dc28a"}
}
```

Unsupported pipeline options return HTTP 422 instead of being silently ignored.

## Administrator management API

`/v2/admin` uses HTTP Basic bootstrap credentials from `MEDIA_ENGINE_ADMIN_USERNAME` and
`MEDIA_ENGINE_ADMIN_PASSWORD`. The built-in management dashboard at `/admin` exchanges the same credentials for a
signed, HTTP-only session cookie. Cookie-authenticated unsafe requests must also send
`X-Media-Engine-Admin-UI: 1`. The dashboard sends that marker on every request so an expired browser session receives a
normal JSON `401` instead of a browser-native HTTP Basic dialog. Direct API clients using HTTP Basic do not need that
header. The management contract includes:

- `GET /v2/admin/overview`: summarize job, stage, worker, client, provider, webhook, and recent usage health;
- `GET /v2/admin/workers`: list worker state, heartbeat, capabilities, and current lease;
- `POST /v2/admin/workers`: create a worker identity and return its token once;
- `PATCH /v2/admin/workers/{worker_id}`: rename or change the packaging profile;
- `POST /v2/admin/workers/{worker_id}/revoke`: immediately revoke worker access;
- `DELETE /v2/admin/workers/{worker_id}`: remove an already-revoked worker from fleet views while retaining its
  historical job attribution;
- `POST /v2/admin/workers/{worker_id}/drain`: finish active work without accepting a new claim;
- `POST /v2/admin/workers/{worker_id}/activate`: resume scheduling after draining;
- `POST /v2/admin/workers/{worker_id}/rotate-token`: invalidate the current token and return its replacement once;
- `GET /v2/admin/jobs`: filter and page job history by status, pipeline, or client;
- `GET /v2/admin/jobs/{job_id}`: inspect its run, source, stages, artifacts, provider usage, and webhook events;

- `GET/POST /v2/admin/providers`: list or add OpenAI/xAI connections;
- `PATCH/DELETE /v2/admin/providers/{provider_config_id}`: update, enable, choose the default, or remove a connection;
- `POST /v2/admin/providers/{provider_config_id}/verify`: test the stored credential;
- `GET/POST /v2/admin/clients`: list or create client projects;
- `GET/POST /v2/admin/clients/{client_id}/api-keys`: list metadata or create a scoped API key;
- `DELETE /v2/admin/api-keys/{api_key_id}`: revoke an API key;
- `GET/POST /v2/admin/clients/{client_id}/webhook-endpoints`: list or register signed destinations;
- `PATCH/DELETE /v2/admin/webhook-endpoints/{endpoint_id}`: update or soft-disable an endpoint;
- `POST /v2/admin/webhook-endpoints/{endpoint_id}/rotate-secret`: replace and return a signing secret once;
- `POST /v2/admin/webhook-endpoints/{endpoint_id}/test`: enqueue a durable test event;
- `GET /v2/admin/webhook-events` and `GET /v2/admin/webhook-events/{event_id}`: inspect event and retry history;
- `GET /v2/admin/usage`: list provider usage/cost events, optionally filtered by provider.

Provider responses expose only a short credential hint; encrypted or plaintext keys are never returned. A newly generated
client API key and webhook signing secret are each returned exactly once by their create or rotation endpoint.

Dashboard sessions expire after `MEDIA_ENGINE_ADMIN_SESSION_TTL_HOURS` (12 hours by default) and are invalidated by
changing either the administrator password or `MEDIA_ENGINE_ADMIN_SESSION_SECRET`. Set
`MEDIA_ENGINE_ADMIN_SESSION_COOKIE_SECURE=true` when serving Media Engine over HTTPS. The dashboard is deliberately not
included in the OpenAPI contract; its underlying `/v2/admin` endpoints are. See [management-dashboard.md](management-dashboard.md)
for deployment and operating guidance.

```bash
client_json=$(curl -u "$MEDIA_ENGINE_ADMIN_USERNAME:$MEDIA_ENGINE_ADMIN_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d '{"name":"telegram-bot"}' \
  http://localhost:8080/v2/admin/clients)

client_id=$(printf '%s' "$client_json" | jq -r .client_id)
curl -u "$MEDIA_ENGINE_ADMIN_USERNAME:$MEDIA_ENGINE_ADMIN_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d '{"name":"production"}' \
  "http://localhost:8080/v2/admin/clients/$client_id/api-keys"
```

## Planned public endpoints

- `DELETE /v2/jobs/{job_id}`: cancel the requesting client's attachment and cancel unshared work when safe;
- `POST /v2/assets/{asset_id}/search`: timestamped semantic search;
- `GET /v2/assets/{asset_id}/segments`: focused media or timeline retrieval.

## Idempotency and correlation

Client requests may supply an idempotency key and client job ID. Idempotency prevents accidental duplicate request records. Pipeline-run caching independently prevents duplicate media processing across different clients.

## Internal worker API

- `POST /v2/internal/workers/register`: idempotently register identity and capabilities;
- `POST /v2/internal/stages/claim`: atomically claim a compatible queued stage;
- `POST /v2/internal/stages/{id}/heartbeat`: extend a lease and report progress;
- `POST /v2/internal/stages/{id}/artifacts/prepare-upload`: validate an output declaration and return a short-lived signed
  S3 PUT URL when the content-addressed object is not already available;
- `POST /v2/internal/stages/{id}/complete`: atomically publish one or more declared S3 artifacts and advance the DAG;
- `POST /v2/internal/stages/{id}/fail`: release for retry or fail after the attempt limit.

Internal endpoints require an individually issued `mew_...` bearer token and are not part of the public product API. The
token determines the worker identity; request bodies never select a worker ID. Workers receive signed S3 transfer URLs
but no PostgreSQL or permanent S3 credentials. Provider-backed claims carry the selected stage's decrypted vendor
credential, so keep this API on a trusted private network and use TLS whenever workers cross a host boundary. Create the
identity through the dashboard or management API before starting a standalone worker. See
[worker-deployment.md](worker-deployment.md) for the deployable CPU/RK1 contract.

## Generated documentation

FastAPI serves interactive Swagger UI at `/docs`, ReDoc at `/redoc`, and the current machine-readable contract at `/openapi.json`. This document explains semantics that cannot be expressed completely by OpenAPI alone.
