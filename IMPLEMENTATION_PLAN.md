# FACEMARK Implementation Plan

## 1. Purpose

This document translates the locked `ARCHITECTURE.md` into an incremental, dependency-aware execution plan. It adds **no architectural decisions**. Every milestone below implements behaviour that is already specified; where this plan names a formula, an ordering or a constraint, it is quoting the architecture, not choosing.

It is written so that another senior engineer — or a coding agent — can execute milestone by milestone without re-reading the architecture to reconstruct intent, and without needing to reinterpret ambiguous requirements mid-implementation.

## 2. Locked Baseline

**Architecture baseline commit:** `8c59fc41ee4333ba767c41ddfd622aa9b9fc965a`

> **`ARCHITECTURE.md` is authoritative. If anything in this implementation plan conflicts with it, `ARCHITECTURE.md` wins** — and the conflict is a defect in this plan, to be corrected here rather than resolved by changing the architecture.

The architecture is frozen. If implementation reveals what looks like an architectural problem, it is recorded in **§24 Implementation Clarifications** of this document and escalated. It is never silently redesigned in code.

Locked technology baseline, restated for convenience only (source: `ARCHITECTURE.md` §4):

| Layer | Technology |
|---|---|
| Frontend | Next.js (App Router), React, TypeScript `strict`, Tailwind, mobile-first PWA |
| Backend | FastAPI, Python 3.12 (container runtime), Pydantic v2, SQLAlchemy 2.x, Alembic |
| Database | PostgreSQL 16 |
| Infrastructure | Docker Compose, npm, modular monolith |
| Explicitly absent | Redis, Kafka, RabbitMQ, any broker or distributed lock service, microservices |

## 3. Delivery Strategy

**Vertical and incremental.** Each milestone delivers a thin complete slice — migration, service, endpoint, UI where relevant, tests — rather than a horizontal layer that cannot be exercised. There is no "wire it all up at the end" milestone.

**The repository is working and testable after every milestone.** Concretely: `docker compose up` succeeds, migrations apply to a clean database, the test suite passes, lint and type checks pass. A milestone that leaves the tree red is not complete.

**Demo-first, production-conscious.** Where the architecture defines a demo adapter behind a production-shaped interface (OTP delivery, rate-limit storage, realtime transport), the interface is built to the production shape from the start and the demo implementation plugs into it. No milestone ships a shortcut that would require redesign to productionise; §23 records exactly which pieces are adapters.

**Migrations and tests evolve with features.** Alembic revisions land in the milestone that needs them, never in a batch. Tests are written in the same milestone as the behaviour they verify.

**Security invariants are implemented with the feature, not postponed.** There is deliberately **no "security hardening" milestone**. Booth isolation ships with staff auth (M09). The reset fence ships with the unit-of-work helper (M02), before any mutation exists to forget it. Rate limiting and trusted-proxy resolution ship with the endpoints they protect (M05). Idempotency ships with the award (M11). M18 is a **verification gate**, not a remediation phase — if M18 finds a missing invariant, that is a defect in the earlier milestone.

**High-risk work is isolated and front-loaded within its dependency window.** M11 (award/idempotency) and M15 (reset lifecycle) are the two milestones where a subtle error is most likely and most damaging; both are scheduled with their full adversarial test suites in the same milestone, not deferred.

## 4. Milestone Dependency Map

```
M01 Foundation
 └─ M02 Database + migrations + unit-of-work fence + maintenance middleware
     ├─ M03 Seed & fixtures
     │   └─ M07 Booths, interests, matching ──┐
     ├─ M04 Registration → OTP → activation   │
     │   ├─ M05 Attendee auth, sessions, rate limiting, CSRF, trusted proxy
     │   │   └─ M06 Contact card + dual-purpose QR ──┐
     │   │       └─ M08 Attendee dashboard ◄─────────┴──(needs M07)
     │   │            └─ M13 Leaderboard + attendee snapshot
     │   │                 └─ M14 Realtime invalidation & reconciliation
     │   └─ M09 Staff auth + booth isolation
     │        └─ M10 Staff scanner + confirmation  (needs M06)
     │             └─ M11 Task award + points ledger  ◄── HIGH RISK
     │                  ├─ M12 Feedback + automatic reward
     │                  ├─ M13 (points must exist to aggregate)
     │                  └─ M16 Minimal admin  (needs M09)
     └─ M15 Demo reset system  ◄── HIGH RISK (needs M02 fence, M03 fixtures,
                                     M05/M09 sessions, M14 stream teardown)

M17 Polish + PWA        ← M08, M10, M13, M14
M18 QA & security gate  ← all of M01–M17
M19 Summit rehearsal    ← M18, M15
```

| Milestone | Depends on | Reason |
|---|---|---|
| M01 | — | Bootstrap |
| M02 | M01 | Needs app skeleton, config, Alembic |
| M03 | M02 | Fixtures need tables |
| M04 | M02, M03 | Needs `users`, `otp_challenges`, and the interest catalogue to select from |
| M05 | M04 | Sessions are created by activation |
| M06 | M04, M05 | Card is issued at registration, resolves only for `ACTIVE` |
| M07 | M02, M03 | Needs `booth_interests` and seeded weights |
| M08 | M05, M06, M07 | Renders profile, QR and matches |
| M09 | M02, M03 | Staff accounts are admin/seed-created |
| M10 | M06, M09 | Resolve needs a card and an authenticated staff member |
| M11 | M10, M07 | Needs `scan_ref` from resolve, presets, booth derivation |
| M12 | M11 | Reuses `PointsService` and the fence |
| M13 | M11, M08 | Aggregates `points_log`; adds surfaces to the M08 shell |
| M14 | M13 | Invalidation signals a snapshot endpoint that must already exist |
| M15 | M02, M03, M05, M09, M14 | Fence, fixtures, sessions to revoke, streams to terminate |
| M16 | M09, M11 | Staff/admin roles and an audit trail to display |
| M17 | M08, M10, M13, M14 | Polishes existing surfaces only |
| M18 | M01–M17 | Verification gate |
| M19 | M18, M15 | Rehearses the verified system, including reset |

No cycle exists. See **§22 Implementation Order Validation** for the simulated chain.

---

# Milestones

## M01 — Project Foundation

### Objective
A running, empty, fully tooled modular monolith: Next.js frontend and FastAPI backend on PostgreSQL under Docker Compose, same-origin via the rewrite proxy, with lint, type checking and tests wired.

### Dependencies
None.

### Scope
- `frontend/` and `backend/` directories per `ARCHITECTURE.md` §5.
- Next.js App Router, TypeScript `strict: true`, Tailwind, PWA manifest scaffold.
- `next.config.ts` rewrite: `/api/:path*` → `BACKEND_INTERNAL_URL` (§3.2). The browser must never address the backend directly.
- FastAPI app factory, router registration point, exception-handler registration point (bodies land in M-later).
- Backend container pinned to **Python 3.12** regardless of host interpreter (§4).
- SQLAlchemy 2.x engine and session factory; Alembic initialised with the constraint naming convention (§7).
- `docker compose`: `db` (PostgreSQL 16), `backend`, `frontend`. Backend command pins **exactly one Uvicorn worker** (invariant §24.17) — this is a demo invariant and belongs in the compose file, not in tribal memory.
- `pydantic-settings` config. **Required secrets have no fallback default; startup fails loudly when they are missing** (invariant §24.14). `.env.example` with placeholders only; `.env` git-ignored.
- `/api/health` returning service and database reachability.
- Tooling: `ruff` + `mypy` (backend), `eslint` + `tsc --noEmit` (frontend), `pytest` with a PostgreSQL-backed fixture, `vitest`/Testing Library scaffold.
- `README.md`: startup, migration, test and lint commands.

### Expected Files / Areas
`frontend/`, `backend/app/{main,core,db}`, `backend/alembic/`, `docker-compose.yml`, `.env.example`, `README.md`, CI-style task scripts.

### Database Changes
None beyond an empty Alembic baseline revision.

### API / Contract Changes
`GET /api/health` only.

### Security / Correctness Invariants
- No secret literal in source; missing config fails at startup (§24.14).
- Single worker pinned (§24.17).
- Browser talks only to the frontend origin (§3.2).

### Tests
- **Unit:** config loader rejects missing required secrets.
- **Integration:** `/api/health` returns healthy against a real PostgreSQL container; Alembic upgrade/downgrade on a clean database.
- **Manual:** `docker compose up` from a clean clone on Windows; frontend reaches `/api/health` through the rewrite, not by direct port.

### Acceptance Criteria
1. Clean clone → `docker compose up` → all three services healthy.
2. `alembic upgrade head` succeeds on an empty database; `downgrade base` succeeds.
3. Frontend fetches `/api/health` same-origin; no CORS header is required for it.
4. Lint, type check and test commands all pass and are documented in `README.md`.
5. Removing a required env var causes a startup failure with a clear message, not a silent default.

### Explicitly Out of Scope
Any domain table, any business endpoint, auth, seeds, HTTPS/TLS termination (M10/M18).

### Demo vs Production Note
**Demo-complete; production-interface.** The compose topology is a demo deployment; the application structure is production-shaped.

---

## M02 — Database Foundation and Migrations

### Objective
Every entity in the locked architecture, with its real constraints, plus the two cross-cutting mechanisms that every later mutation depends on: the **shared reset fence in the unit-of-work helper** and the **advisory maintenance-mode check**.

### Dependencies
M01.

### Scope

Entities enumerated from the current `ARCHITECTURE.md` §7 and §8.2 — thirteen tables:

| # | Table | Source | Key structural requirements |
|---|---|---|---|
| 1 | `users` | §7.1, §8.1 | `UNIQUE(email)` on `citext`; `CHECK (sponsor_consent = false OR sponsor_consent_at IS NOT NULL)`; **`status` enum `PROVISIONAL`/`ACTIVE` and `activated_at`** per §8.1 with `CHECK (status='PROVISIONAL' OR activated_at IS NOT NULL)` — see clarification C1 |
| 2 | `interests` | §7.2 | `UNIQUE(slug)` |
| 3 | `user_interests` | §7.3 | `PRIMARY KEY (user_id, interest_id)`; index `(interest_id)`; FK cascade on user |
| 4 | `booths` | §7.4 | `UNIQUE(slug)`; `allow_repeat_task_points` **nullable** (NULL = inherit) |
| 5 | `booth_interests` | §7.5 | `PRIMARY KEY (booth_id, interest_id)`; `CHECK (weight > 0 AND weight <= 1)`; `numeric(3,2)` |
| 6 | `contact_cards` | §7.6 | `UNIQUE(public_token)`; **partial unique `(user_id) WHERE revoked_at IS NULL`** |
| 7 | `staff_accounts` | §7.7 | `UNIQUE(email)`; role enum; **`CHECK ((role='BOOTH_STAFF' AND booth_id IS NOT NULL) OR (role='ADMIN' AND booth_id IS NULL))`** |
| 8 | `sessions` | §7.8 | `UNIQUE(token_hash)`; `CHECK (num_nonnulls(user_id, staff_account_id) = 1)`; principal/FK agreement CHECK; index `(expires_at)` |
| 9 | `tasks` | §7.9, §7.12 | **`UNIQUE (id, booth_id)`** — exists solely to support the composite FK from `points_log` |
| 10 | `task_preset_points` | §7.10 | **Two partial unique indexes**: `(booth_id, points) WHERE booth_id IS NOT NULL` and `(points) WHERE booth_id IS NULL`; `CHECK (points > 0 AND points <= 100)` |
| 11 | `feedback` | §7.11 | `CHECK (rating BETWEEN 1 AND 5)`; indexes `(booth_id, created_at DESC)`, `(user_id, booth_id)`; **deliberately not unique on `(user_id, booth_id)`** |
| 12 | `points_log` | §7.12 | See below — the most constraint-dense table |
| 13 | `idempotency_records` | §7.12.1 | `PRIMARY KEY (staff_account_id, idempotency_key)`; **no `status` column** |
| 14 | `event_config` | §7.13 | `CHECK (id = 1)`; bounds CHECKs; `dataset_generation`, `maintenance_mode` |
| 15 | `otp_challenges` | §8.2 | `purpose` enum `ACTIVATION`/`LOGIN`; **partial unique `(email, purpose) WHERE consumed_at IS NULL`** |
| 16 | `consent_audit` | §7.1 | `user_id`, `granted`, `consent_version`, `changed_at`, `source` |

