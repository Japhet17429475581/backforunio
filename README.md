# UNIO Backend

FastAPI services for [UNIO](https://github.com/Japhet17429475581/unio), a Flutter
university companion for Android and PWA.

## Responsibilities

- Public student and teacher schedules, group validation, and room lookup.
- PostgreSQL persistence and Redis caches / distributed refresh locks.
- University news ingestion and article retrieval.
- UNIO token authentication, consent receipts, account deletion, and retention.
- A browser-only university portal bridge using encrypted university cookies.
- Optional Web Push and a deterministic notification plan for native clients.
- Optional document assistant: PostgreSQL full-text retrieval, bounded context,
  citation validation, contact lookup, and application actions.
- Structured operational logs that omit credentials, raw queries, and bodies.

The Android client authenticates directly with the university. Unused legacy
credential routes are unmounted. The explicitly configured PWA bridge processes
credentials during login; it stores encrypted university cookies and token
hashes, not university passwords. There are no server-side chat-history tables.
Personal grade cards are handled by the client rather than sent to the model.

## Requirements

Python **3.12**, PostgreSQL with permission to create the application tables and
assistant schema, and Redis. No embedding service or vector extension is needed.
`requirements.lock.txt` constrains runtime dependencies to the tested versions;
update it deliberately and rerun the suite when upgrading dependencies.

```sh
python -m venv .venv
# Linux / macOS:
source .venv/bin/activate
# Windows PowerShell:
# .venv/Scripts/Activate.ps1
pip install -r requirements.txt
```

Copy settings from `.env.example` into your host environment. `main.py` reads
environment variables; it does not automatically load a local `.env` file.
For local development, Uvicorn can load it explicitly:

```sh
uvicorn main:app --host 127.0.0.1 --port 8000 --env-file .env --no-access-log
```

For the configured hosting entry point:

```sh
python main.py
```

## Configuration

| Setting | Purpose / default |
| --- | --- |
| `DATABASE_URL` | PostgreSQL connection URI; required |
| `REDIS_URL` | Redis URI; default `redis://localhost:6379/0` |
| `HOST`, `PORT` | Listener; default `0.0.0.0:8000` |
| `POSTGRES_MIN_POOL_SIZE`, `POSTGRES_MAX_POOL_SIZE` | Default `1`, `10` |
| `CACHE_TTL` | Schedule cache freshness, default 86400 seconds |
| `NEWS_CACHE_TTL` | News-list freshness, default 300 seconds |
| `NEWS_SYNC_INTERVAL` | News synchronization interval, default 86400 seconds |
| `AUTH_TOKEN_TTL_DAYS` | UNIO token lifetime, default 30 days |
| `USER_RETENTION_DAYS` | Inactive-account retention, default 90 days |
| `SCHEDULE_RETENTION_DAYS` | Old schedule retention, default 90 days |
| `UNIO_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`; default `INFO` |

A schedule contains a week's lessons; cache freshness is a separate duration.
The periodic retention job runs hourly. Provider logs, host backups, and external
AI retention must be configured separately by the operator.

## Database initialization

Startup creates the base schema and runs `migrations/*.sql` in filename order.
Migrations are idempotent; migration execution is serialized with a PostgreSQL
advisory transaction lock. Deploy the complete `migrations/` directory. Missing
web-push tables can otherwise break logout and retention.

Back up the database before deployment. The schema user needs the permissions
used by the migrations. This release requires no new connection-URI format and
adds no new schema migration. Startup and shutdown clean up partially acquired
connections and background tasks.

## PWA portal and push

Set `WEB_PORTAL_ORIGINS` to the allowed frontend HTTPS origins (comma-separated)
and `WEB_PORTAL_KEYS` to one or more Fernet keys (newest first). Generate a key:

```sh
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Store keys in the hosting secret store. Keep old keys during rotation so existing
cookies remain decryptable. Changing or losing all keys invalidates stored web
sessions. Configure an HTTPS reverse proxy and same-origin `/api/` forwarding;
see the frontend's `deploy/pwa/` example.

Web Push additionally uses `WEB_PUSH_PUBLIC_KEY`, `WEB_PUSH_PRIVATE_KEY`, and
`WEB_PUSH_SUBJECT`. Browser notification support and installed-PWA requirements
vary. Do not enable push without stable encryption keys.

## Assistant

Set `AI_ENABLED=true`, `DEEPSEEK_API_KEY`, and the desired `DEEPSEEK_MODEL`.
See `assistant/.env.example` for token budgets, context size, and concurrency.
The main `.env.example` enables quotas with `AI_TEST_MODE=false`; the existing
code defaults to closed-test mode if the variable is omitted, so explicitly set
it for every deployment.

Startup imports the bundled `assistant/data/corpus.json`, `full_text.md`, and
additional knowledge records when the assistant is enabled. Original PDFs and
Word files are not required. Reviewed / active state and applicability still
control retrieval. Owner-approved test records are bundled; that approval is not
an independent verification that every university document is still current.

```sh
python -m assistant.manage status
python -m assistant.manage list
python -m assistant.manage import
```

Activate a newly reviewed document with the management command and applicable
dates. Do not automatically approve unknown future uploads. Do not put provider
keys in the mobile app or commit them to Git.

## Tests

```sh
pip install -r requirements-dev.txt
python -m pytest
```

Tests mock external services and do not need a production database or portal
account. The legal-document fixture keeps the client/server version contract
testable in this standalone repository. When changing legal text, update the
frontend asset, `legal_versions.json`, consent version bindings, and
`fixtures/legal_documents.json` together.

Tests cover parsers, privacy, legacy-client compatibility, web login / refresh,
assistant retrieval, news cache page sizes, and cleanup after startup failures.
They do not replace real PostgreSQL migration checks or physical-device tests.

## Deployment

```sh
docker build -t unio-backend .
docker run --env-file .env -p 8000:8000 unio-backend
```

`GET /health` reports service status. Keep `.env`, keys, dumps, caches, original
document archives, and production logs out of the image and repository. Review
proxy logging too: application-side redaction does not configure the host's
access logs or backups. Backend source and model names are visible here; model
selection is not exposed in the app UI.

UNIO is an independent project. This repository does not establish official
university endorsement or certify compliance with personal-data legislation.
