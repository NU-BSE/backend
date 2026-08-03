# Creepy.IM backend

FastAPI + PostgreSQL backend for Creepy.IM. Separate service from the Express
attestation server; it owns accounts, subscription gating and the cloud agent
proxy.

Persistence scope is deliberately minimal (see `sql/migrate_001.sql`, kept in
sync with the frontend's `db/schema.sql`):

- `users` — the account
- `subscription_plans`, `subscriptions`, `subscription_invoices`,
  `subscription_events`, `subscription_entitlements` — the gate for agent usage

Everything else stays on the device: chat history, auth providers, attestation.

## Endpoints

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/auth/email/request-code` | – | send a 6-digit code by email (Resend) |
| POST | `/auth/email/verify-code` | – | verify code, upsert user, issue JWTs |
| POST | `/auth/refresh` | refresh token | new access token |
| POST | `/auth/logout` | – | client-side discard (contract only) |
| GET | `/users/me` | bearer | profile |
| PATCH | `/users/me` | bearer | update name |
| GET | `/subscriptions/plans` | – | public plan catalogue |
| GET | `/subscriptions/me` | bearer | active subscription + entitlements |
| POST | `/admin/grant` | admin bearer | manual subscription grant (MVP switch) |
| POST | `/chat/http` | bearer | gated AG-UI streaming agent proxy |
| GET | `/healthz` | – | db + config checks |

Auth responses are camelCase (`accessToken`, `refreshToken`,
`onboardingCompleted`) to match `src/auth/emailAuth.ts` in the frontend.
`onboardingCompleted` is derived: a user has completed onboarding once they
have a name.

### The agent gate (`POST /chat/http`)

1. Verify `Authorization: Bearer <accessToken>`.
2. Resolve entitlements from an active subscription:
   - no `agent_access` → `403 {code: "AGENT_ACCESS_REQUIRED"}`
   - no `cloud_agent_allowed` → `403 {code: "CLOUD_AGENT_NOT_ALLOWED"}`
   - daily cap exceeded → `402 {code: "USAGE_LIMIT_REACHED", retryAfterSeconds}`
3. Count the message (Redis `INCR`, daily TTL).
4. Proxy to the upstream LLM and stream AG-UI events back as **NDJSON** —
   one JSON event per line, the exact framing `xhrHttpStream`
   (@tanstack/ai-client) parses:

```
{"type":"RUN_STARTED","threadId":...,"runId":...}
{"type":"TEXT_MESSAGE_START","messageId":...,"role":"assistant"}
{"type":"TEXT_MESSAGE_CONTENT","messageId":...,"delta":"..."}   (xN)
{"type":"TEXT_MESSAGE_END","messageId":...}
{"type":"RUN_FINISHED","threadId":...,"runId":...}
```

Failed runs terminate with `RUN_ERROR` (never a silent close).

Email codes, rate limits and usage counters live in Redis (or an in-process
TTL store when `REDIS_URL` is empty — dev only). Tokens are stateless JWTs:
~15 min access, ~30 day refresh; no session table.

## Local development

```bash
cp .env.example .env          # set JWT_SECRET (openssl rand -hex 32)
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
docker compose up -d postgres redis
.venv/bin/uvicorn app.main:app --reload --port 8000
```

The schema (`sql/migrate_001.sql`) is applied idempotently on startup and the
`free`/`pro` plans are seeded. Swagger UI: `http://localhost:8000/docs`.

With `LLM_MOCK=true` (default in `.env.example`) the agent streams a canned
reply, so the whole gate can be exercised without any LLM keys. With no
`RESEND_API_KEY`, verification codes are logged instead of emailed.

### Granting access (manual MVP)

Register through the app (or the two auth endpoints), then as a user listed in
`ADMIN_EMAILS`:

```bash
curl -X POST http://localhost:8000/admin/grant \
  -H "Authorization: Bearer <adminAccessToken>" \
  -H "Content-Type: application/json" \
  -d '{"userId":"usr_...","planCode":"pro","days":30}'
```

### Checks

```bash
make check    # ruff + mypy + pytest
```

Tests run against sqlite + the in-memory TTL store — no services required.

## Frontend integration

- `EXPO_PUBLIC_API_URL` → this service's base URL (auth + users + subscriptions).
- `EXPO_PUBLIC_TANSTACK_AI_BASE_URL` → this service's base URL; the client
  then calls `POST {base}/chat/http` via `xhrHttpStream` (src/ai/index.ts).
- The remote connection must send `Authorization: Bearer <accessToken>` —
  pass it via `xhrHttpStream(url, { headers: { Authorization: ... } })`.
  Without the header the gate cannot work.
- Optional: surface the 402/403 rejection `code`s (`CLOUD_AGENT_NOT_ALLOWED`,
  `USAGE_LIMIT_REACHED`) in the chat UI as an upgrade prompt.

## Roadmap

- Stripe checkout + webhooks → reconcile into `subscriptions`,
  `subscription_invoices`, `subscription_events`; recompute entitlements on
  each lifecycle event (schema already in place).
- `auth_sessions` if server-side revocation becomes necessary.
- Observability: metrics for agent calls/rejections.