`points_log` specifically (§7.12):
- `entry_type` enum `AWARD`/`REVERSAL`; `reverses_points_log_id` **UNIQUE**, nullable.
- `CHECK (points <> 0)`.
- `CHECK ((entry_type='AWARD' AND points > 0 AND reverses_points_log_id IS NULL) OR (entry_type='REVERSAL' AND points < 0 AND reverses_points_log_id IS NOT NULL))`.
- `CHECK` binding `source` to `booth_id`, `entered_by_staff_id` and `entered_by_system` as written in §7.12.
- **Partial unique `(user_id, booth_id) WHERE source='FEEDBACK' AND entry_type='AWARD'`** — structural feedback-reward uniqueness.
- `UNIQUE(dedupe_key)` (nullable — TASK repeat policy only).
- Composite `FOREIGN KEY (task_id, booth_id) REFERENCES tasks (id, booth_id)`.
- Indexes `(user_id)`, `(user_id, created_at DESC)`, `(booth_id, created_at DESC)`.
- **No `idempotency_key` column** — that lives in `idempotency_records`.

Cross-cutting, and the reason this milestone is more than schema:

- **First migration runs `CREATE EXTENSION IF NOT EXISTS citext;`** (§7) before any `citext` column.
- **Named constraints throughout**, because errors are dispatched by `diag.constraint_name`, never by report order (§7.14, §24.24). Establish the naming map (`uq_points_feedback_user_booth`, `uq_points_dedupe_key`, `pk_idempotency_records`, `uq_contact_card_live_user`, `ck_staff_role_booth`, `fk_points_task_booth`, `uq_preset_global_points`, …) and a translation helper from constraint name → domain error.
- **Unit-of-work helper (`db/session.py`) that acquires `pg_advisory_xact_lock_shared(FACEMARK_RESET_FENCE)` as the first statement of every mutating transaction** (§19.4, §24.29), with `lock_timeout` configured. Every later mutation uses this context manager; the fence is never hand-written per endpoint. **Shipping it now, before any mutation exists, is deliberate — it removes the possibility of a later path forgetting it.**
- `FACEMARK_RESET_FENCE` constant defined once in `app/core/locks.py`.
- **Advisory maintenance-mode middleware** rejecting mutating routes with `503 MAINTENANCE` — explicitly a courtesy fast-path, never the authoritative gate (§19.4).
- Read-only transactions deliberately do **not** take the fence (§19.4 coverage table).

### Expected Files / Areas
`backend/app/modules/*/models.py`, `backend/app/db/{base,session}.py`, `backend/app/core/{locks,errors}.py`, `backend/alembic/versions/`.

### Database Changes
All of the above — the bulk of the schema, in reviewable migrations grouped by domain (identity, catalogue, staff, ledger, operations).

### API / Contract Changes
None.

### Security / Correctness Invariants
§24.11 (DB-enforced uniqueness), §24.24 (dispatch by constraint name), §24.29 (fence before first reset-sensitive read), §24.6 (structural feedback uniqueness), §24.22 (id is identity, not cursor — asserted in review, not code).

### Tests
- **Integration (PostgreSQL only):** every CHECK, partial unique index and composite FK rejects the state it is meant to forbid. Specifically: two global presets with the same `points` are rejected; two live contact cards for one user are rejected; a `BOOTH_STAFF` row without a booth is rejected; a `points_log` row referencing a task from another booth is rejected; a second `FEEDBACK` `AWARD` for the same `(user, booth)` is rejected; a `REVERSAL` for an already-reversed row is rejected.
- **Integration:** `alembic upgrade head` then `downgrade base` on a clean database; a second `upgrade` is clean (no drift).
- **Concurrency:** two concurrent transactions both taking the shared fence proceed without blocking each other; a session-scoped exclusive lock blocks both until released.
- **Unit:** constraint-name → domain-error map is total for every named constraint (an unmapped name raises rather than guessing).

### Acceptance Criteria
1. Every entity in `ARCHITECTURE.md` §7 and §8.2 exists with its documented constraints and indexes.
2. Each negative constraint test fails at the **database**, not in application code.
3. The unit-of-work helper demonstrably issues the advisory lock as the first statement of a mutating transaction (asserted by statement log or explicit lock-view query).
4. `citext` extension is created by migration, not assumed.
5. No `status` column on `idempotency_records`; no `idempotency_key` column on `points_log`.

### Explicitly Out of Scope
Business services, endpoints, seed content, the reset CLI (M15 — only the *mutation side* of the fence ships here).

### Demo vs Production Note
**Production-ready.** The schema and its constraints are the production schema.

---

## M03 — Seed and Fixture Infrastructure

### Objective
Deterministic, fictional AI-summit reference data that makes matching demonstrable and the demo repeatable, loadable idempotently.

### Dependencies
M02.

