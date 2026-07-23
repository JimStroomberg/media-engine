# Management dashboard

Media Engine includes a same-origin management dashboard at `/admin`. It is intended for operators who need to inspect
the system or configure integrations without direct access to PostgreSQL.

## What it manages

The dashboard currently provides:

- an operational overview of jobs, active stages, workers, provider state, usage, and webhook failures;
- filterable job history with stage attempts, worker assignments, artifacts, errors, provider usage, and webhook state;
- worker online/offline state, last heartbeat, capabilities, and current lease;
- OpenAI and xAI provider creation, editing, credential verification, enable/disable, default selection, and removal;
- client-project creation and scoped API-key generation or revocation;
- webhook endpoint creation, default selection, test delivery, signing-secret rotation, and delivery history;
- provider/model token, duration, latency, outcome, and estimated-cost reporting.

Job execution remains read-only in this first interface. It does not retry, cancel, delete, or mutate jobs and artifacts.

## Local use

Start the normal Compose deployment, then open:

```text
http://localhost:8080/admin
```

Log in with `MEDIA_ENGINE_ADMIN_USERNAME` and `MEDIA_ENGINE_ADMIN_PASSWORD` from `.env.local`. The local environment
helper creates `MEDIA_ENGINE_ADMIN_SESSION_SECRET` automatically without replacing existing values:

```bash
./scripts/create-local-env.sh .env.local
docker compose --env-file .env.local up -d --build
```

New API keys and webhook signing secrets appear once. Copy them into the consuming product before closing the notice.
Media Engine stores only an API-key hash and stores webhook/provider credentials encrypted, so it cannot reveal those
plaintext values later.

## Production deployment

Serve the API and dashboard through HTTPS and set:

```dotenv
MEDIA_ENGINE_ADMIN_SESSION_COOKIE_SECURE=true
```

Keep `/admin`, `/admin/session`, and `/v2/admin` behind the same trusted network or access proxy. Use a unique, randomly
generated `MEDIA_ENGINE_ADMIN_SESSION_SECRET` of at least 32 characters and keep it stable across API restarts and
replicas. Rotating the secret or administrator password invalidates existing dashboard sessions.

The dashboard session lasts 12 hours by default. Change it with `MEDIA_ENGINE_ADMIN_SESSION_TTL_HOURS` (accepted range:
1 to 168 hours). The session is stateless, so logging out removes it from that browser; rotate the session secret when
all issued sessions must be revoked immediately.

Dashboard requests carry an internal UI marker. This lets expired or invalid browser sessions return to the Media Engine
login page without opening the browser's separate HTTP Basic authentication dialog. HTTP Basic remains available for
scripts calling the management API directly.

## API automation

Scripts can call `/v2/admin` with HTTP Basic authentication and do not need a browser session. Dashboard-only routes are
excluded from OpenAPI, while all management data and configuration endpoints remain documented at `/docs` and
`/openapi.json`.
