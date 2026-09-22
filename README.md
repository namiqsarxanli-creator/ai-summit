# FACEMARK

Event networking and gamification platform for an AI Summit demo.

**Current status: M01 — Project Foundation only.** No business functionality
exists yet (no registration, QR, staff scanner, points, leaderboard, or
admin). See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the system design and
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) for the milestone plan
this repository is built against.

Every command below was executed and verified against this repository
before being documented.

---

## Prerequisites

| Tool | Version used | Notes |
|---|---|---|
| Docker Desktop / Docker Engine | 29.5.3, Docker Compose v5.1.4 | Required. The backend, frontend and database all run in containers |
| Node.js | v24 | Required only if you want to run `npm` commands (lint/typecheck/build/test) directly on the host for a faster feedback loop. Not required to run the app — the frontend container has its own Node install |
| npm | v11 | Ships with Node |
| Python | **not required on the host** | The backend's runtime is **Python 3.12**, pinned inside `backend/Dockerfile`, regardless of what Python version (if any) is installed on your machine. All backend commands (tests, lint, type checks, Alembic) are run inside the container — see below |
| Git | any recent version | |

You do **not** need to install PostgreSQL, Python 3.12, or any backend
dependency on your host. Docker Compose is the supported development
workflow for this project.

---

## Initial Setup

### 1. Create your environment file

PowerShell:
```powershell
Copy-Item .env.example .env
```

Git Bash / WSL:
```bash
cp .env.example .env
```

Open `.env` and adjust values if you like — the defaults work for local
development out of the box. **Never commit `.env`** (it is git-ignored).

### 2. Build the images

```bash
docker compose build
```

This builds the `backend` image (Python 3.12, dependencies installed into
`/opt/venv`) and the `frontend` image (Node 24, `npm install`).

### 3. Start everything

```bash
docker compose up -d
```

This starts three containers, in dependency order:

1. `db` — PostgreSQL 16, waits until `pg_isready` reports healthy
2. `backend` — FastAPI, starts once `db` is healthy
3. `frontend` — Next.js, starts once `backend` is up

Check they are all running:

```bash
docker compose ps
```

You should see all three as `Up` (and `db` specifically as `Up (healthy)`).

---

## Verification

Every check below was run against a real build of this repository.

### PostgreSQL health

```bash
docker compose ps
```
`db` should show `Up (healthy)` — this uses the compose-level `pg_isready`
healthcheck, which is also what gates the `backend` container's startup.

### Backend health (direct)

```bash
curl http://localhost:8001/api/health
```
Expected, exactly:
```json
{"status":"ok"}
```
(Port `8001` is the host-side mapping from `.env`'s `BACKEND_PORT` — the
backend also runs the database connectivity check as part of serving this
route, so a `503` here means the backend is up but cannot reach Postgres.)

### Frontend

```bash
curl http://localhost:3000/
```
Should return the FACEMARK foundation page HTML (HTTP 200).

### Frontend → backend proxy (the same-origin `/api` strategy)

This is the important one — it proves a request to the **frontend's own
origin** is transparently forwarded to FastAPI, exactly as
`ARCHITECTURE.md` §3.2 describes:

```bash
curl http://localhost:3000/api/health
```
Expected, exactly the same body as the direct backend check:
```json
{"status":"ok"}
```

Open `http://localhost:3000` in an actual browser and you will also see
this same JSON rendered live on the page — the page performs a
client-side `fetch("/api/health")` on load and displays the result.

---

## Quality Commands

### Frontend

Run from `frontend/` (either on the host with Node 24, or via
`docker compose exec frontend <command>`):

```bash
npm run lint        # eslint .
npm run typecheck    # tsc --noEmit
npm run build         # next build — production build
npm run test           # vitest run — component tests
```

All four pass cleanly on a clean checkout.

> **Note:** `npm run build` reads `BACKEND_INTERNAL_URL` at build time to
> validate the rewrite rule in `next.config.ts`. If you run it on the host
> (not inside the container), export it first:
> ```bash
> export BACKEND_INTERNAL_URL="http://backend:8000/api"   # Git Bash
> $env:BACKEND_INTERNAL_URL = "http://backend:8000/api"   # PowerShell
> ```
> This is deliberate, not a bug — the same "missing required config fails
> loudly" principle applies here as everywhere else in the project.

### Backend

Run inside the `backend` container (Python 3.12 lives only there):