### Scope
- `app/seed/demo_fixtures.py` as the single declarative source: interests, booths, `booth_interests` weights, tasks, `task_preset_points` (global `+5/+10/+15`), `staff_accounts`, `event_config` defaults.
- `python -m app.cli seed` — idempotent upsert by natural key (slug/email). Safe to re-run.
- **Stable seeded `public_token` values** for demo attendees (§19.8) so printed QR cards and slide screenshots survive every reset.
- Demo attendees seeded as `status = ACTIVE` with `activated_at` set (§19.8) — a fixture property only; ordinary registration still requires verification.
- **Staff passwords come from environment variables with no fallback**; the CLI fails loudly if unset (§19.8). Never a literal.
- **Matching-friendly data design (§19.9), which is a deliverable of this milestone, not an afterthought:**
  - at least one attendee/booth pair with genuinely strong alignment (their selected interests are that booth's top-weighted ones) so a high score arises from a real match;
  - deliberate mid and low matches so the recommendation list visibly discriminates;
  - **expected scores computed by hand and asserted in M07's tests** — if a seed change moves a headline number, a test fails rather than the discrepancy surfacing on stage.

### Expected Files / Areas
`backend/app/seed/`, `backend/app/cli.py`.

### Database Changes
None (data only).

### API / Contract Changes
None.

### Security / Correctness Invariants
- No password, secret or token literal in fixtures except the deliberately fixed demo `public_token`s, which are safe only under the demo-mode guard (§19.8).
- Fixed demo tokens must be unreachable in production — enforced by the M15 guards; until then, documented.

### Tests
- **Integration:** seed → re-seed produces no duplicates and no constraint violations.
- **Integration:** seeded `booth_interests` weights satisfy `CHECK (weight > 0 AND weight <= 1)`.
- **Unit:** fixture set contains at least one strong, one mid and one low expected match (asserted structurally here; numerically in M07).

### Acceptance Criteria
1. `python -m app.cli seed` on a clean migrated database populates the full catalogue.
2. Running it twice changes nothing and errors on nothing.
3. Demo attendee card tokens are byte-identical across runs.
4. Seeding fails with a clear message when staff password env vars are absent.

### Explicitly Out of Scope
Destructive reset, truncation, `dataset_generation` advancement, maintenance mode — all M15. This milestone only *creates*.

### Demo vs Production Note
**Demo-complete.** Fixture content is demo data; the seeding mechanism is reusable for production reference data.

---

## M04 — Registration and Attendee Activation

### Objective
The `PROVISIONAL → OTP → ACTIVE` lifecycle (§8.1), with auditable consent — and, critically, **no authenticated session at registration**.

### Dependencies
M02, M03.

### Scope
- `POST /api/attendees/register`: first name, last name, email, phone, profession, optional LinkedIn, interest slugs, sponsor consent.
- One transaction creates the `PROVISIONAL` user, `user_interests`, the `consent_audit` row and the `contact_card` (inert until activation), then issues an `ACTIVATION` OTP challenge.
- **Returns no session.** The response says only that a code was sent (§8.1).
- `sponsor_consent` is a **required explicit boolean with no default**; absence is a validation error. The `users` row and its `consent_audit` row are written in the **same transaction** (§18 audit atomicity).
- Interests resolved **by slug against active rows**; unknown slug → `UNKNOWN_INTEREST`, never silently dropped.
- **Duplicate-email behaviour (§8.1), all three branches returning an identical response body** so the endpoint is not a membership oracle: none → create provisional + activation OTP; `PROVISIONAL` → overwrite the provisional profile and re-send; `ACTIVE` → create nothing and send a **login** OTP to the registered address, discarding submitted fields.
- `otp_challenges` per §8.2: 6-digit `secrets.randbelow(1_000_000)`, **Argon2id hash only**, 10-minute expiry enforced in SQL, single use via `SELECT … FOR UPDATE` then `UPDATE … WHERE consumed_at IS NULL` with row-count check, 5-attempt cap incremented **inside the same locked transaction**, partial unique `(email, purpose) WHERE consumed_at IS NULL` for supersession.
- `POST /api/auth/otp/verify` → **activation transaction**: consume challenge, set `status=ACTIVE` + `activated_at`, activate the card, create the session — all or nothing.
- **OTP delivery adapter** with a production-shaped interface. Demo implementation writes to the server log and surfaces in the admin console, gated on `DEMO_MODE=true`; config raises at startup if `DEMO_MODE=true` while `ENVIRONMENT=production` (§8.2).
- `python -m app.cli purge-provisional` — removes `PROVISIONAL` users older than 24 h with their interests, cards and challenges (§8.1). Idempotent.
- Every mutation here uses the M02 unit-of-work fence helper.

### Expected Files / Areas
`backend/app/modules/attendees/`, `backend/app/modules/auth/`, frontend registration and OTP-entry routes.

### Database Changes
None new (M02 created the tables); this milestone exercises them.

### API / Contract Changes
`POST /api/attendees/register`, `POST /api/auth/otp/request`, `POST /api/auth/otp/verify`.

### Security / Correctness Invariants
- §24.25: no attendee capability before email ownership is proven.
- §24.13: OTP codes stored only as hashes.
- §24.16: `extra="forbid"` on every mutating schema.
- §24.29: fence before the first reset-sensitive read, including the email-uniqueness lookup.
- Non-enumerating responses on all three duplicate-email branches.

### Tests
- **Unit:** consent absence rejected; unknown interest slug rejected; LinkedIn shape validation.
- **Integration:** registration creates `PROVISIONAL` **and returns no `Set-Cookie` session**; activation flips to `ACTIVE` and issues one; a failed activation leaves `status` unchanged and no session.
- **Integration:** all three duplicate-email branches return byte-identical bodies; the `ACTIVE` branch creates no user and sends a `LOGIN` OTP.
- **Concurrency (PostgreSQL):** two simultaneous correct OTP submissions for one challenge — exactly one activates, the other gets `OTP_INVALID`; five parallel wrong guesses do not exceed the attempt cap by racing on a stale counter.
- **Integration:** expired, consumed and attempt-exhausted codes all return the same `OTP_INVALID`.
- **Integration:** `purge-provisional` frees the email for re-registration.

### Acceptance Criteria
1. A registered-but-unverified attendee has **no session, no resolvable card, no matching, no leaderboard presence**.
2. Registering with an already-`ACTIVE` email creates nothing and is indistinguishable in response from a fresh registration.
3. OTP row stores only a hash; the plaintext code appears nowhere in the database.
4. Concurrency tests pass against real PostgreSQL.

### Explicitly Out of Scope
Real email delivery, login-by-OTP for returning attendees beyond issuing the challenge (M05 completes the login surface), profile editing.

### Demo vs Production Note
**Production-interface with demo adapter.** Lifecycle, hashing, expiry, single-use and attempt limits are production logic; only the delivery channel is an adapter.

---

## M05 — Attendee Authentication and Session Security

### Objective
Complete the passwordless session surface and ship the cross-cutting request-security layer: cookies, CSRF, rate limiting and trusted-proxy resolution.

### Dependencies
M04.

### Scope

**Boundary with M04, stated to avoid duplicated work:** M04 owns challenge *issuance and verification mechanics* and the activation transaction. M05 owns the **returning-attendee login surface**, the **session lifecycle** (cookie, expiry, logout, revocation) and the **request-security middleware** that protects both.

- Opaque session tokens: `secrets.token_urlsafe(32)`, **only the SHA-256 hash stored** (§8.3, §24.13).
- Cookie: `HttpOnly; SameSite=Lax; Path=/; Secure when HTTPS; Max-Age 12h`.
- Logout revokes server-side (`revoked_at`) so the token dies immediately.
- **Session death propagates to open SSE streams** — the contract is defined here (re-validate each heartbeat, emit `session_invalid`, close); the stream side is implemented in M14 (§8.3, §17.9).
- **CSRF (§8.3.1):** `SameSite=Lax` plus a server-side **Origin assertion** on every mutating request; mismatch or absence → `CSRF_REJECTED` (403). No CSRF token table.
- **Rate limiting (§21.7) — per identity primary, per-IP ceilings high**, because a venue shares one NAT address (§24.26):

  | Endpoint | Primary (per identity) | Shared-IP ceiling |
  |---|---|---|
  | `POST /api/auth/otp/request` | 3 / 15 min per normalised email | 600 / 15 min |
  | `POST /api/auth/otp/verify` | 5 per challenge + 10 / 15 min per email | 1200 / 15 min |
  | `POST /api/staff/auth/login` | 10 / 15 min per account + lockout | 600 / 15 min |
  | `POST /api/attendees/register` | `UNIQUE(email)` + the `ACTIVE`-email branch | 600 / hour |
  | `GET /api/cards/{token}` | 30 / min **per token** | 1200 / min |
  | `POST /api/staff/scan/resolve` | 120 / min per staff account | — |
  | `POST /api/staff/points/award` | **not limited** | — |

- **Trusted-proxy resolution (§21.7.1):** ignore forwarding headers unless the peer is in `TRUSTED_PROXY_CIDRS`; walk `X-Forwarded-For` **from the right**, discarding trusted hops; default empty (trust nothing). **Taking the leftmost entry is prohibited** — it is the spoofable choice.
- Demo limiter is an in-process fixed-window counter, consistent with the single-worker invariant; the interface is storage-agnostic.

### Expected Files / Areas
`backend/app/core/{deps,security,ratelimit,proxy}.py`, `backend/app/modules/auth/`, frontend login route and session handling.

### Database Changes
None.

### API / Contract Changes
`POST /api/auth/logout`, `GET /api/attendees/me`; middleware applies to all existing mutating routes.

### Security / Correctness Invariants
§24.13, §24.26, CSRF policy §8.3.1, trusted-proxy semantics §21.7.1.

### Tests
- **Unit:** XFF resolution — untrusted peer ignores headers entirely; trusted peer walks right; a client-supplied `X-Forwarded-For` **cannot** change its own rate-limit identity.
- **Unit:** rate-limit windows per identity and per IP independently.
- **Integration:** logout immediately invalidates; an expired session is rejected; a mutating request with a foreign or missing `Origin` is rejected with `CSRF_REJECTED`.
- **Integration (venue NAT simulation):** 200 distinct emails registering from one source IP all succeed — the regression that per-IP limiting would have caused.
- **Integration:** session cookie is `HttpOnly` and not readable from JS.

### Acceptance Criteria
1. Spoofed `X-Forwarded-For` from an untrusted peer has zero effect on limiting.
2. 200 registrations from one IP succeed; 4 OTP requests for one email in 15 minutes do not.
3. Logout kills the session server-side, not just client-side.
4. Every mutating route enforces the Origin assertion.

### Explicitly Out of Scope
Staff login (M09), SSE stream teardown implementation (M14), shared rate-limit storage (production evolution).

### Demo vs Production Note
**Production-interface with demo adapter** — only the limiter's storage is in-process.

---

## M06 — Contact Card and Dual-Purpose QR

### Objective
One opaque token serving two consumers with two deliberately different response shapes.

### Dependencies
M04, M05.

### Scope
- `QrTokenService`: `secrets.token_urlsafe(32)` (256-bit), **not derived from any attendee attribute** (§9.1).
- QR payload is **only** `https://<host>/c/<public_token>` — no name, email, interests, JSON or internal id (§9.2, §24.4).
- `GET /api/cards/{token}` (unauthenticated) → first name, last name, profession, interests, and **exactly one server-chosen contact action** (§9.3.1):
  - LinkedIn present → `{"type":"LINKEDIN","value":…}`;
  - otherwise → `{"type":"EMAIL","value":…}`.
  - **Phone is absent from the schema entirely. Sponsor-consent state is never exposed.** When LinkedIn exists, email is not in the response at all — the client is never sent both and asked to choose (§24.19).
- `POST /api/staff/scan/resolve` (staff session required) → **first name, last name, profession only**, plus `scan_ref`. Widening the public card never widens this (§24.20). *(The `scan_ref` envelope itself is specified in M10, where it is consumed.)*
- **Invalid token handling (§9.4):** malformed, unknown, revoked, belonging to a `PROVISIONAL` attendee, or deleted all return the **identical** `404 INVALID_QR`. No branch is distinguishable.
- Attendee "My QR" screen rendering the code client-side from the token.
- **Privacy protections (§21.11):** application access log masks `/c/***`; card pages send `Referrer-Policy: no-referrer` so the token does not leak to LinkedIn on tap; `X-Robots-Tag: noindex` and `robots.txt` exclusion (§9.5). **Proxy-level log masking is an infrastructure requirement recorded for M18.**
- Rotation/revocation is **schema-ready but not built** (§9.6): `revoked_at` and the partial unique index exist from M02; resolution filters `revoked_at IS NULL`. No UI.

### Expected Files / Areas
`backend/app/modules/contact_cards/`, frontend `/c/[token]` and attendee "My QR" routes.

### Database Changes
None.

### API / Contract Changes
`GET /api/cards/{token}`, `POST /api/staff/scan/resolve`.

### Security / Correctness Invariants
§24.4 (no PII/ids in QR), §24.19 (no phone, no consent state publicly), §24.20 (staff sees name + profession only), §9.4 (indistinguishable failures), §9.5 (enumeration resistance).

### Tests
- **Unit:** token is 43 chars, URL-safe, and uncorrelated with user id across a large sample.
- **Unit:** contact-action selection — LinkedIn present → LinkedIn and **no email field**; absent → email.
- **Integration:** public card response contains no `phone`, no `sponsor_consent`, no `user_id`, no `email` when LinkedIn exists.
- **Integration:** staff resolve returns exactly three identity fields.
- **Integration:** all five invalid-token cases produce byte-identical `404 INVALID_QR`.
- **Integration:** a `PROVISIONAL` attendee's token does not resolve on either endpoint.
- **Integration:** response carries `Referrer-Policy: no-referrer` and `X-Robots-Tag: noindex`.

### Acceptance Criteria
1. Scanning a demo QR with a phone camera opens a working public card over HTTPS.
2. No response on either path contains phone, consent state or an internal id.
3. The five invalid cases are indistinguishable by body, status and timing class.
4. Provisional attendees are unscannable.

### Explicitly Out of Scope
`scan_ref` construction and verification (M10), awarding (M11), rotation UI.

### Demo vs Production Note
**Production-ready**, except that proxy log masking is an infrastructure task verified in M18.

---

## M07 — Booths, Interests and Matching

### Objective
The deterministic, explainable matching engine — implemented exactly, with fixed-vector tests.

### Dependencies
M02, M03.

### Scope
- `MatchingService` with a **pure core**: `score(attendee_interest_ids, booth_weights, config) -> MatchResult`, no database access, unit-testable with literals (§11.2).
- **The locked formula (§11.3), implemented verbatim:**
  ```
  raw      = Σ_{i ∈ M} w(i)          where M = A ∩ B
  coverage = raw / |A|
  focus    = raw / Σ_{i ∈ B} w(i)
  score01  = 0.7·coverage + 0.3·focus
  score    = round_half_up(100 · score01)
  ```
- **Rounding semantics are a named implementation trap (§11.3):** Python's `round()` is banker's rounding and **must not be used**. Use `Decimal(...).quantize(0, ROUND_HALF_UP)` or `floor(x + 0.5)`. Weights are handled as `Decimal` matching `numeric(3,2)` to avoid float drift.
- **Edge cases (§11.4):** no interests selected → `match: null` with a prompt, **never 0%**; `M = ∅` → score 0, excluded from recommendations but visible in the full list; booth with no interest rows → excluded entirely; interest changes recompute on read (nothing cached).
- **Explanation (§11.5):** deterministic template over **actual** matched interests, ordered by `w(i)` desc then name asc, at most three named. Bands: ≥80 Strong, 60–79 Good, 40–59 Partial, 1–39 Some overlap. **No LLM anywhere.**
- **Ordering (§11.6):** `score DESC, booth.name ASC, booth.id ASC` — total and stable.
- Response contract exactly per §11.7 including `algorithm_version: "weighted-v1"`.
- Endpoints: booth list, booth detail, recommendations for the authenticated attendee.

### Expected Files / Areas
`backend/app/modules/{matching,booths,interests}/`.

### Database Changes
None.

### API / Contract Changes
`GET /api/booths`, `GET /api/booths/{slug}`, `GET /api/attendees/me/recommendations`.

### Security / Correctness Invariants
§24.5 — computed, never random, never LLM; **the displayed score is exactly the service's output, with no transform at any layer**. Scores are computed server-side only; the frontend never calculates one.

### Tests
- **Unit (fixed vectors):** the architecture's worked example must yield **exactly 78** — `AIWorks` (AI 1.0, GenAI 1.0, DA 0.8, Auto 0.7), attendee `{GenAI, DA}` → `raw 1.8`, `coverage 0.900`, `focus 0.514`, `score01 0.784`, **78**. This is the canonical fixture.
- **Unit:** half-way rounding cases prove `ROUND_HALF_UP`, not banker's rounding.
- **Property-based:** score ∈ [0,100] for all valid inputs, with no clamping (§11.3).
- **Unit:** determinism — same input yields the same score and the same explanation ordering across repeated runs.
- **Unit:** explanation names only interests in `M`; ordering ties broken by name.
- **Unit:** all four edge cases behave as specified; `match: null` is distinct from `score: 0`.
- **Integration:** the **seed fixtures' expected scores are asserted numerically** (§19.9), so a seed change that moves a headline number fails a test.

### Acceptance Criteria
1. The canonical fixture returns exactly 78.
2. No random source, no LLM call and no multiplier appears anywhere in the matching path — verified by inspection and by determinism tests.
3. Recommendation ordering is byte-stable across repeated requests.
4. Seeded demo pairs produce their hand-computed scores.

### Explicitly Out of Scope
Embeddings/pgvector (`embedding-v1` is a documented future path, §11.8), caching, attendee interest editing UI.

### Demo vs Production Note
**Production-ready.** The contract is deliberately shaped so an embedding implementation can replace the core without a frontend change.

---

## M08 — Attendee Dashboard

### Objective
A real, backend-connected mobile-first attendee experience for everything that exists at this point in the chain.

### Dependencies
M05, M06, M07.

### Scope
- Mobile navigation: **Home · Booths · My QR · Leaderboard · Profile**.
- Home: welcome/profile summary and matched booths.
- Booths: recommendation list with score, matched interests and explanation; booth detail with tasks and location.
- My QR: the card from M06.
- Profile: registration data, selected interests, consent state (private view only).
- Loading, empty and error states on every surface; error copy driven by the `code` → message map (§20.4) with a generic fallback for unrecognised codes.
- **No hardcoded business data** — every value comes from the API.

**Dependency-driven boundary, stated explicitly:** score, rank and the leaderboard require `points_log` (M11) and the aggregation (M13), which are later in the chain. M08 therefore ships the **Leaderboard tab and the score surface as defined, disabled placeholders** that render an honest "not yet available" state, and **M13 wires them to real data**. This is an ordering adjustment, not an architecture change: it avoids M08 depending on functionality scheduled later, and avoids inventing fake numbers to fill the gap.

### Expected Files / Areas
`frontend/src/app/(attendee)/`, `frontend/src/{components,lib,types}`.

### Database Changes
None.

### API / Contract Changes
None new — consumes M05/M06/M07 contracts. TypeScript types generated from OpenAPI.

### Security / Correctness Invariants
§24.2 — hidden controls are never a security measure; §24.9 — no business logic in components; scores rendered exactly as received (§24.5).

### Tests
- **Component:** recommendation card renders score, matched interests and explanation from props; `match: null` renders the prompt, not 0%.
- **Component:** loading/empty/error states for each surface.
- **Integration:** unauthenticated access to attendee routes redirects to login; a `PROVISIONAL` user cannot reach the dashboard.
- **Manual/device:** iPhone Safari and Android Chrome at 375 px — no horizontal scroll, touch targets ≥ 44 px.

### Acceptance Criteria
1. Every displayed value originates from an API response.
2. All five tabs render correctly on a 375 px viewport.
3. Score/leaderboard placeholders state honestly that data is not yet available — no zeros presented as real, no mock data.
4. `tsc --noEmit` and `eslint` pass.

### Explicitly Out of Scope
Visual polish (M17), realtime updates (M14), live score/rank/leaderboard (M13).

### Demo vs Production Note
**Demo-complete** for the surfaces it owns.

---

## M09 — Staff Authentication and Booth Isolation

### Objective
Staff and admin accounts with **no self-registration**, and the four-layer booth invariant proven by negative tests.

### Dependencies
M02, M03.

### Scope
- Accounts created by seed/CLI or admin only. **No staff registration endpoint exists anywhere in the API surface** (§12.1).
- **Argon2id** password hashing (`argon2-cffi`, `t=2, m=64 MiB, p=1` tuned to ~100 ms), `verify_and_update` on parameter change (§21.1).
- `POST /api/staff/auth/login` → `INVALID_CREDENTIALS` with **no distinction** between unknown email and wrong password.
- **Brute force (§21.8):** `failed_login_count`; at 10 failures `locked_until = now() + 15 min`, returning `ACCOUNT_LOCKED` **without evaluating the password**; reset on success; constant-time comparison.
- Session issued exactly as M05, `principal_type = STAFF`.
- Dependencies `current_booth_staff` and `current_admin`; admin/staff distinction comes from `staff_accounts.role` — one role source (§7.8).
- `GET /api/staff/me` returning booth name so staff always see which booth they are awarding for.
- **`GET /api/staff/points/presets`** returning the resolved allow-list for **the authenticated staff member's booth** (§13.2) — presets are never hardcoded in the frontend, which would create a second source of truth.
- **Booth authority is derived from the session**, never from input (§12.2, §24.3).

### Expected Files / Areas
`backend/app/modules/staff/`, `backend/app/core/deps.py`, frontend `/staff/login`.

### Database Changes
None.

### API / Contract Changes
`POST /api/staff/auth/login`, `POST /api/staff/auth/logout`, `GET /api/staff/me`, `GET /api/staff/points/presets`.

### Security / Correctness Invariants
§24.3 (booth from session), §12.2 four-layer enforcement, §12.3 role table — **admins deliberately cannot award points**, §21.1, §21.8.

### Tests
- **Negative (required):** no route exists that creates a staff account from an unauthenticated request — asserted against the generated OpenAPI schema.
- **Negative (required):** staff A cannot read or act on booth B's resources; `current_booth_staff` rejects attendee sessions, admin-only accounts and inactive accounts.
- **Unit:** password verification is constant-time; hashes are Argon2id; no plaintext is ever persisted or logged.
- **Integration:** lockout triggers at 10 failures and short-circuits password evaluation; success resets the counter.
- **Integration:** `presets` returns the caller's booth presets, falling back to globals, and never another booth's.

### Acceptance Criteria
1. The OpenAPI schema contains no staff self-registration operation.
2. Cross-booth access attempts fail with `STAFF_NOT_AUTHORIZED` in every tested shape.
3. Login failures are indistinguishable between unknown email and wrong password.
4. An admin account cannot reach any award route.

### Explicitly Out of Scope
Scanner UI (M10), awarding (M11), admin console (M16), MFA (production evolution).

### Demo vs Production Note
**Production-ready.**

---

## M10 — Staff QR Scanner and Confirmation

### Objective
Real camera scanning on a phone, mandatory human confirmation, and `scan_ref` issuance — with an explicit physical-device HTTPS verification.

### Dependencies
M06, M09.

### Scope
- Staff panel showing the bound booth; **SCAN ATTENDEE** opens the camera.
- In-browser QR decode; token extracted client-side.
- `POST /api/staff/scan/resolve` → name + profession + `scan_ref`.
- **Mandatory confirmation screen** — *"Attendee found — {name} · {profession}. Is this the correct attendee?"* `[Cancel] [Confirm]`. **There is no endpoint that scans and awards in one step** (§13.1); a misread QR must never cause an immediate irreversible award.
- Preset buttons rendered from `GET /api/staff/points/presets` (§13.2) — never hardcoded.
- **`scan_ref` creation (§13.3), exactly as specified:**
  - envelope `P = {v, sid, ses, tok = sha256(card_token), iat}` with **server-issued** `iat`;
  - canonical JSON — keys sorted, no insignificant whitespace, UTF-8 — base64url **unpadded** → `B`;
  - `sig = base64url_nopad(HMAC_SHA256(SCAN_REF_SECRET, B))`; `scan_ref = B + "." + sig`;
  - **the HMAC covers `B`, the exact transmitted string**, not a re-serialization;
  - `SCAN_REF_SECRET` from config with no fallback default.
- **Frontend transport is opaque (§13.3 simplicity contract):** the client stores the string and echoes it. **No parsing, no inspection, no expiry timer, no countdown.** Expiry is learned only from a server `SCAN_EXPIRED`, handled with one message — *"That scan expired. Scan again."*
- **UI error handling:** camera permission denied (`NotAllowedError`) and no camera (`NotFoundError`) → `CAMERA_PERMISSION_DENIED` with recovery instructions; malformed QR; invalid token; unknown/inactive attendee; repeated scans; network errors; cancel/rescan.
- **Manual token entry is development and emergency fallback only (§13.5, §24.21)** — reachable only after a camera failure, never the default path, never presented as the normal flow.
- The client generates the award `idempotency_key` (UUIDv4) **when the confirmation screen opens**, not per click (§13.4).

### Expected Files / Areas
`frontend/src/app/staff/`, `backend/app/modules/staff/` (resolve + `scan_ref` issuance), `backend/app/core/security.py`.

### Database Changes
None.

### API / Contract Changes
`POST /api/staff/scan/resolve` gains `scan_ref` in its response.

### Security / Correctness Invariants
§24.20 (minimum identity), §24.21 (camera primary, manual fallback only), §13.3 (envelope, canonical serialization, server-issued `iat`), §21.11 (`scan_ref` never logged).

### Tests
- **Unit:** canonical JSON is byte-stable across runs and key insertion orders; HMAC is computed over the transmitted `B`.
- **Unit:** `tok` is a hash, never the token; `scan_ref` appears in no log sink.
- **Integration:** resolve requires a staff session; returns exactly three identity fields plus `scan_ref`.
- **Component:** confirmation screen cannot be bypassed; preset buttons come from the API; manual entry is not reachable without a prior camera failure.
- **Manual/device — milestone gate:** see below.

**M-CAM (required device gate, §13.5).** On a **physical iPhone (Safari)** *and* a **physical Android device (Chrome)**, over the **actual presentation network and HTTPS origin**: load the scanner, grant camera permission, scan a **printed** attendee QR, and complete resolve → confirm. A desktop browser, a simulator or a screen-displayed QR **does not satisfy this gate**. It is scheduled with enough lead time to change the HTTPS mechanism if a target device fails.

### Acceptance Criteria
1. M-CAM passes on both physical platforms over HTTPS.
2. Awarding is impossible without an explicit human confirmation action.
3. `scan_ref` is opaque to the client — no client code parses it or times it.
4. Camera denial produces a recoverable, explained state.

### Explicitly Out of Scope
The award itself (M11), `scan_ref` **verification** ordering (M11 — issuance only here), HTTPS mechanism selection (infrastructure, verified M18).

### Demo vs Production Note
**Production-ready**, given an HTTPS origin. The HTTPS mechanism is deliberately replaceable (§13.5).

---

## M11 — Task Award and Points Ledger

> **HIGH-RISK MILESTONE.** The highest concentration of concurrency and authorization correctness in the system. Its full adversarial test suite ships **in this milestone**, not later.

### Objective
`PointsService.award_task()` implementing the locked stage ordering exactly, with `points_log` as the sole source of truth.

### Dependencies
M07, M10.

### Scope

**The canonical request contract (§12.2) — the only authoritative definition:**
```python
class AwardTaskPointsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_token: str
    points: int
    idempotency_key: UUID
    scan_ref: str
    # deliberately absent: booth_id, staff_id, source, task_id, timestamp
```
`booth_id`, `entered_by_staff_id`, `source` and `created_at` are **server-derived and cannot be expressed by a client** (§24.3, §24.16).

**Stage ordering, implemented verbatim (§13.4, §14.3):**

*Stage A — pre-fence, pure, no database:*
1. `A1` parse and schema-validate.
2. `A2` extract the session cookie — **a string, not an authenticated identity**.
3. `A3` `scan_ref` structural + cryptographic checks (§13.3 checks 1–4: split, HMAC verify, version, **future-`iat` rejection with 5 s skew — treated as tampering, never as expiry**). **Expiry is NOT checked here.**
4. `A4` `scan_ref` token binding, `sha256(card_token) == tok` (check 7).
5. `A5` extract signed values; compute the request fingerprint `sha256(canonical_json({v, tok, points}))` — deliberately **excluding** `scan_ref`, booth and all configuration.
6. `A6` maintenance fast-path — **advisory only**, never the authoritative gate.

*Stage B — one transaction, fence first:*
- `B0` **`pg_advisory_xact_lock_shared(FACEMARK_RESET_FENCE)` as the first statement, before any reset-sensitive read** (§24.29).
- `B1` **authoritative** session → staff account → active; `scan_ref` sid/ses binding (checks 5–6).
- `B2` `booth_id = staff.booth_id`.
- `B3` **idempotency lookup** — match → return the stored snapshot with `replayed: true` and **stop, without evaluating freshness**; fingerprint differs → `IDEMPOTENCY_KEY_REUSE`.
- `B3a` **`scan_ref` expiry (check 8) — new awards only**, reached only when B3 found no record.
- `B4` resolve token → attendee, require `ACTIVE`.
- `B5` `points ∈ presets(booth)` — **allow-list membership, not a range check**; `12` is rejected though it lies between 10 and 15.
- `B6` repeat policy → `dedupe_key` (§14.5).
- `B7` claim the idempotency key — **first write**.
- `B8` insert `points_log`; violations dispatched **by constraint name**.
- `B9` compute the resulting total.
- `B10` build the complete canonical response.
- `B11` persist it as the snapshot.
- `B12` **COMMIT** — award, claim and snapshot become visible together; the fence releases here.

*Stage C — after commit only:* `C1` publish the realtime invalidation signal (best-effort, never fails the award — the signal consumer arrives in M14); `C2` return.

**Concurrency — wait-then-replay (§13.4):** a second request with the same key blocks on `pk_idempotency_records`; when the first commits, it re-reads the **complete** record and replays. **No `IN_PROGRESS` state exists.** `AWARD_IN_PROGRESS` is a `lock_timeout` safety valve only, never the normal contract.

**Response snapshot (§13.4):** `points_log_id`, `points_awarded`, `resulting_total`, `attendee_name`, `booth_name`, `awarded_at`, `replayed`. **`rank` is deliberately excluded** — it is not an immutable property of an award.

**No crash-recovery branch and no heuristic recovery lookup.** Both were removed from the architecture; neither may reappear.

`points_log` is **append-only** in application code; corrections are `REVERSAL` rows (§24.15).

### Expected Files / Areas
`backend/app/modules/points/`, frontend staff award UI.

### Database Changes
None new — M02 created `points_log` and `idempotency_records`.

### API / Contract Changes
`POST /api/staff/points/award`.

### Security / Correctness Invariants
§24.1, §24.3, §24.7, §24.11, §24.15, §24.16, §24.23, §24.24, §24.29.

### Tests

**Unit**
- Fingerprint excludes `scan_ref`, booth and configuration.
- Response snapshot contains no `rank`.
- Constraint-name dispatch maps each violation to its own domain error.

**Integration**
- Valid `+5`, `+10`, `+15` awards create exactly one ledger row each.
- Arbitrary values (`12`, `0`, `-5`, `1000`) → `INVALID_POINT_VALUE`.
- `INVALID_QR` for unknown/revoked tokens; `PROVISIONAL` attendee rejected.
- Request body containing `booth_id` → `422` (schema cannot express it); resulting row always carries the staff member's own booth.
- Tampered `scan_ref` → `INVALID_QR`; forged future `iat` → `INVALID_QR`, no row.
- `scan_ref` valid for the account but a **different session** → `STAFF_NOT_AUTHORIZED`.
- **Expired `scan_ref` on a NEW award → `SCAN_EXPIRED`, no row.**
- **Expired `scan_ref` on a COMPLETED replay → the original success is replayed, `replayed: true`, still exactly one row.** *(The regression this ordering exists to prevent.)*
- Same key + same payload → replay, one row.
- Same key + different `points` → `IDEMPOTENCY_KEY_REUSE`; same key + different attendee → same.
- Same key + **different staff account** → a separate record, proceeds normally.
- Repeat policy: second award to the same attendee/booth with `allow_repeat=false` → `DUPLICATE_TASK_REWARD`; with `allow_repeat=true` → permitted.
- Transaction rollback leaves **no** award and **no** claimed key.

**Concurrency (real PostgreSQL)**
- Two simultaneous identical requests → exactly one ledger row; the second replays the first's snapshot.
- Two simultaneous requests with different keys for the same attendee/booth under `allow_repeat=false` → one succeeds, one `DUPLICATE_TASK_REWARD`, and the loser leaves no orphaned idempotency record.
- Response-lost simulation: commit, discard the response, retry → replay, one row.
- **Fence:** an award holding the shared fence blocks an exclusive acquire until it finishes; an award cannot perform any reset-sensitive read before acquiring the fence (asserted via statement ordering).

### Acceptance Criteria
1. Every test above passes against real PostgreSQL.
2. `git grep` finds no `IN_PROGRESS`, no recovery-by-`(staff, token, points, window)`, and no client-supplied `booth_id` path.
3. The ledger total equals `SUM(points)` under every tested sequence; no denormalised total exists anywhere.
4. Expired-`scan_ref`-on-replay returns the original success.

### Explicitly Out of Scope
Feedback rewards (M12), leaderboard (M13), realtime consumption (M14), correction/reversal UI.

### Demo vs Production Note
**Production-ready.**

---

## M12 — Feedback and Automatic Reward

### Objective
Feedback submission with a structurally-once automatic reward per `(attendee, booth)`.

### Dependencies
M11.

### Scope
- `POST /api/attendees/feedback` — booth slug, rating 1–5, optional comment.
- **One transaction (§15):** fence → authoritative auth (attendee session, `ACTIVE`) and booth resolution → insert feedback (always succeeds) → `SAVEPOINT` → attempt the reward insert → on violation of `uq_points_feedback_user_booth`, roll back **to the savepoint only** so the feedback row still commits → recompute total → commit → publish signal only if points were awarded.
- Reward amount from `event_config.feedback_points`; `entered_by_system = true`, `entered_by_staff_id = NULL`, `dedupe_key = NULL` (feedback uniqueness is structural, §14.5).
- **Duplicate response contract (§15) — one shape, both cases, both `200`:**
  ```jsonc
  { "feedback_id": 91, "awarded_points": 5, "already_rewarded": false, "total_points": 50 }
  { "feedback_id": 92, "awarded_points": 0, "already_rewarded": true,  "total_points": 50 }
  ```
  `DUPLICATE_FEEDBACK` exists in the catalogue but **is not returned by this endpoint**.
- Multiple feedback submissions are **allowed and stored**; only the reward is de-duplicated.
- **No "check then insert" path anywhere** — that pattern races (§15).

### Expected Files / Areas
`backend/app/modules/feedback/`, frontend booth feedback form.

### Database Changes
None.

### API / Contract Changes
`POST /api/attendees/feedback`.

### Security / Correctness Invariants
§24.6 (DB-enforced, not application-checked), §24.11, §24.29 (fence before the attendee lookup), `ACTIVE` gating.

### Tests
- **Integration:** first submission awards; second submission stores feedback and returns `awarded_points: 0, already_rewarded: true` with `200`.
- **Integration:** a `PROVISIONAL` attendee cannot submit.
- **Concurrency (real PostgreSQL):** two simultaneous first-submissions → exactly one reward row, **both feedback rows committed**.
- **Integration:** reward row satisfies the `source='FEEDBACK'` CHECK triple and carries `dedupe_key = NULL`.
- **Integration:** reversing a feedback award does **not** free the partial unique index — the attendee remains ineligible for a second reward at that booth; the `REVERSAL` only adjusts net points.
- **Unit:** frontend button disabling is cosmetic — the server refuses regardless.

### Acceptance Criteria
1. First eligible feedback submission at a booth succeeds and is awarded.
2. Duplicate feedback at the same booth does not produce another reward (`200`, `already_rewarded: true`, never an error).
3. Reversal of the original reward adjusts the attendee's net points via the append-only ledger.
4. Feedback submitted again after that reversal still does **not** produce another reward — reward eligibility is lifetime once-per-`(attendee, booth)`, not restored by reversal.
5. Feedback text is never lost because the reward was refused.

### Explicitly Out of Scope
Feedback moderation, per-booth reward amounts, analytics.

### Demo vs Production Note
**Production-ready.**

---

## M13 — Leaderboard and Attendee Snapshot

### Objective
Leaderboard and personal score/rank derived from `points_log`, with the deterministic ordering the demo depends on — and the snapshot endpoint realtime will later invalidate.

### Dependencies
M11, M08.

### Scope
- Aggregation exactly per §16.1:
  ```sql
  SELECT u.id, …, COALESCE(SUM(p.points),0) AS total_points, MAX(p.created_at) AS last_award_at
  FROM users u LEFT JOIN points_log p ON p.user_id = u.id
  WHERE u.status = 'ACTIVE'
  GROUP BY u.id
  ```
  The **`LEFT JOIN` keeps zero-point attendees rankable**; the **`status` filter is required, not cosmetic** — without it an unverified registration made with someone else's email would appear publicly under that person's name.
- **Ordering (§16.2):** `total_points DESC, last_award_at ASC NULLS LAST, u.id ASC` — total, so two phones never disagree. Displayed rank uses `RANK() OVER (ORDER BY total_points DESC)`, so equal totals share a rank while list order stays deterministic.
- Views (§16.3): top N from `event_config.leaderboard_top_n`; **`current_user` always populated for an authenticated attendee**, even inside the top N, so the client has one component and no special case; show-all with keyset pagination.
- **Pagination consistency (§16.3):** responses carry `as_of` and `dataset_generation`; a page whose generation differs from the first page's is discarded and the listing restarts.
- `GET /api/attendees/me/score` — the authoritative snapshot (§17.4): `total_points`, `rank`, `recent_awards` (last 10, `created_at DESC, id DESC`, **display order only**), `as_of`, `dataset_generation`.
- Wire the M08 placeholders to real data.
- **No caching** (§16.4) — justified at demo scale, and a cache would add a staleness window that can be wrong on stage. **No denormalised total is introduced** (§24.1).

### Expected Files / Areas
`backend/app/modules/leaderboard/`, frontend leaderboard and home score surfaces.

### Database Changes
None.

### API / Contract Changes
`GET /api/leaderboard`, `GET /api/attendees/me/score`.

### Security / Correctness Invariants
§24.1 (ledger is truth; no mutable cached total), §24.22 (`recent_awards` order is display-only, never a cursor), `ACTIVE`-only visibility.

### Tests
- **Integration:** zero-point `ACTIVE` attendees appear; `PROVISIONAL` users never appear.
- **Integration:** tie handling — equal totals share a rank; list order is byte-identical across repeated queries.
- **Integration:** `current_user` is present both inside and outside the top N, with a correct rank.
- **Integration:** `REVERSAL` rows reduce totals naturally via `SUM`.
- **Integration:** keyset pagination returns each row once under a static dataset; a generation change mid-listing is detectable by the client.
- **Performance:** aggregation over a seeded few-thousand-row ledger completes in single-digit milliseconds.

### Acceptance Criteria
1. Leaderboard and score are computed from `points_log` on every request, with no cache.
2. Repeated identical requests return identical ordering.
3. Unverified registrations are invisible.
4. M08's placeholders now render live data.

### Explicitly Out of Scope
Realtime updates (M14), materialised views / Redis sorted sets (production evolution, §16.5), leaderboard privacy changes (open question, §23.3).

### Demo vs Production Note
**Demo-complete; production evolution documented** — §16.5 escalation path, with `points_log` remaining authoritative in all of it.

---

## M14 — Realtime Invalidation and Reconciliation

### Objective
SSE as a pure invalidation channel, with the reconciliation model that makes correctness independent of delivery.

### Dependencies
M13.

### Scope

**The protocol (§17.4), implemented exactly:**
1. **Subscribe first, then snapshot** — open `EventSource`, wait for `ready`, *then* `GET /api/attendees/me/score`. Fetching first would leave a gap in which a change is missed.
2. On any signal → refetch.
3. On reconnect → refetch (**no event replay, no resume token**).
4. On visibility regained → refetch.
5. **Every 20 s even while connected → refetch** — the **correctness backstop**; "refetch" means both the personal snapshot and the leaderboard whenever that view is mounted.
6. No frame for 45 s → treat as dead, close, reconnect, refetch.

**Events (§17.5), carrying no attendee data:** `ready`, `state_changed`, `leaderboard_changed`, `heartbeat` (15 s), `session_invalid`, `dataset_reset`. **Events deliberately have no `id:` field**, so `Last-Event-ID` never exists and there are no resume semantics to get wrong.

**Two channels (§17.3/§17.5):** personal, keyed by `user_id` **from the session** — never a query parameter; and broadcast, **data-free by construction** (§24.18). A broadcast never animates.

**Animation without ordering (§17.6):** `animatedAwardIds: Set<bigint>`, seeded from the **first** snapshot, in memory only. An award not in the set animates once and is added. **This is correct regardless of commit order** — set membership has no notion of "too old". Displayed state is always **assigned** from the snapshot; the client never computes `total += 15`. The documented bound (an award outside the 10-row window skips its animation while total and rank stay exact) is accepted, not worked around.

**`points_log.id` is used only as an identity for that set — never as a cursor, high-water mark, ordering or resume mechanism** (§24.22).

**Liveness and proxy behaviour (§17.9):** `Cache-Control: no-cache, no-transform`, `X-Accel-Buffering: no`, **compression disabled for `text/event-stream`**; session re-validated **on every heartbeat tick** with `session_invalid` + close (bounding authorization lifetime to ~15 s); streams closed after **30 minutes** max age; backend restart resolved by reconnect + refetch.

**Fallback (§17.11):** after 3 failed reconnects, poll every 5 s and retry SSE every 60 s; **polling refreshes both personal state and leaderboard**, since no broadcast exists in fallback mode.

**Dataset generation (§19.3):** every response carries `X-Dataset-Generation`; a mismatch forces hard reinitialisation.

**Transport (§17.10):** in-process `asyncio` pub/sub — `user_id → set[Queue]` plus one broadcast set. No Redis. **Single worker remains invariant (§24.17)**, though the 20 s floor now bounds the consequence of violating it to degraded latency rather than incorrectness.

> **State explicitly in code comments and docs: SSE correctness does not depend on durable delivery.** Losing every event degrades latency to 20 s; it never produces a wrong value.

### Expected Files / Areas
`backend/app/modules/realtime/`, `frontend/src/lib/` (SSE client, reconciliation loop).

### Database Changes
None.

### API / Contract Changes
`GET /api/realtime/stream`.

### Security / Correctness Invariants
§24.10, §24.12 (publish after commit), §24.18 (broadcast carries no data), §24.22, §24.27 (periodic reconciliation), §24.28.

### Tests
- **Integration:** an award on one session triggers a signal on the attendee's stream; the client refetches and displays server values.
- **Integration:** subscribe-before-snapshot — an award landing between subscribe and snapshot still results in a correct final state.
- **Integration (lost event):** suppress publication entirely after commit → the client self-corrects **within 20 s**. *The headline F04 test.*
- **Integration (backend restart):** streams drop, clients reconnect and refetch; no server-side state to rebuild.
- **Integration (stall):** withhold frames → client detects at 45 s, reconnects, refetches.
- **Integration:** revoked session → `session_invalid` within one heartbeat and the stream closes.
- **Integration:** a client cannot subscribe to another user's personal channel (no query-parameter path exists).
- **Unit:** out-of-order award ids both animate exactly once; an award already in the first snapshot never animates.
- **Unit:** broadcast events never animate and carry no attendee fields.
- **Manual:** two phones — award on B updates A's score, rank and leaderboard without manual refresh.

### Acceptance Criteria
1. With **all** events suppressed, both phones still converge to correct values within 20 s.
2. No `Last-Event-ID`, no resume token, no id-based cursor exists in the codebase.
3. No SSE payload contains points, totals, ranks or names.
4. Proxy-buffering headers are present on the stream response.

### Explicitly Out of Scope
`LISTEN/NOTIFY` or Redis transport (production evolution), multi-worker operation, reset integration beyond emitting `dataset_reset` (M15).

### Demo vs Production Note
**Demo-complete; production-interface.** `EventBus` is an interface precisely so the transport swap touches one class.

---

## M15 — Demo Reset System

> **HIGH-RISK MILESTONE.** Reset is the only operation that destroys and rebuilds the dataset while clients are connected. Its adversarial concurrency tests ship **in this milestone**.

### Objective
The locked reset lifecycle: fenced, atomic, session-invalidating, generation-advancing, and fail-safe.

### Dependencies
M02 (fence + `event_config`), M03 (fixtures), M05/M09 (sessions to revoke), M14 (streams to terminate).

### Scope

- **CLI only (§19.1):** `docker compose exec backend python -m app.cli reset-demo --confirm`. **No HTTP endpoint exists — authenticated, hidden or otherwise** (§24.8).
- **Guards (§19.2):** refuses unless `DEMO_MODE=true` **and** `ENVIRONMENT != production`, plus explicit `--confirm`.
- **Dedicated non-pooled connection (§19.4):** its own engine / `NullPool`. **Never the application pool** — a session-scoped advisory lock returned to a pool could be inherited by an unrelated request.
- **Lock lifecycle with `try/finally` (§19.4):**
  1. open the dedicated connection;
  2. set and **commit** `maintenance_mode = true` (fail-safe, committed first);
  3. `pg_advisory_lock(FACEMARK_RESET_FENCE)` — exclusive, session-scoped, `lock_timeout 30 s`, **which drains all in-flight mutations**;
  4. `try:` run the critical section;
  5. `finally:` `pg_advisory_unlock(...)` when usable, then **close/dispose the connection — the backstop**, since PostgreSQL frees session-scoped locks when the session ends.
- **Sequence (§19.5):** maintenance ON → middleware rejects → **acquire fence** → **single atomic restore transaction** (truncate `points_log`, `idempotency_records`, `feedback`, `sessions`, `otp_challenges`, `consent_audit`, `user_interests`, `contact_cards`, `users` with `RESTART IDENTITY CASCADE`; **reconcile reference data — delete rows whose natural key is absent from fixtures, then upsert**, not merely upsert; restore `event_config`; insert demo attendees with **fixed** tokens; optional baseline points; **`dataset_generation += 1`**) → **commit** → post-restore in-process cleanup (emit `dataset_reset`, close all streams, clear the broker) → verify → **maintenance OFF** → **release fence** → report.
- **Release order is load-bearing (§19.5):** maintenance is cleared **only after** restore, cleanup and verification; the fence is released **last**. Clearing maintenance before releasing the fence keeps the transition monotonic.
- **Session invalidation is the `TRUNCATE` of `sessions` inside the restore transaction (§19.6)** — not a separate pre-step, so it can never be left applied after a rollback. **All sessions, no exception**; the CLI operator needs no web session.
- **`points_log` and `idempotency_records` are truncated together** (§19.6) — any other combination would let a replay assert an award that no longer exists.
- **Stable fixture QR tokens survive** (§19.8); `dataset_generation` disambiguates state, tokens deliberately persist.
- **Failure behaviour (§19.7):** restore rolls back entirely; **maintenance stays ON** in every failure path; the fence is released by `finally`. Recovery is explicit operator action (`reset-demo --confirm` retry, or `maintenance --off`).

### Expected Files / Areas
`backend/app/cli.py`, `backend/app/modules/admin/reset.py`, `backend/app/core/locks.py`.

### Database Changes
None new — `maintenance_mode` and `dataset_generation` exist from M02.

### API / Contract Changes
None. `X-Dataset-Generation` is already emitted (M13/M14).

### Security / Correctness Invariants
§24.8, §24.28, §24.29, §24.30.

### Tests

**Adversarial concurrency (real PostgreSQL — all required)**
- **Mutation before reset:** an award holding the shared fence causes the exclusive acquire to **wait** until it commits or rolls back.
- **Mutation during reset:** a request arriving while reset holds the fence is rejected by maintenance mode; if it slipped past the advisory check, it **blocks at B0 before any resolution** and returns `MAINTENANCE` on `lock_timeout`. It must never validate against the old dataset.
- **Mutation immediately after reset:** succeeds against the **new** dataset, with all authoritative validation re-run.
- **Reset transaction failure:** dataset, sessions and generation unchanged; **maintenance ON**; fence released.
- **Unexpected exception after lock acquisition:** `finally` unlocks and closes; maintenance ON.
- **Process kill (SIGKILL):** PostgreSQL releases the session-scoped lock when the connection dies; `maintenance_mode = true` persists in the database.
- **Fence-acquire timeout:** aborts **before any destructive step**, dataset untouched, reporting the holding backend.

**Integration**
- Reset restores byte-identical fixture state; demo card tokens are unchanged.
- Reconciliation removes an admin-created extra booth (the rehearsal-leftover case that plain upsert would miss).
- `dataset_generation` increments by exactly one.
- A pre-reset award retried after reset: **authentication fails first**; even bypassing that, no idempotency record exists and no award exists to duplicate.

**Manual/device**
- **Two phones connected with live streams:** both receive `dataset_reset`, streams close, sessions are invalid, and both are forced to reinitialise and re-authenticate. Stale pre-reset values are never displayed against reused ids.

### Acceptance Criteria
1. Every adversarial case above behaves as specified against real PostgreSQL.
2. No HTTP reset path exists — asserted against the OpenAPI schema.
3. Reset refuses to run with `DEMO_MODE=false` or `ENVIRONMENT=production`.
4. Every failure path leaves maintenance **ON** and the advisory lock **released**.
5. Two connected phones reinitialise cleanly.

### Explicitly Out of Scope
Partial/selective reset, production reset (it must not exist), backup/restore.

### Demo vs Production Note
**Demo-only by design.** The guards exist to ensure this can never execute in production.

---

## M16 — Minimal Admin

### Objective
Only the operational visibility the architecture and demo require — deliberately not a CMS.

### Dependencies
M09, M11.

### Scope
- `/api/admin/*` behind `current_admin` (requires `principal_type = STAFF` **and** `role = ADMIN`); a booth-staff session gets `403` on every route in the namespace (§18).
- Read views: attendees (with `status`), booths and interest weights, staff accounts, `points_log` audit trail.
- Minimal mutations: create/deactivate staff, manage booths/interests/weights, manage tasks and preset values, edit `event_config`.
- **Demo-mode OTP visibility** surface (§8.2), gated on `DEMO_MODE`.
- **Admins cannot award or modify points (§12.3).** The audit view is **read-only**. No undocumented award path may exist.
- **Every admin mutation and its audit row are written in the same transaction** (§18), using the shared fence helper.
- First admin comes from seed/CLI only — **no bootstrap route that creates an admin if none exists**; that is a backdoor by another name (§18).
- Admin routes excluded from the production OpenAPI schema and rate-limited (§21.9).

### Expected Files / Areas
`backend/app/modules/admin/`, `frontend/src/app/admin/`.

### Database Changes
An `admin_audit` table if not already created in M02 — otherwise none. *(Recorded as clarification C4.)*

### API / Contract Changes
`/api/admin/*` namespace.

### Security / Correctness Invariants
§18 role boundary, §21.9, §24.9, audit atomicity, §24.29.

### Tests
- **Negative (required):** booth staff receive `403` on every admin route; **no admin route creates a `points_log` row** — asserted by schema inspection and by exercising every admin mutation and confirming the ledger is unchanged.
- **Negative:** no unauthenticated admin-bootstrap route exists.
- **Integration:** every admin mutation writes an audit row **in the same transaction**; a failed mutation leaves no audit row.
- **Integration:** `event_config` edits are bounded by the DB CHECKs, not only by the form.

### Acceptance Criteria
1. No admin path can create or modify points.
2. Audit rows and mutations are atomic.
3. Booth staff are fully excluded from `/api/admin/*`.

### Explicitly Out of Scope
Full CMS, bulk import/export, analytics dashboards, reset-from-UI (M15 is CLI-only).

### Demo vs Production Note
**Intentionally deferred hardening** — production needs broader admin tooling, MFA and richer audit review; the boundary and audit atomicity are production-correct.

---

## M17 — UI/UX Polish and PWA Hardening

### Objective
Presentation quality on real devices. **No business-logic change.**

### Dependencies
M08, M10, M13, M14.

### Scope
- Mobile-first layout refinement; premium AI-summit visual language; typography and spacing scale; touch targets ≥ 44 px.
- QR presentation (contrast, quiet zone, size for reliable scanning at arm's length), scanner framing and guidance.
- Award confirmation and success states; the `+N` animation driven by the M14 identity-set rule — **polish changes its appearance, never its trigger condition**.
- Leaderboard movement, subtle microinteractions, refined loading/empty/error states.
- Accessibility: contrast, focus order, labels, reduced-motion support.
- PWA: manifest, icons, installability, offline shell for static assets only — **never cached business data** (it would contradict snapshot authority).
- Device testing: iPhone Safari, Android Chrome, tablet, desktop Chrome.

**Deliberately avoided:** excessive glassmorphism, neon, gratuitous animation, generic admin-template appearance.

### Expected Files / Areas
`frontend/src/{components,app}`, Tailwind theme, PWA assets.

### Database Changes
None.

### API / Contract Changes
None.

### Security / Correctness Invariants
No business logic may move into components (§24.9). **No client-side score, rank or points computation may be introduced** (§24.1, §24.5). Offline caching must not cache authoritative values (§24.10).

### Tests
- **Component:** visual regression on key surfaces; reduced-motion honoured.
- **Accessibility:** automated contrast/labels audit on main flows.
- **Manual/device:** iPhone, Android, tablet, desktop — layout, QR scan reliability at realistic distance, scanner usability in variable lighting.

### Acceptance Criteria
1. No API contract or service logic changed in this milestone (verifiable from the diff).
2. QR scans reliably from a phone screen and from print at arm's length.
3. No horizontal scroll at 375 px; touch targets meet size guidance.
4. Installable as a PWA; offline shell does not serve stale business data.

### Explicitly Out of Scope
New features, new endpoints, matching/points/realtime behaviour changes.

### Demo vs Production Note
**Demo-complete.**

---

## M18 — Full QA and Security Gate

> **Feature freeze.** No new functionality. This is a verification gate; a failure here is a defect in the milestone that introduced it, not an invitation to patch around it.

### Objective
Execute the complete verification matrix with explicit PASS/FAIL criteria.

### Dependencies
M01–M17.

### Scope — verification matrix

| # | Area | PASS criterion |
|---|---|---|
| 1 | Clean migration | `alembic upgrade head` on an empty database succeeds; `downgrade base` succeeds; re-`upgrade` shows no drift |
| 2 | Seed | `seed` populates; re-run is a no-op |
| 3 | Backend tests | Full suite green against **real PostgreSQL** |
| 4 | Frontend tests | Component and integration suites green |
| 5 | Lint | `ruff` and `eslint` clean |
| 6 | Types | `mypy` and `tsc --noEmit` clean |
| 7 | Authentication | No capability before `ACTIVE`; sessions hashed; logout immediate |
| 8 | Authorization | Role boundaries hold on every protected route |
| 9 | **Booth isolation** | Staff A cannot affect booth B in any tested shape |
| 10 | OTP | Hashed, expiring, single-use, attempt-capped, non-enumerating |
| 11 | Consent | Stored as decision + timestamp + version, with an atomic audit row |
| 12 | QR privacy | No phone, consent state or internal id in any public response; token masked in app logs; **proxy log masking verified** |
| 13 | Matching | Canonical fixture = **78**; determinism; no random/LLM/multiplier |
| 14 | `scan_ref` | All ten cases from §13.3 pass, including wrong-session and future-`iat` |
| 15 | Idempotency | All eight reuse scenarios; **expired-on-replay replays** |
| 16 | Concurrency | Simultaneous awards, simultaneous feedback, wait-then-replay |
| 17 | **Reset fence** | No mutation crosses a reset; no pre-fence authoritative read |
| 18 | Feedback uniqueness | One reward per `(attendee, booth)` under concurrency |
| 19 | Ledger correctness | Totals equal `SUM(points_log)`; no denormalised total exists |
| 20 | Leaderboard | Deterministic order; zero-point `ACTIVE` included; `PROVISIONAL` excluded |
| 21 | Realtime reconciliation | **All events suppressed → correct within 20 s** |
| 22 | Dataset generation | Stale client forced to reinitialise |
| 23 | Rate limiting | 200 registrations from one NAT IP succeed; per-identity limits bite |
| 24 | Trusted proxy | Spoofed XFF from an untrusted peer has no effect |
| 25 | Browser states | Loading/empty/error on every surface; no unhandled rejection |
| 26 | Infrastructure | `docker compose up` from clean clone; HTTPS origin serves the scanner; **SSE not buffered through the real proxy chain** |

**Security review sweep — each must return nothing live:** `IN_PROGRESS` idempotency, `points_log.id` as cursor, pre-fence authoritative DB reads, `scan_ref` expiry before completed replay, client-authoritative `booth_id`, immediate authenticated registration, mutable cached total, reset preserving web sessions, secrets with fallback defaults, `allow_origins=["*"]`, string-formatted SQL with request data.

### Expected Files / Areas
Test suites, CI configuration, a QA results record.

### Database Changes / API Changes
None.

### Security / Correctness Invariants
All 30 invariants in `ARCHITECTURE.md` §24 are individually checked and recorded as PASS/FAIL.

### Tests
The entire suite, plus the manual device matrix and the proxy-chain SSE check from §17.9 — **which cannot be satisfied against `localhost`**, since that bypasses the proxy entirely.

### Acceptance Criteria
1. Every row of the matrix is PASS, with evidence recorded.
2. Every §24 invariant is individually verified.
3. The security sweep returns no live occurrence of any removed design.
4. Zero known defects in the demo-critical path.

### Explicitly Out of Scope
Any new feature; any remediation that changes architecture (escalate instead).

### Demo vs Production Note
**Gate.** Passing means demo-ready, not production-certified — §23 states what production still requires.

---

## M19 — Summit Presentation Rehearsal

### Objective
Rehearse the golden path end to end on real devices, including a reset, until it is reliable.

### Dependencies
M18, M15.

### Scope — the golden path

**Phone A (attendee):** 1 open registration · 2 register · 3 receive/use OTP · 4 become `ACTIVE` · 5 see matched booths · 6 open My QR.

**Phone B (staff):** 7 log in · 8 open scanner · 9 scan Phone A · 10 confirm attendee · 11 select **+15** · 12 submit award.

**Phone A:** 13 receive invalidation/reconciliation · 14 score updates · 15 rank updates · 16 leaderboard updates · 17 success animation.

**Then:** 18 submit feedback · 19 automatic reward appears · 20 submit duplicate feedback · 21 **no second reward** · 22 attempt a duplicate task reward under the demo repeat policy · 23 verify the expected rejection (`DUPLICATE_TASK_REWARD`, since the demo default is `allow_repeat_task_points = false`) · 24 **run demo reset while clients are connected** · 25 verify session invalidation and client reinitialisation · 26 repeat the golden path after reset.

Also rehearse: camera-permission denial recovery; a deliberately expired `scan_ref` (scan, wait, then award) showing `SCAN_EXPIRED` and a clean re-scan; network interruption on Phone A showing self-correction within 20 s.

Measure and record **presentation latency** — scan → confirm → award → visible update on Phone A (§13.3 test case 7).

### Expected Files / Areas
Rehearsal notes and measurements. **`DEMO_RUNBOOK.md` is produced by this milestone when it executes — it is deliberately NOT created during Phase 2.**

### Database Changes / API Changes
None.

### Security / Correctness Invariants
No invariant may be relaxed to make the rehearsal smoother. If the demo is awkward, the fix is UX or seed data, never a weakened rule.

### Tests
Full manual rehearsal on the presentation devices and network, repeated until two consecutive clean runs including a reset between them.

### Acceptance Criteria
1. Golden path completes on real devices over the real HTTPS origin and network.
2. Steps 20–23 produce the expected refusals — visibly and understandably.
3. Reset with connected clients leaves both phones cleanly reinitialised.
4. The post-reset golden path succeeds identically.
5. Two consecutive clean end-to-end runs.
6. Measured scan-to-visible latency is comfortable for stage use and recorded.

### Explicitly Out of Scope
New features; creating `DEMO_RUNBOOK.md` during Phase 2.

### Demo vs Production Note
**Demo-complete.**

---

## 20. Risk Register

| # | Risk | Impact | Mitigation | Verified in |
|---|---|---|---|---|
| R1 | **Mobile camera requires HTTPS.** `getUserMedia` is gated on a secure context; a phone on `http://192.168.x.x` is denied | **Demo-fatal** — the scanner cannot work | HTTPS origin mandatory (§13.5); mechanism deliberately replaceable; manual entry as emergency fallback only | **M10 (M-CAM)**, M18 |
| R2 | iOS/Android QR decoding differences (codec support, focus, permission prompts) | Scanner unreliable on one platform | Both platforms tested on physical hardware with printed codes; QR size/contrast tuned | M10, M17, M19 |
| R3 | **Venue Wi-Fi / NAT** — hundreds of attendees behind one public IP | Per-IP limits would lock out genuine users | Per-identity limits with high shared-IP ceilings; per-token card limit; trusted-proxy resolution (§21.7) | **M05**, M18 |
| R4 | **Proxy buffering of SSE** — a buffered stream is indistinguishable from a healthy idle one | Live updates silently stop | `X-Accel-Buffering: no`, `no-transform`, compression off; heartbeats buffer too so the 45 s rule detects it; 20 s reconciliation floor makes it non-fatal; tested through the **real** proxy chain | M14, **M18** |
| R5 | OTP delivery provider failure or latency | Attendees cannot activate | Demo uses a logging adapter behind a production interface; delivery is not on the demo critical path | M04, M19 |
| R6 | **Concurrent awards** to one attendee/booth | Double points, or a crash on stage | DB-enforced `dedupe_key` / partial unique indexes; constraint-name dispatch; concurrency tests on real PostgreSQL | **M11**, M18 |
| R7 | **Idempotency retries** after a lost response | Double award, or a spurious error | Key scoped to `(staff, key)` with an immutable fingerprint; resolved before mutable validation; wait-then-replay; expired-`scan_ref` replay explicitly tested | **M11**, M18 |
| R8 | **Reset races** — a mutation crossing the reset boundary | A ledger row belonging to no coherent dataset | Shared fence acquired **before the first reset-sensitive read** and held through commit; exclusive acquire drains in-flight mutations | **M15**, M18 |
| R9 | **Advisory-lock misuse** — wrong scope, leaked lock | Every future reset blocks, or the fence silently does nothing | Mutations use the **transaction-scoped** variant (auto-released); reset uses session-scoped on a dedicated connection with `try/finally`; fence lives in the shared unit-of-work helper, not per endpoint | M02, **M15** |
| R10 | **Connection pooling** — a session-scoped lock returned to the pool | An unrelated request inherits the lock | Dedicated **non-pooled** connection for reset; closure is the backstop | **M15** |
| R11 | **Stale browser state after reset** — ids are reused | A phone shows pre-reset values against new ids | `dataset_generation` on every response; mismatch forces hard reinitialisation; all sessions revoked | M15, M19 |
| R12 | Leaderboard query performance | Slow refresh on stage | No cache is justified at demo scale (§16.4); indexed aggregation; performance asserted on a seeded ledger; §16.5 escalation documented | M13, M18 |
| R13 | **Privacy / contact-card exposure** — email visible in the fallback case; token in URL path | Attendee data leak; token in proxy logs | Phone and consent never exposed; one server-chosen action; per-token rate limit; `no-referrer`; app **and proxy** log masking | M06, **M18** |
| R14 | **Demo seed/reset reliability** — printed QR invalidated, or rehearsal leftovers persist | Printed materials stop working; "known state" is not known | Fixed fixture tokens; reconcile-not-upsert; verification step; fail-safe maintenance | M03, **M15**, M19 |
| R15 | Single-worker invariant accidentally violated | Realtime signals miss clients | Pinned in compose; bounded by the 20 s reconciliation floor (degraded, not wrong) | M01, M14 |
| R16 | Seed data yields unimpressive match scores | Demo looks weak — and tempts a cosmetic fix, which is forbidden | Seed designed for genuine strong/mid/low matches; expected scores asserted in tests | M03, **M07** |

## 21. Test Strategy

**Backend unit tests.** Pure logic with no I/O: the matching core (fixed vectors, `ROUND_HALF_UP`, property-based bounds), explanation templating, `scan_ref` canonical serialization and HMAC, fingerprint computation, trusted-proxy XFF resolution, error mapping. Fast, run on every save.

**Database integration tests — PostgreSQL only.** Every constraint, partial index, composite FK and CHECK is verified by attempting the forbidden state and asserting the database refuses it.

> **SQLite cannot validate this system and is not used anywhere in the test suite.** Partial unique indexes, expression indexes, `citext`, native enums, `num_nonnulls`, advisory locks, `FOR UPDATE` semantics and constraint-name reporting are all PostgreSQL behaviours. A green SQLite suite would be actively misleading about exactly the properties this architecture depends on. **All backend tests run against a real PostgreSQL container**, including in CI.

**Concurrency tests against real PostgreSQL.** Multiple real connections with controlled interleaving: simultaneous same-key awards (wait-then-replay), simultaneous different-key awards under the repeat policy, simultaneous feedback, OTP verification races, and the fence tests — mutation-before-reset drains, mutation-during-reset blocks, mutation-after-reset validates against the new dataset.

**Frontend component tests.** Where they carry weight: recommendation rendering (`match: null` ≠ `0%`), error-code → copy mapping with fallback, the animation identity-set rule, confirmation-screen non-bypassability, loading/empty/error states.

**API integration tests.** Full request/response through the app against real PostgreSQL: auth flows, role boundaries, the award stage ordering, response shapes, and the error envelope.

**Physical mobile device tests.** Non-negotiable for camera and QR work — **M-CAM** (iPhone Safari + Android Chrome, real HTTPS, printed codes), scanner usability, PWA install, and the two-phone realtime flow. Simulators do not satisfy these.

**End-to-end golden path.** The M19 sequence, scripted where practical and manual where devices are required.

**Reset rehearsal.** Reset executed with live clients connected, verifying termination, invalidation, generation advance and clean reinitialisation — then the golden path repeated.

## 22. Implementation Order Validation

Simulated against the required checks:

| Check | Result |
|---|---|
| No milestone depends on later functionality | ✅ — one conflict found and resolved: **M08 originally required score/rank/leaderboard**, which depend on M11/M13. M08 now ships those surfaces as honest placeholders and **M13 wires them**. This is an ordering adjustment; no architecture changed |
| Migrations before feature code | ✅ M02 precedes every feature milestone |
| Auth before protected flows | ✅ M04/M05 before M06/M08; M09 before M10/M11/M16 |
| QR identity before scanner | ✅ M06 → M10 |
| Scanner before award UX | ✅ M10 → M11 |
| PointsLog before leaderboard | ✅ M02 (table) and M11 (rows) → M13 |
| Snapshot endpoints before realtime UI | ✅ M13 (`/me/score`, `/leaderboard`) → M14 |
| **Mutation fence before reset** | ✅ **M02** ships the shared fence in the unit-of-work helper; M15 implements the reset side. Every mutation from M04 onward inherits it |
| **`dataset_generation` before reset/realtime** | ✅ `event_config` created in M02; emitted from M13/M14; advanced in M15 |
| Seed infrastructure before rehearsal | ✅ M03 → M19 |

Additional checks: `scan_ref` is **issued** in M10 and **verified** in M11, so no milestone consumes an unbuilt mechanism. Stream teardown on session death is **contracted** in M05 and **implemented** in M14, with the boundary stated in both. No cycles exist in the dependency map.

## 23. Demo vs Production Matrix

| Item | DEMO | PRODUCTION EVOLUTION |
|---|---|---|
| **OTP delivery** | Logged / shown in admin, gated on `DEMO_MODE`; startup refuses `DEMO_MODE` in production | Real email provider behind the same adapter interface. Lifecycle, hashing, expiry and attempt caps are already production logic |
| **HTTPS / deployment** | HTTPS **mandatory** for camera; mechanism chosen against three properties and deliberately not vendor-locked | Managed TLS at the edge. No application code depends on which mechanism provides the certificate |
| **Worker topology** | **Exactly one** Uvicorn worker (invariant §24.17) | N workers, once `EventBus` uses a shared transport. Bounded today by the 20 s reconciliation floor |
| **Realtime broker** | In-process `asyncio` pub/sub, no external service | PostgreSQL `LISTEN/NOTIFY` (no new infrastructure) or Redis pub/sub at scale. One `EventBus` implementation changes |
| **Monitoring** | Structured logs with correlation ids; no dashboards | Metrics, tracing, alerting on award failures, fence wait times, stream counts |
| **Observability** | Manual log inspection | Centralised aggregation with the sensitive-key redaction filter carried over unchanged |
| **Rate-limit storage** | In-process fixed-window counters (single worker) | Shared store; the limiter interface is already storage-agnostic. **Limits themselves are production-shaped** — per identity, not per IP |
| **Backups** | **None.** Demo data is disposable and reset is destructive by design | Automated backups, PITR, tested restore. Reset must not exist |
| **Secret management** | `.env`, git-ignored; no fallback defaults; startup fails on absence | Secret manager. `pydantic-settings` reads either without code change |
| **Admin completeness** | Minimal operational views plus the seeded first admin | Broader tooling, MFA, delegated roles, reviewable audit UI |
| **Scaling** | Single node, hundreds of attendees, uncached aggregation (justified §16.4) | Projection table or materialised view (§16.5) with `points_log` remaining authoritative; horizontal workers |
| **Demo reset** | CLI, guarded, destructive | **Must not exist.** Guards already refuse production |
| **Demo QR tokens** | Fixed in fixtures so printed cards survive resets | Always `secrets`-generated per attendee; fixtures never load |
| **Staff credentials** | Seeded from env vars, no defaults | Admin-created per person |
| **Matching** | `weighted-v1` | `embedding-v1` via pgvector, same API contract (§11.8) |

**Nothing above is presented as production-ready when it is not.** Every DEMO entry is either an adapter behind a production interface or an explicitly guarded demo-only mechanism.

## 24. Implementation Clarifications

Genuine implementation ambiguities found while planning. **None is an architecture change**, and none may be resolved by editing `ARCHITECTURE.md` without a new architecture phase.

**C1 — `users` field table vs the F06 lifecycle.** §7.1's field table does not list `status` or `activated_at`, but §8.1 specifies both normatively (`PROVISIONAL`/`ACTIVE`, `activated_at`, and the accompanying CHECK). §7.1 predates the F06 correction. **Resolution for implementation: follow §8.1** — it is the later, more specific and explicitly-corrected text, and §16.1 depends on `status` existing. Flagged for a future architecture editorial pass.

**C2 — stale cross-reference in §17.7.** It cites "§14.3 step 13" for post-commit publication; after the Phase 1.5 renumbering, that step is **C1**. The rule is unchanged; only the pointer is stale.

**C3 — attendee interest editing.** §11.4 says "attendee changes interests → scores recompute on read", implying interests are editable, but no endpoint or UI for editing them is specified. **Resolution: not implemented in M04–M08.** If the demo requires it, it is a small additive endpoint reusing registration's slug-resolution logic — but it is not assumed here.

**C4 — admin audit table.** §18 and §21.9 require an audit log for admin mutations, but §7 does not enumerate an `admin_audit` table (unlike `consent_audit`). **Resolution: create it in M02** alongside the other tables, shaped like `consent_audit` (actor, action, target, before/after, timestamp), so M16's atomicity requirement is satisfiable.

**C5 — maintenance-mode read frequency.** The advisory middleware check reads `event_config` per mutating request. The architecture does not specify caching. **Resolution: read per request in the demo** — it is one indexed single-row lookup, it is explicitly advisory, and caching it would add a staleness window to a fast-path whose whole purpose is to be fresh enough to be useful.

**C6 — the six open product questions (§23.3)** — repeat-task default, feedback reward amount, preset scope, OTP delivery channel, leaderboard name privacy, zero-point visibility — all have working defaults and are **configuration or seed data**, not structure. They are consumed by M03 (`event_config` and preset fixtures) and can change without code. None blocks any milestone.

**C7 — `scan_ref` clock source.** §13.3 specifies a server-issued `iat` and a 5 s skew allowance, but does not address clock drift between the issuing and verifying process. **Resolution: not an issue in the demo** — single worker, one host, one clock. Recorded because a multi-host production deployment would need NTP discipline or a wider skew allowance.