```bash
docker compose exec backend pytest -v          # test suite
docker compose exec backend ruff check .       # lint
docker compose exec backend mypy app tests alembic   # strict type checking
```

All three pass cleanly on a clean checkout. `pytest` includes a real
PostgreSQL connectivity test (`tests/test_db_connectivity.py`) — the `db`
container must be up and healthy for it to pass.

### Database migrations

```bash
docker compose exec backend alembic upgrade head     # apply migrations
docker compose exec backend alembic current           # show current revision
docker compose exec backend alembic downgrade base    # roll back everything
```

M01 ships one intentionally empty baseline migration (`0001_baseline`) that
proves the mechanism works end-to-end against real PostgreSQL. No business
tables exist yet — those begin in M02.

---

## Shutdown

```bash
docker compose down
```
Stops and removes the containers; the named volume `facemark_postgres_data`
(and therefore your local database contents) is preserved.

To also wipe the database volume (full reset):
```bash
docker compose down -v
```

---

## Troubleshooting

**Docker daemon not running.**
`docker compose up` fails immediately with something like `error during
connect... the system cannot find the file specified`. Start Docker
Desktop and wait for it to report "running" before retrying. This project
cannot start any service without a running Docker daemon — there is no
host-native fallback.

**PostgreSQL port conflict (`5433` already in use).**
If another process already has port `5433` bound, `docker compose up`
will fail to publish the `db` port. Change `POSTGRES_PORT` in `.env` to a
free port and re-run `docker compose up -d`. (The default in this
repository is intentionally `5433`, not `5432`, because `5432` is commonly
already taken by another local Postgres instance.)

**`.env` file missing.**
`docker compose up` will fail or start services with empty/blank
credentials, and the backend will fail at startup with a Pydantic
`ValidationError` naming the missing field (this is deliberate — see
`app/core/config.py`). Run `Copy-Item .env.example .env` (or `cp
.env.example .env`) and try again.

**Backend cannot connect to the database.**
`GET /api/health` returns `503 database unreachable`. Check:
- `docker compose ps` — is `db` `Up (healthy)`? If it's still starting,
  wait a few seconds; the backend's `depends_on: condition:
  service_healthy` should already handle this, but a slow first boot on a
  fresh volume can take longer.
- Do the `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` values in
  `.env` match what the `db` container was originally initialized with? If
  you changed them after the first `docker compose up`, the existing
  volume still has the old credentials — either revert `.env` or run
  `docker compose down -v` to reinitialize with the new ones.

**Frontend proxy cannot reach the backend.**
`curl http://localhost:3000/api/health` fails or hangs, but
`curl http://localhost:8001/api/health` works directly. Check:
- `BACKEND_INTERNAL_URL` in `.env` — it must be
  `http://backend:8000/api` (the Compose service DNS name `backend`, not
  `localhost` and not the host-mapped port `8001`). `localhost` inside the
  frontend container refers to the frontend container itself, not the
  backend.
- Restart the frontend container after changing `.env`:
  `docker compose up -d --force-recreate frontend`.

---

## Repository Layout (M01)

```
FACEMARK/
├── ARCHITECTURE.md          # locked system architecture (authoritative)
├── IMPLEMENTATION_PLAN.md   # locked milestone plan
├── README.md                 # this file
├── docker-compose.yml         # db + backend + frontend
├── .env.example                # environment template (tracked)
├── .env                         # your local secrets (git-ignored, not tracked)
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml         # dependencies + ruff/mypy/pytest config
│   ├── alembic.ini
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/0001_baseline.py   # empty — proves the mechanism only
│   ├── app/
│   │   ├── main.py             # app factory, /api/health
│   │   ├── core/config.py       # Settings — required, no fallback defaults
│   │   ├── db/{base,session}.py # SQLAlchemy engine/session, naming convention
│   │   └── modules/              # empty — domain modules start in M02
│   └── tests/
└── frontend/
    ├── Dockerfile
    ├── package.json
    ├── next.config.ts          # the /api/* same-origin rewrite
    ├── src/app/
    │   ├── layout.tsx
    │   ├── page.tsx              # foundation page: proves the proxy works
    │   ├── page.test.tsx
    │   └── globals.css
    └── vitest.config.ts
```

No `User`, `Booth`, `PointsLog`, or any other business-domain model exists
in this repository yet — see `IMPLEMENTATION_PLAN.md` M02 onward.
