# FACEMARK — System Architecture

**Status:** Phase 1 — Architecture. No application code exists yet.
**Target:** Presentation-ready demo for an Artificial Intelligence Summit, built on foundations that can evolve into production without a rewrite.

---

## 1. System Context

FACEMARK is an event networking and gamification platform. Attendees register, declare interests, receive a personal networking QR card, get explainable booth recommendations, visit booths physically, complete activities, and earn points that feed a live leaderboard.

### 1.1 Actors

| Actor | How they enter the system | What they can do |
|---|---|---|
| **Attendee** | Self-registration | Register, select interests, view their QR card, see recommendations, browse booths, submit feedback, view their score and the leaderboard |
| **Booth Staff** | Created by Admin only | Log in, scan attendee QR, confirm identity, award a preset point value **for their own booth only** |
| **Admin** | Provisioned out-of-band (seed/CLI) | Manage booths, interests, staff accounts, tasks and preset values; inspect the points audit trail |

### 1.2 System boundary

```
          ┌───────────────────── Browser (mobile-first) ─────────────────────┐
          │  Attendee PWA       Staff Scanner PWA        Admin console       │
          └──────────┬───────────────────┬──────────────────────┬───────────┘
                     │   same-origin HTTP (Next.js rewrite proxy)
          ┌──────────▼───────────────────▼──────────────────────▼───────────┐
          │            Next.js (React, TypeScript, Tailwind)                 │
          │              presentation only — no business rules               │
          └──────────────────────────┬───────────────────────────────────────┘
                                     │  /api/*  →  reverse proxy
          ┌──────────────────────────▼───────────────────────────────────────┐
          │           FastAPI modular monolith (Python 3.12)                  │
          │   API layer → Service layer → Repository layer → SQLAlchemy       │
          │   MatchingService · PointsService · FeedbackService               │
          │   LeaderboardService · AuthService · QrTokenService · EventBus    │
          └──────────────────────────┬───────────────────────────────────────┘
                                     │
          ┌──────────────────────────▼───────────────────────────────────────┐
          │                  PostgreSQL (single database)                     │
          │            PointsLog = source of truth for all points             │
          └───────────────────────────────────────────────────────────────────┘
```

There are **no external service dependencies** in the demo. No LLM is called at request time. No message broker, no cache server, no object storage.

### 1.3 Explicit non-goals for the demo

Payments, ticketing, badge printing, attendee-to-attendee messaging, multi-event tenancy, analytics warehousing, native mobile apps, internationalisation.

---

## 2. Architecture Principles

1. **The backend is the only authority.** The frontend renders state and collects intent. It never decides what a user may do, and never computes a value that affects stored data.
2. **One source of truth per fact.** Points live in `points_log` and nowhere else. Derived values are computed, or are explicitly marked caches rebuildable from the log.
3. **Invariants belong in the database where the database can express them.** Uniqueness and reward de-duplication are enforced by constraints, not by application-level "check then insert", which races.
4. **Deterministic over impressive.** Match scores come from a pure function over real data. Nothing is randomised or generated to look intelligent.
5. **Modular monolith.** One deployable backend, internally partitioned by domain module behind explicit service boundaries. No microservices.
6. **Boring infrastructure.** Every piece of infrastructure must earn its place. The demo runs on Postgres and two processes.
7. **Demo mode is a flag, not a fork.** Demo shortcuts live behind explicit configuration and refuse to run when the environment is production. There is no separate demo codebase.
8. **Errors are typed and enumerated.** Every user-reachable failure has a stable machine code. No stack trace reaches a client.
9. **Realtime is an optimisation.** If every connection died, the product would still be correct after a refresh.

---

## 3. High-Level Architecture

### 3.1 Backend layering

Four layers. Dependencies point downward only; a lower layer never imports an upper one.

| Layer | Responsibility | May not |
|---|---|---|
| **API** (`routers`) | HTTP concerns: routing, request/response schemas, status codes, auth dependency injection | Contain business rules or build SQL |
| **Service** (`services`) | All business logic, invariants, transaction boundaries, authorization decisions | Know about HTTP, `Request`, or status codes |
| **Repository** (`repositories`) | Data access, query construction, constraint-violation translation | Contain business rules |
| **Model** (`models`) | SQLAlchemy entities, constraints, indexes | Contain behaviour |

The **service layer is the seam** for everything the specification calls a "boundary": `MatchingService`, `PointsService`, `FeedbackService`, `LeaderboardService`, `QrTokenService`, `AuthService`. Each is a plain class taking a session and configuration in its constructor — constructible in a unit test with no HTTP, and for `MatchingService` with no database at all.

### 3.2 Why the frontend is proxied rather than CORS-configured

The Next.js app exposes the API to the browser at **`/api/*` on its own origin**, via a `next.config.ts` rewrite to the FastAPI container:

```typescript
// next.config.ts
const nextConfig: NextConfig = {
  async rewrites() {
    return [{ source: '/api/:path*', destination: `${process.env.BACKEND_INTERNAL_URL}/:path*` }]
  },
}
```

Consequences, all of which materially de-risk the live demo:

- The browser makes **same-origin requests**, so there is no CORS preflight to misconfigure on venue Wi-Fi.
- Session cookies are same-origin, so `SameSite=Lax` suffices. We avoid `SameSite=None`, which mandates `Secure`, which mandates HTTPS on every hop.
- The FastAPI container need not be reachable from attendee phones at all — only from the Next.js server. Fewer moving parts to expose.

CORS is therefore configured restrictively (§21.6) and exists only for direct developer access to `/docs`.

### 3.3 Request flow — awarding points (the demo's critical path)

```
Staff phone              Next.js          FastAPI                       PostgreSQL
    │ scan QR (camera)      │                │                               │
    │ POST /api/staff/scan/resolve {token}    │                               │
    ├──────────────────────►├───────────────►│ AuthService: session → staff  │
    │                       │                │ QrTokenService.resolve(token) ├─ SELECT by unique token
    │◄── {first_name, last_name, profession} ─┤ (minimal identity only)       │
    │                       │                │                               │
    │ [human confirms: "Is this the correct attendee?"]                       │
    │                       │                │                               │
    │ POST /api/staff/points/award            │                               │
    │   {token, points, idempotency_key}      │                               │
    ├──────────────────────►├───────────────►│ PointsService.award_task()    │
    │                       │                │  1. booth_id ← staff session  │
    │                       │                │  2. points ∈ booth presets?   │
    │                       │                │  3. compute dedupe_key        │
    │                       │                │  4. INSERT points_log ────────┼─► unique(dedupe_key)
    │                       │                │                               │   unique(idempotency_key)
    │                       │                │  5. publish event POST-COMMIT │
    │◄── {awarded: 15, new_total: 45} ────────┤                               │
                                             │
Attendee phone  ◄── SSE: points_awarded (id = points_log.id) ──┘
```

The attendee's screen updates from an SSE notification, but the value it shows is server-computed, and the client reconciles against the API on reconnect (§17).

---

## 4. Technology Decisions

| Area | Decision | Rationale | Rejected alternative |
|---|---|---|---|
| Backend framework | **FastAPI**, Python 3.12 | Pydantic-native validation, first-party SSE, OpenAPI for free | Django (heavier), Flask (weaker typing story) |
| Runtime | **Python 3.12** in the container | Mature wheels for `psycopg`, `argon2-cffi`. Host has 3.14; the container is authoritative | Host 3.14 — risks C-extension gaps |
| ORM | **SQLAlchemy 2.x** typed ORM | Constraints/indexes co-located with models; parameterised SQL by construction | Raw SQL (loses constraint co-location); SQLModel (couples table and API schema, which we deliberately separate) |
| Migrations | **Alembic** | Reversible, reviewable, ordered | `create_all()` at startup — unreviewable |
| Database | **PostgreSQL 16** | Partial/expression unique indexes, window functions, `LISTEN/NOTIFY` as a future realtime path | SQLite — lacks the concurrency semantics the points engine depends on |
| Frontend | **Next.js (App Router), React, TypeScript `strict`, Tailwind** | Mandated baseline; rewrite proxy solves the origin problem | — |
| Realtime | **Server-Sent Events** + polling fallback | §17 | WebSockets — bidirectional capability we do not need |
| Password hashing | **Argon2id** (`argon2-cffi`) | Memory-hard; current OWASP guidance | bcrypt (acceptable fallback); SHA-family (unacceptable) |
| Sessions | **Opaque server-side sessions**, httpOnly cookie | Instantly revocable; no key rotation; no token-expiry surprises mid-demo | JWT — revocation is genuinely hard, and a live event needs working logout and staff re-provisioning |
| QR token | `secrets.token_urlsafe(32)` (256-bit) | Non-enumerable, no PII, no sequential IDs | UUIDv4 (lower entropy, invites "it's just an ID" thinking); HMAC of user id (leaks structure, unrevocable without key rotation) |
| Frontend package manager | **npm** | Mandated; installed; no global installs needed | pnpm/yarn — not installed |

---

## 5. Repository Structure

Proposed layout. **Not created in this phase.**

```
FACEMARK/
├── ARCHITECTURE.md
├── README.md
├── docker-compose.yml
├── .env.example                      # never .env — secrets are not committed
├── backend/
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── alembic/versions/
│   └── app/
│       ├── main.py                   # app factory, routers, exception handlers
│       ├── cli.py                    # demo reset / seed entrypoint
│       ├── core/
│       │   ├── config.py             # pydantic-settings; env only
│       │   ├── security.py           # argon2 hashing, token generation
│       │   ├── errors.py             # DomainError hierarchy + error codes
│       │   └── deps.py               # current_attendee / current_staff / current_admin
│       ├── db/
│       │   ├── base.py               # DeclarativeBase + constraint naming convention
│       │   └── session.py            # engine, session factory, unit of work
│       ├── modules/
│       │   ├── attendees/            # models routers services repositories schemas
│       │   ├── interests/
│       │   ├── booths/
│       │   ├── matching/             # MatchingService — pure, DB-free scoring core
│       │   ├── contact_cards/        # QR token issue/resolve
│       │   ├── staff/
│       │   ├── points/               # PointsService — the only writer of points_log
│       │   ├── feedback/
│       │   ├── leaderboard/
│       │   ├── admin/
│       │   └── realtime/             # EventBus + SSE endpoint
│       └── seed/
│           └── demo_fixtures.py      # deterministic seed incl. fixed QR tokens
├── frontend/
│   ├── package.json
│   ├── next.config.ts                # /api/* rewrite proxy
│   ├── tsconfig.json                 # "strict": true
│   └── src/
│       ├── app/
│       │   ├── (attendee)/           # register, interests, my-card, booths, leaderboard
│       │   ├── c/[token]/            # public networking card
│       │   ├── staff/                # login, scanner, confirm, award
│       │   └── admin/
│       ├── components/
│       ├── lib/                      # api client, SSE client, error-code → copy map
│       └── types/                    # generated from OpenAPI
└── docs/
```

**Module rule:** a module owns its tables and exposes behaviour through its service. Cross-module access is service→service, never repository→foreign table. This is what keeps `points_log` writable from exactly one place, and what makes later extraction possible without a rewrite.

---

## 6. Domain Model

### 6.1 Entity overview

```
Interest ──< UserInterest >── User ──< ContactCard
   │                            │
   │                            ├──< Feedback >── Booth
   │                            ├──< ConsentAudit
   └──< BoothInterest >── Booth ─┤
                            │    └──< PointsLog >─┬── User
                            │                     ├── Booth
                            ├──< Task             ├── StaffAccount (entered_by, nullable)
                            ├──< TaskPresetPoint  └── Task (nullable)
                            └──< StaffAccount ──< Session >── User

   OtpChallenge >── User (nullable)        EventConfig (singleton, no relations)
```

### 6.2 Deviations from the proposed entity list

Two justified changes; everything else is kept as named.

**`StaffSession` → `Session` (generalised).** Attendees and admins also need sessions. Three near-identical session tables would triple the session-validation logic and create three places to get expiry wrong. One table with a `principal_type` discriminator keeps a single code path for "who is this request" — the highest-value place in an auth system to avoid duplication. It still fulfils the `StaffSession` responsibility.

**`BoothStaff` → `StaffAccount` (generalised).** Admins need credentials too. A separate admin credential table would mean two password-hashing paths and two login endpoints to harden. One table carries a `role` enum (`BOOTH_STAFF` | `ADMIN`) with a `CHECK` constraint enforcing that booth staff must have a booth and admins must not. The name changed because the table legitimately holds non-booth accounts, and a misleading name invites mistakes.

Three entities are **added**, each justified where it is defined:

| Added entity | Why it is necessary | Defined in |
|---|---|---|
| `EventConfig` | Single-row table of event-wide tunables (feedback reward, repeat-task default). These must be changeable at the venue without a redeploy | §7.13 |
| `OtpChallenge` | Attendee auth is OTP-based; a challenge needs a hash, an expiry, a consumed flag and an attempt counter. There is no other table that can hold this | §8.2 |
| `ConsentAudit` | Sponsor consent must be **auditable**. A current-value column cannot show a later withdrawal or what text was agreed to at the time | §7.1 |

---

## 7. Database Design

Conventions: surrogate `id BIGINT GENERATED ALWAYS AS IDENTITY` primary keys; `created_at`/`updated_at` as `TIMESTAMPTZ` (never naive); all enums as Postgres native enum types; an Alembic constraint naming convention so migrations are readable.

The first migration must `CREATE EXTENSION IF NOT EXISTS citext;` — the `citext` columns below depend on it, and a migration that assumes the extension is present will fail on a fresh database.

**Internal integer IDs are never exposed in any public URL or QR payload.** They appear only inside authenticated API responses.

### 7.1 `users` (attendees)

| Field | Type | Notes |
|---|---|---|
| `id` | bigint PK | internal only |
| `first_name`, `last_name` | text NOT NULL | |
| `email` | citext NOT NULL | **UNIQUE** — the login identity |
| `phone` | text NOT NULL | |
| `profession` | text NOT NULL | shown on staff confirm screen |
| `linkedin_url` | text NULL | optional |
| `sponsor_consent` | boolean NOT NULL | explicit, no default — see below |
| `sponsor_consent_at` | timestamptz NULL | set when consent is granted |
| `sponsor_consent_version` | text NULL | which consent text was agreed to |
| `created_at`, `updated_at` | timestamptz NOT NULL | |

*Responsibility:* the attendee identity and profile.
*Constraints:* `UNIQUE(email)`; `CHECK (sponsor_consent = false OR sponsor_consent_at IS NOT NULL)`.
*Auditability:* consent is stored as a triple — decision, timestamp, and the version of the text agreed to. A bare boolean is not auditable, because it cannot answer "what did they actually agree to, and when". Changes to consent are additionally appended to `consent_audit` (`user_id`, `granted`, `consent_version`, `changed_at`, `source`), because the current value alone cannot show a later withdrawal.

### 7.2 `interests`

`id`, `slug` (text, **UNIQUE**, stable machine key), `name`, `is_active`, `sort_order`.
*Responsibility:* the controlled vocabulary shared by attendees and booths. Matching is meaningless unless both sides reference the same rows — free-text interests are explicitly rejected for the demo.

### 7.3 `user_interests`

`user_id` FK→users ON DELETE CASCADE, `interest_id` FK→interests, `created_at`.
*Constraints:* **`PRIMARY KEY (user_id, interest_id)`** — prevents duplicate selection at the database level.
*Index:* `(interest_id)` for reverse lookups.

### 7.4 `booths`

| Field | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `slug` | text **UNIQUE** | stable public identifier used in URLs instead of the id |
| `name`, `description`, `location_label` | text | |
| `logo_url` | text NULL | |
| `is_active` | boolean NOT NULL DEFAULT true | inactive booths are excluded from recommendations |
| `allow_repeat_task_points` | boolean **NULL** | per-booth override; NULL = inherit the event default (§14.5) |

### 7.5 `booth_interests`

`booth_id` FK, `interest_id` FK, `weight` numeric(3,2) NOT NULL.
*Constraints:* **`PRIMARY KEY (booth_id, interest_id)`**; **`CHECK (weight > 0 AND weight <= 1)`**.
*Responsibility:* the booth's weighted interest profile — the sole input to matching from the booth side. A zero or negative weight is rejected rather than stored, so "not associated" has exactly one representation (an absent row).

### 7.6 `contact_cards`

| Field | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `user_id` | FK→users NOT NULL | |
| `public_token` | text NOT NULL **UNIQUE** | 43-char URL-safe, 256-bit entropy |
| `issued_at` | timestamptz NOT NULL | |
| `revoked_at` | timestamptz NULL | non-null = dead token |
| `version` | int NOT NULL DEFAULT 1 | increments on rotation |

*Constraints:* `UNIQUE(public_token)`; **partial unique index `UNIQUE (user_id) WHERE revoked_at IS NULL`** — an attendee has at most one *live* card, while revoked cards are retained for audit. This is what makes rotation possible later without deleting history.

### 7.7 `staff_accounts`

| Field | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `email` | citext NOT NULL **UNIQUE** | |
| `password_hash` | text NOT NULL | Argon2id encoded string; never a plaintext password |
| `full_name` | text NOT NULL | |
| `role` | enum `BOOTH_STAFF` \| `ADMIN` NOT NULL | |
| `booth_id` | FK→booths **NULL** | |
| `is_active` | boolean NOT NULL DEFAULT true | deactivation kills future logins |
| `failed_login_count` | int NOT NULL DEFAULT 0 | |
| `locked_until` | timestamptz NULL | brute-force throttle |

*Constraint — the structural half of the booth invariant:*
```sql
CHECK ((role = 'BOOTH_STAFF' AND booth_id IS NOT NULL)
    OR (role = 'ADMIN'       AND booth_id IS NULL))
```
It is **not representable** in this schema for a booth staff account to lack a booth. That removes the entire class of "which booth do we attribute this to?" ambiguity at award time.

### 7.8 `sessions`

`id`, `token_hash` (text NOT NULL **UNIQUE**), `principal_type` (enum `ATTENDEE` | `STAFF`), `user_id` FK NULL, `staff_account_id` FK NULL, `issued_at`, `expires_at`, `revoked_at` NULL, `last_seen_at`, `user_agent`, `ip`.

*Constraints:* exactly one principal —
```sql
CHECK (num_nonnulls(user_id, staff_account_id) = 1)
CHECK ((principal_type = 'ATTENDEE' AND user_id IS NOT NULL)
    OR (principal_type = 'STAFF'    AND staff_account_id IS NOT NULL))
```
*Index:* `(token_hash)` unique; `(expires_at)` for cleanup.
**Only the hash of the session token is stored.** A database leak must not yield usable sessions. Admin sessions use `principal_type = 'STAFF'`; the admin/staff distinction comes from `staff_accounts.role`, so there is one role source, not two.

### 7.9 `tasks`

`id`, `booth_id` FK NOT NULL, `title`, `description`, `is_active`, `sort_order`.
*Responsibility:* the activity described to attendees ("try the live demo"). It is **descriptive**, not the authorization object — the staff flow in §13 awards points without selecting a task, so `points_log.task_id` is nullable. Tasks exist so the booth detail screen can tell attendees what to do.

### 7.10 `task_preset_points`

`id`, `booth_id` FK **NULL**, `points` int NOT NULL, `label` text, `sort_order` int, `is_active` boolean.
*Constraints:* **`UNIQUE (booth_id, points)`**; `CHECK (points > 0 AND points <= 100)`.
*Responsibility:* the allowed award values. `booth_id IS NULL` rows are the event-wide defaults (+5/+10/+15); a booth with its own rows overrides the defaults entirely.
*Resolution:* `presets(booth) = rows WHERE booth_id = booth.id AND is_active`, falling back to `rows WHERE booth_id IS NULL AND is_active` when the first set is empty.
This table is **the server-side allow-list** that makes "staff cannot enter arbitrary point values" enforceable rather than aspirational (§14.3).

### 7.11 `feedback`

`id`, `user_id` FK NOT NULL, `booth_id` FK NOT NULL, `rating` smallint NOT NULL, `comment` text NULL, `created_at`.
*Constraints:* `CHECK (rating BETWEEN 1 AND 5)`.
*Index:* `(booth_id, created_at DESC)`; `(user_id, booth_id)`.
*Deliberately NOT unique on `(user_id, booth_id)`* — the specification says feedback is rewarded once per booth, not that it may only be submitted once. Multiple submissions are allowed and stored; only the *reward* is de-duplicated (§15). Making feedback itself unique would silently reject a second, more considered comment.

### 7.12 `points_log` — the source of truth

| Field | Type | Notes |
|---|---|---|
| `id` | bigint PK IDENTITY | monotonic; doubles as the SSE event id |
| `user_id` | FK→users NOT NULL | who earned |
| `booth_id` | FK→booths **NULL** | null only for future non-booth sources |
| `source` | enum `TASK` \| `FEEDBACK` NOT NULL | extensible |
| `points` | int NOT NULL | |
| `task_id` | FK→tasks NULL | |
| `entered_by_staff_id` | FK→staff_accounts **NULL** | NULL ⇔ system-generated |
| `entered_by_system` | boolean NOT NULL DEFAULT false | |
| `idempotency_key` | uuid **NULL, UNIQUE** | client-supplied per interaction |
| `dedupe_key` | text **NULL, UNIQUE** | business-rule de-duplication (§14.5) |
| `note` | text NULL | |
| `created_at` | timestamptz NOT NULL DEFAULT now() | |

*Constraints and indexes:*
```sql
CHECK (points <> 0)
CHECK ((source = 'TASK'     AND booth_id IS NOT NULL
                            AND entered_by_staff_id IS NOT NULL
                            AND entered_by_system = false)
    OR (source = 'FEEDBACK' AND booth_id IS NOT NULL
                            AND entered_by_staff_id IS NULL
                            AND entered_by_system = true))
UNIQUE (idempotency_key)          -- NULLs do not collide
UNIQUE (dedupe_key)               -- NULLs do not collide
INDEX  (user_id)                  -- leaderboard aggregation
INDEX  (user_id, created_at DESC) -- attendee point history
INDEX  (booth_id, created_at DESC)
```

**This table is append-only by policy.** No `UPDATE`, no `DELETE` in application code. A correction is a new compensating row with negative points — which is why the check is `points <> 0` rather than `points > 0`. This preserves the audit trail and means the leaderboard can never disagree with history. The demo does not expose a correction UI, but the schema does not have to change to add one.

`entered_by_staff_id` is populated from the **authenticated session**, never from the request body.

### 7.13 `event_config` (added entity)

Single row enforced by `CHECK (id = 1)`. Fields: `feedback_points` int NOT NULL DEFAULT 5, `allow_repeat_task_points_default` boolean NOT NULL DEFAULT false, `leaderboard_top_n` int NOT NULL DEFAULT 10, `updated_at`, `updated_by_staff_id`.

*Justification:* the feedback reward and the repeat-task rule are product decisions that may need to change **between demo runs, at the venue, without a redeploy**. Putting them in code would mean a rebuild during an event; putting them in environment variables would mean a container restart and no audit of who changed what. A one-row table is the least complex option that avoids both.

### 7.14 Concurrency summary

Every rule that must hold under concurrent requests is backed by a **unique index**, not by a read-then-write check:

| Rule | Mechanism |
|---|---|
| One live contact card per attendee | partial unique `(user_id) WHERE revoked_at IS NULL` |
| No duplicate feedback reward | unique `dedupe_key` = `FEEDBACK:{user_id}:{booth_id}` |
| No repeat task reward (when disallowed) | unique `dedupe_key` = `TASK:{user_id}:{booth_id}` |
| No double-submit / retry duplication | unique `idempotency_key` |
| One interest selection per pair | PK `(user_id, interest_id)` |
| Booth staff always have a booth | `CHECK` constraint |

The application catches `IntegrityError`, inspects the violated constraint name, and maps it to a domain error. A `SELECT` that checks for an existing row before inserting is **explicitly forbidden as an enforcement mechanism**; it may only be used to produce a nicer message before the authoritative insert is attempted.

---

## 8. Attendee Registration & Authentication

### 8.1 Registration

`POST /api/attendees/register` accepts first name, last name, email, phone, profession, optional LinkedIn URL, interest slugs, and sponsor consent.

Server-side rules:
- Email normalised (trim, lowercase) and validated; `citext` uniqueness is enforced by the database, and a conflict returns `EMAIL_ALREADY_REGISTERED` — never a 500.
- Interests are resolved **by slug against active rows**. Unknown slugs are rejected with `UNKNOWN_INTEREST`, rather than silently dropped, so an attendee is never told they matched on something that does not exist.
- `sponsor_consent` is a required explicit boolean with **no default**. Absence is a validation error, not an implied "false" and certainly not an implied "true".
- LinkedIn URL is validated for scheme and host shape; stored as-is otherwise.
- Registration, interest rows, and contact card issuance happen in **one transaction**. An attendee never exists without a QR card.

On success the server issues an attendee session and returns the card token, so the attendee lands on their QR card with no second step.

### 8.2 Attendee authentication — email + OTP (no password)

The architecture is OTP-based from the start; only the *delivery channel* is simplified for the demo.

**`otp_challenges`** (entity introduced by this section, justified as the OTP store):

`id`, `email` citext NOT NULL, `user_id` FK NULL, `code_hash` text NOT NULL, `created_at`, `expires_at`, `consumed_at` NULL, `attempt_count` int NOT NULL DEFAULT 0, `request_ip` text.

| Concern | Decision |
|---|---|
| Code format | 6 digits, generated with `secrets.randbelow(1_000_000)`, zero-padded |
| Storage | **Argon2id hash only.** A 6-digit code has a 10⁶ space; storing it in plaintext makes a database read an account takeover |
| Expiration | 10 minutes, enforced in SQL (`expires_at > now()`), never by a client clock |
| Single use | `consumed_at` set in the **same transaction** as successful verification, guarded by `UPDATE ... WHERE consumed_at IS NULL` and checking the affected row count. Two concurrent verifications cannot both succeed |
| Attempt limit | 5 failed attempts invalidates the challenge; `attempt_count` incremented on every attempt |
| Request throttle | Per-email and per-IP rate limit on issuance (§21.7) |
| Enumeration | `POST /api/auth/otp/request` returns **the same response whether or not the email is registered**. Otherwise the endpoint is a membership oracle for the attendee list |
| Superseding | Requesting a new code marks prior unconsumed challenges for that email as consumed |

**Demo simplification, explicitly bounded:** when `DEMO_MODE=true`, the OTP is written to the server log and surfaced in the admin console instead of being emailed. The endpoint contract, hashing, expiry, single-use and attempt-limit logic are **identical** in both modes. `config.py` raises at startup if `DEMO_MODE=true` while `ENVIRONMENT=production`. Adding real delivery later is an email adapter, not a redesign.

### 8.3 Sessions

On successful registration or OTP verification: generate `secrets.token_urlsafe(32)`, store **only its SHA-256 hash** in `sessions`, and set it as a cookie:

`HttpOnly; SameSite=Lax; Path=/; Secure (when served over HTTPS); Max-Age = 12h`

`HttpOnly` means XSS cannot exfiltrate the session. Same-origin proxying (§3.2) is what allows `SameSite=Lax`. Logout revokes server-side (`revoked_at`), so the token is dead immediately — the property JWTs cannot offer without extra machinery.

---

## 9. Networking QR Architecture

### 9.1 Token generation

```python
public_token = secrets.token_urlsafe(32)   # 256 bits of entropy, 43 URL-safe chars
```

`secrets` is CSPRNG-backed. The token is **not derived** from the user id, email, or any other attribute — there is no function from attendee data to token, so knowing an attendee tells an attacker nothing about their token.

### 9.2 Storage and QR payload

Stored in `contact_cards.public_token` with a unique index. The QR encodes **only** an absolute URL:

```
https://<host>/c/<public_token>
```

No name, no email, no phone, no interests, no internal id, no JSON payload. A photographed QR code reveals nothing by itself; it is a capability to fetch a public card, and the server decides what that card contains.

### 9.3 Resolution — two consumers, two response shapes

The same token resolves differently depending on who asks. This is the central design point of this section.

| Consumer | Endpoint | Auth | Returns |
|---|---|---|---|
| Public / networking | `GET /api/cards/{token}` | none | first name, last name, profession, interests, **one contact action** (§9.3.1) |
| Staff scanner | `POST /api/staff/scan/resolve` | staff session required | **first name, last name, profession only** — and a short-lived `scan_ref` |

Neither response ever contains `user_id`, phone number, or sponsor-consent state. The staff response is deliberately the *minimum* needed to answer "is this the correct attendee?" — staff have no reason to see contact details, and not returning them means a compromised staff account cannot harvest them. **The two resolutions are independent**: broadening the public card never widens what staff receive.

The staff resolve additionally returns a `scan_ref`, which the award call must carry. Its construction and purpose are specified in §13.3.

#### 9.3.1 Public card contact action — RESOLVED

The public card exposes exactly **one** contact action, chosen server-side:

```
if linkedin_url is present  →  action = LINKEDIN, value = linkedin_url
else                        →  action = EMAIL,    value = email
```

```jsonc
{
  "first_name": "…", "last_name": "…", "profession": "…",
  "interests": [ { "slug": "generative-ai", "name": "Generative AI" } ],
  "contact": { "type": "LINKEDIN", "value": "https://www.linkedin.com/in/…" }
}
```

Rules, each enforced by the response schema rather than by frontend discipline:

- **Phone number is never exposed publicly.** It is not a fallback, and it is absent from the public card schema entirely — so it cannot be leaked by a future field addition.
- **Sponsor-consent state is never exposed publicly.** It governs sponsor data sharing, not card rendering, and surfacing it would disclose a private choice to anyone holding the QR.
- **The selection happens on the server.** The client receives one resolved `contact` object and renders it. It is never sent both values and asked to choose — that would mean the unused one was transmitted anyway, defeating the rule.
- Email appears **only** in the fallback case. When a LinkedIn URL exists, the email is not present in the response at all.

The tradeoff is accepted deliberately: in the fallback case an attendee's email is readable by anyone holding their QR code, which is the intended networking behaviour for a conference card. It is bounded by the fact that a token must be possessed to read anything (§9.5) and by the rate limit on `/api/cards/{token}` (§21.7), which prevents bulk harvesting. The response also carries `X-Robots-Tag: noindex`, so exposed addresses do not reach search indexes.

### 9.4 Invalid token handling

All of: malformed token, unknown token, revoked token, and token belonging to a deleted attendee produce the **identical** response — `404` with code `INVALID_QR`. Distinguishing them would turn the endpoint into an oracle that separates "never existed" from "revoked". The frontend shows one message: *"This QR code isn't valid. Ask the attendee to reopen their card."*

### 9.5 Enumeration resistance

- 256-bit random space: guessing is not a practical attack, independent of rate limiting.
- Rate limiting on `/api/cards/{token}` and the staff resolve endpoint (§21.7) bounds scraping attempts regardless.
- Tokens are never sequential and never ordered, so possessing one token gives no information about any other.
- Card URLs are excluded from crawling via `robots.txt` and `X-Robots-Tag: noindex`, so tokens do not leak into search indexes.

### 9.6 Revocation (schema-ready, not built in the demo)

`contact_cards.revoked_at` plus the partial unique index on `(user_id) WHERE revoked_at IS NULL` means rotation is: set `revoked_at` on the current row, insert a new row with a fresh token, bump `version`. Resolution filters `revoked_at IS NULL`, so the old token starts returning `INVALID_QR` immediately with no extra code path. Historical `points_log` rows reference `user_id`, not the card, so **rotation never affects a score**. No revocation UI ships in the demo; the data model does not need to change to add one.

---

## 10. Booth & Interest Model

Booths hold a weighted interest profile; attendees hold an unweighted interest set (a multi-select). Both reference the same `interests` rows.

```
AIWorks
  Artificial Intelligence : 1.0
  Generative AI           : 1.0
  Data Analytics          : 0.8
  Automation              : 0.7
```

Weights are curated by Admin and mean "how central is this topic to what this booth offers", constrained to `(0, 1]`. Attendee interests are deliberately unweighted: asking summit attendees to rank interests during registration costs conversion and buys little precision.

Recommendations consider only `booths.is_active = true` booths with at least one interest row. A booth with no interest profile is excluded from recommendations entirely rather than scored at 0 — it is unconfigured, not a bad match, and showing "0% match" for an unconfigured booth misrepresents data.

---

## 11. Matching Engine

### 11.1 Requirements restated

Deterministic, explainable, normalised 0–100, independently testable, computed server-side only, and **never** an LLM invented percentage.

### 11.2 Design

`MatchingService` has a **pure core** with no database access:

```python
def score(attendee_interest_ids: frozenset[int],
          booth_weights: Mapping[int, float],
          config: MatchConfig) -> MatchResult
```

Same inputs, same output — always. Unit-testable with literals.

### 11.3 The formula

Let `A` = attendee's selected interest ids, `B` = booth's interest ids, `w(i)` = booth weight for interest `i`, and `M = A ∩ B` (the matched interests).

```
raw      = Σ_{i ∈ M} w(i)

coverage = raw / |A|                      # how much of what YOU care about this booth serves
focus    = raw / Σ_{i ∈ B} w(i)           # how much of what the BOOTH is about is relevant to you

score01  = α · coverage + (1 − α) · focus       with α = 0.7
score    = round_half_up(100 · score01)          # integer 0–100
```

> **Implementation trap — do not use Python's built-in `round()`.** It performs banker's rounding, so `round(78.5)` is `78` while `round(79.5)` is `80`. Scores would be inconsistent at exact halves and unit tests would fail in ways that look like arithmetic errors. Use `int(Decimal(value).quantize(0, ROUND_HALF_UP))` or `math.floor(value + 0.5)`. Floating-point accumulation should also be avoided in the sums: use `Decimal` for `w(i)`, matching the `numeric(3,2)` column type, so the stored weight and the computed score agree exactly.

**Why two components.** `coverage` alone rewards a booth that lists every interest in the catalogue — it would match everyone at 100%. `focus` alone penalises broad, genuinely relevant booths and rewards narrow ones. Blending makes gaming the weights counter-productive while keeping the attendee's perspective dominant, which is why α favours coverage.

Both terms are in `[0, 1]`: every `w(i) ≤ 1`, so `raw ≤ |M| ≤ |A|`, and `raw ≤ Σ_{i∈B} w(i)` since `M ⊆ B`. Therefore `score ∈ [0, 100]` with no clamping needed — a property worth asserting in a property-based test.

**Worked example** — AIWorks as configured above; attendee selected *Generative AI* and *Data Analytics*:

```
M        = {Generative AI, Data Analytics}
raw      = 1.0 + 0.8 = 1.8
coverage = 1.8 / 2   = 0.900
focus    = 1.8 / 3.5 = 0.514
score01  = 0.7(0.900) + 0.3(0.514) = 0.630 + 0.154 = 0.784
score    = 78
```

The engineer implementing this should get exactly 78 for these inputs. That is the test fixture.

> **RESOLVED — the formula above is final, and the displayed score is always the calculated score.**
>
> The "94%" in the product brief was **illustrative, not a required output**. The honest result for that configuration is 78, and 78 is what the UI will show.
>
> **No cosmetic multiplier, curve, floor, or display transform may be applied** to make percentages look more impressive. There is exactly one number: the output of `MatchingService`. The API returns it, the UI renders it, and no layer in between adjusts it. This is the fake-percentage failure mode the whole section exists to prevent, and it is now an invariant (§24.5).
>
> The approved lever for strong headline matches is **seed data design** (§19.5): demo booths whose top-weighted interests genuinely align with the demo attendees' selected interests produce high scores *under the real formula*, because the match is real. `α` remains 0.7 and is not being tuned for presentation purposes.

### 11.4 Edge cases

| Case | Behaviour |
|---|---|
| Attendee selected no interests | No scores computed. Return booths in curated order with `match: null` and a prompt to select interests. **Never** show 0% — it reads as "bad match" rather than "no data" |
| `M = ∅` | `score = 0`, no explanation, excluded from recommendations (but visible in the full booth list) |
| Booth has no interest rows | Excluded from recommendations (§10) |
| Attendee changes interests | Scores recompute on read. Nothing is cached, so there is no staleness |

### 11.5 Explanation generation — template, not generative

Explanations are produced by a deterministic template over the **actual** matched interests, ordered by contribution `w(i)` descending, tie-broken by interest name ascending. At most three are named.

```
band(score):  ≥80 "Strong match"   60–79 "Good match"
              40–59 "Partial match"  1–39 "Some overlap"

"{band} based on your interests in {A}, {B} and {C}."
```

Only interests actually in `M` may appear. No LLM is involved at any point.

### 11.6 Ordering

Recommendations sort by `score DESC`, then `booth.name ASC`, then `booth.id ASC` — total and deterministic, so two loads of the same page never reorder.

### 11.7 API contract (the part that must not change)

```jsonc
{
  "booth": { "slug": "aiworks", "name": "AIWorks", "location_label": "Hall B · 14" },
  "match": {
    "score": 78,
    "matched_interests": [
      { "slug": "generative-ai",  "name": "Generative AI"  },
      { "slug": "data-analytics", "name": "Data Analytics" }
    ],
    "explanation": "Good match based on your interests in Generative AI and Data Analytics.",
    "algorithm_version": "weighted-v1"
  }
}
```

`algorithm_version` is the forward-compatibility hinge: it lets us compare algorithms and lets the client render an honest label without knowing how the score was produced.

### 11.8 Evolution to embeddings without a frontend change

The contract above exposes a score, matched interests, and an explanation — **not** a formula. A future `EmbeddingMatchingService` implementing the same interface can store `pgvector` embeddings of attendee and booth profiles, compute cosine similarity, and derive matched interests from nearest catalogue concepts, publishing `algorithm_version: "embedding-v1"`. The frontend needs no change. This is exactly why the score is computed server-side and why the response carries explanation text rather than the inputs to build one.

---

## 12. Staff Authentication & Authorization

### 12.1 Authentication

Staff are **created by Admin only**; there is no staff self-registration endpoint anywhere in the API surface. Login is email + password at `POST /api/staff/auth/login`, verified against the Argon2id hash. Failure returns `INVALID_CREDENTIALS` with **no distinction** between unknown email and wrong password. Brute-force handling is in §21.8. Success issues a session cookie exactly as in §8.3, with `principal_type = STAFF`.

### 12.2 The booth authorization invariant

> **A staff member can award points only for their own booth.**

Enforcement has four independent layers. Any one of them alone would be a single point of failure.

**1. The request schema cannot express a booth.**
```python
class AwardTaskPointsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_token: str
    points: int
    idempotency_key: UUID
    # there is deliberately no booth_id field
```
`extra="forbid"` means a client sending `booth_id` receives a `422`. The value cannot reach the service even by accident.

**2. The service derives the booth from the authenticated identity.** The only assignment in the codebase is:
```python
booth_id = staff.booth_id      # from the session-resolved StaffAccount
```
`PointsService.award_task()` takes a `StaffAccount`, not a `booth_id`. **It is not possible to call it with a booth of the caller's choosing** — the parameter does not exist.

**3. The schema makes the invalid state unrepresentable.** The `CHECK` constraint in §7.7 guarantees `staff.booth_id IS NOT NULL` for every `BOOTH_STAFF` row, so the derivation above can never yield null.

**4. Route-level role dependency.** `Depends(current_booth_staff)` rejects attendee sessions, admin-only accounts, inactive accounts, and expired sessions before the handler body runs.

A test asserting that a request carrying a foreign `booth_id` is rejected, and that the resulting `points_log` row carries the staff member's own booth, is a **required** test for the points phase.

### 12.3 Role boundaries

| Capability | Attendee | Booth staff | Admin |
|---|---|---|---|
| Register self | ✅ | ❌ | ❌ |
| View own card / score | ✅ | — | — |
| Resolve a card token to identity | ❌ | ✅ (name + profession only) | ✅ |
| Award task points | ❌ | ✅ own booth only | ❌ (deliberately — see below) |
| Submit feedback | ✅ | ❌ | ❌ |
| Create booths / staff / presets | ❌ | ❌ | ✅ |
| Run demo reset | ❌ | ❌ | ✅ via CLI only |

**Admins deliberately cannot award points.** Admin is a configuration role. Letting the account that manages presets also award points collapses the separation that makes the audit trail meaningful. If an admin must award, they hold a booth staff account as well — which is recorded honestly in `entered_by_staff_id`.

---

## 13. Staff QR Scan Flow

### 13.1 Steps

```
1. Staff opens panel               → GET  /api/staff/me    (booth name shown in the header,
                                                            so staff always see which booth
                                                            they are awarding for)
2. Press SCAN ATTENDEE             → camera opens (requires a secure context — §13.5)
3. QR decoded in-browser           → token extracted client-side
4. Resolve                         → POST /api/staff/scan/resolve { card_token }
5. Confirmation screen (MANDATORY) → "Attendee found — {name} · {profession}.
                                      Is this the correct attendee?"  [Cancel] [Confirm]
6. Confirm                         → preset buttons [ +5 ] [ +10 ] [ +15 ]  (from the server)
7. Award                           → POST /api/staff/points/award
                                      { card_token, points, idempotency_key }
8. Result                          → "+15 awarded to {name}" or a typed error
```

The confirmation step is **not** skippable and not a client-side convenience: the award endpoint is a separate call that a human action triggers. There is no endpoint that scans and awards in one step, because that would make a misread QR an immediate irreversible award.

### 13.2 Preset buttons come from the server

The `+5 / +10 / +15` buttons are rendered from `GET /api/staff/points/presets`, which returns the resolved presets for **the authenticated staff member's booth** (§7.10). Hardcoding them in the frontend would create a second source of truth for allowed values and let a stale client offer values the server rejects.

### 13.3 What the award call carries

The award carries the **card token** again, not a user id — consistent with "QR never contains raw internal user IDs" and meaning the award endpoint performs its own authoritative resolution rather than trusting a value the client held between two calls.

The resolve step additionally returns `scan_ref`: an HMAC-SHA256 signature over `(staff_account_id, card_token, issued_at)`, keyed by the application secret from configuration (§21.5 — no fallback default), with a 120-second validity window. The award endpoint recomputes and compares it in constant time.

It binds *this staff member* to *this token* for *this short window*. Without it, a staff client could award against any token it obtained by other means — a photograph of someone's card, a value pasted from a log — without ever having scanned it in their own session. It also bounds how long a scan stays actionable, so a phone left on a confirmation screen during a coffee break cannot award an hour later. Cost: one response field and one signature check.

Being stateless (a signature, not a stored row) it needs no table and no cleanup job, and it survives the single-worker constraint of §17.7 without shared state.

**Frontend simplicity contract — CONFIRMED.** `scan_ref` must add no meaningful complexity to the client. The entire client-side obligation is:

> Store the `scan_ref` string returned by resolve; send it back with the award. Do not parse it, do not inspect it, do not track its expiry with a timer.

The client treats it as an opaque string. Expiry is discovered by the server returning `SCAN_EXPIRED` (§20.3), which the UI handles with one message — *"That scan expired. Scan again."* — returning to the scanner. There is no countdown, no refresh logic, and no client-side clock comparison, because a client clock cannot be trusted for a security decision anyway and a timer would be a second source of truth about validity.

**Required test matrix (implementation plan).** Because this mechanism sits directly on the demo's critical path, reliability is treated as equal in weight to its security value. Phase-2 work is not complete until all seven pass:

| # | Case | Expected |
|---|---|---|
| 1 | Generation | Resolve returns a well-formed `scan_ref`; award with it succeeds |
| 2 | Expiry | `scan_ref` older than 120 s → `SCAN_EXPIRED`, no `points_log` row written |
| 3 | Tampering | Any modified byte → `INVALID_QR`, no row written, constant-time comparison |
| 4 | Wrong staff account | `scan_ref` issued to staff A, award attempted by staff B → rejected, no row written |
| 5 | Repeated award | Same `idempotency_key` replayed → original result returned, exactly one row total |
| 6 | Happy path | Full scan → confirm → award cycle succeeds end-to-end on a real device |
| 7 | Presentation latency | Resolve and award each complete well inside a comfortable on-stage pause; the measured end-to-end scan-to-points-visible time is recorded, including the SSE update on the attendee's phone |

Cases 4 and 5 are the security-critical ones; cases 2, 6 and 7 are the demo-critical ones. Case 7 is measured rather than assumed, because a mechanism that is secure but visibly slow on stage still fails the presentation.

### 13.4 Idempotency

The client generates a UUIDv4 **when the confirmation screen opens** — not per click. Every retry of that award, from a double-tap or a flaky network, carries the same key. The unique index on `idempotency_key` means the second insert fails; the service catches it, loads the original row, and returns the **same successful response**. A double-tap therefore shows one award, not an error — the correct experience, and one the frontend cannot guarantee by disabling a button.

### 13.5 Secure context and camera access — RESOLVED

**Decision: real QR camera scanning is mandatory for the summit demo.** The presentation environment must serve the frontend over a secure HTTPS context that iPhone Safari and Android Chrome both accept. This is a hard requirement, not a preference.

`navigator.mediaDevices.getUserMedia` requires a **secure context**: HTTPS, or `localhost`. A staff phone pointed at `http://192.168.x.x:3000` over venue Wi-Fi **will be denied camera access by the browser** on both target platforms. No application code can work around this, so it is resolved at the infrastructure layer.

**Deployment mechanism is deliberately left replaceable.** No paid or vendor-specific tunnelling product is selected at this stage. The architecture requires only that the frontend origin satisfies three properties:

1. Served over HTTPS with a certificate both target browsers trust.
2. Reachable from both demo phones on the venue network.
3. A single origin, so the `/api/*` rewrite proxy (§3.2) and `SameSite=Lax` cookies continue to work unchanged.

Any mechanism meeting those three properties is acceptable and swappable without touching application code — a locally-trusted certificate for the LAN host, a reverse proxy terminating TLS, or a tunnel. The choice is made in the infrastructure phase against these criteria, and changing it later is a configuration change, not a redesign.

**Manual token entry is an emergency fallback only.** It exists so a presenter is never stranded mid-demo, and for development on machines without a camera. It is explicitly **not** the primary demo flow, must not be presented as the normal path, and must not be reached by default — it is available behind a deliberate action after a camera failure. The scanner handles `NotAllowedError` and `NotFoundError` from `getUserMedia` and surfaces `CAMERA_PERMISSION_DENIED` with recovery instructions before offering it.

**Required milestone (implementation plan).** A dedicated milestone, scheduled *before* presentation day and not merged into general frontend work:

> **M-CAM — Real-device HTTPS camera verification.** On a physical iPhone (Safari) *and* a physical Android device (Chrome), over the actual presentation network: load the staff scanner over HTTPS, grant camera permission, scan a printed attendee QR code, and complete a full scan → confirm → award cycle. Passing requires real hardware and real printed codes; a desktop browser, a simulator, or a screen-displayed QR does not satisfy it.

Failing M-CAM blocks the demo, so it is scheduled with enough lead time to change the HTTPS mechanism if the first choice does not work on a target device.

---

## 14. Points Engine

### 14.1 Source of truth

`points_log` is the **only** place points exist. There is no `users.total_points` column. A user's score is `SELECT COALESCE(SUM(points), 0) FROM points_log WHERE user_id = ?`.

A denormalised total is rejected because it creates two facts that can disagree, and the failure is silent — a score that drifts is invisible until someone notices the leaderboard is wrong, probably on stage. At demo scale (hundreds of attendees, thousands of rows) the aggregate is sub-millisecond with the `(user_id)` index. §16.5 covers the production path if that ever stops being true.

### 14.2 `PointsService` — the only writer

Every write to `points_log` goes through `PointsService`. Repositories for other modules do not insert into it. Two entry points:

```python
def award_task(staff: StaffAccount, card_token: str, points: int,
               idempotency_key: UUID, scan_ref: str) -> AwardResult
def award_feedback(user: User, booth: Booth, feedback: Feedback) -> AwardResult | None
```

### 14.3 Validation sequence for `award_task`

Ordered so that cheap and security-relevant checks precede expensive ones:

1. **Session → staff account.** Expired/revoked session → `UNAUTHENTICATED`.
2. **Role check.** Not `BOOTH_STAFF`, or inactive → `STAFF_NOT_AUTHORIZED`.
3. **`booth_id = staff.booth_id`.** Derived, never read from input (§12.2).
4. **Resolve the token** → attendee, or `INVALID_QR`.
5. **Validate `scan_ref`.** Signature mismatch → `INVALID_QR` (indistinguishable from a bad token, since a forged signature is an attack and deserves no detail). Valid signature but older than 120 s → `SCAN_EXPIRED`, which tells staff to simply scan again. The distinction is safe because expiry reveals nothing about any attendee — only about the staff member's own session timing.
6. **`points ∈ presets(booth)`** — membership in the server-side allow-list, not a range check. `12` is rejected even though it lies between 10 and 15 → `INVALID_POINT_VALUE`.
7. **Compute `dedupe_key`** (§14.5).
8. **Single `INSERT`.** Constraint violations are translated (§14.6).
9. **Recompute the total inside the same transaction** and return it.
10. **Publish the realtime event *after* commit** (§17.6).

### 14.4 Feedback points

Awarded by the system: `entered_by_system = true`, `entered_by_staff_id = NULL`, amount from `event_config.feedback_points`. Never chosen by any client. Details in §15.

### 14.5 The open repetition rule — `allow_repeat_task_points`

The product has not permanently decided whether an attendee may earn task points repeatedly at the same booth. The architecture therefore treats this as **configuration, not structure**.

**Where it lives:** `booths.allow_repeat_task_points` (nullable) overriding `event_config.allow_repeat_task_points_default`.

```python
allow_repeat = booth.allow_repeat_task_points
if allow_repeat is None:
    allow_repeat = event_config.allow_repeat_task_points_default   # demo default: False
```

**How it is enforced — the `dedupe_key` mechanism.** A static unique index on `(user_id, booth_id) WHERE source='TASK'` would hard-code "no repeats" into the schema, requiring a migration to change a product decision. Instead the service computes the key and the database enforces whatever was computed:

| Situation | `dedupe_key` |
|---|---|
| `FEEDBACK` | `"FEEDBACK:{user_id}:{booth_id}"` — **always**, unconditionally |
| `TASK`, repeats disallowed | `"TASK:{user_id}:{booth_id}"` |
| `TASK`, repeats allowed | `NULL` |

Because Postgres unique indexes **do not collide on NULLs**, a NULL key means unlimited awards, while a non-NULL key is enforced atomically by the index. One column, one index, both policies — and flipping the config changes behaviour for future awards with no migration and no code change.

Two consequences to state plainly:
- Flipping from disallow→allow does **not** retroactively delete existing keys. Attendees who already earned at that booth under the old rule cannot earn a second time *under that key*. For a demo this is correct; a production change would need a documented backfill.
- Feedback de-duplication is **not** configurable. It is a stated invariant, so it is unconditional.

### 14.6 Threat/failure matrix

| Threat | Defence |
|---|---|
| Arbitrary point values | Server-side allow-list membership (§14.3 step 6); `INVALID_POINT_VALUE` |
| Client-supplied `booth_id` | Field does not exist; `extra="forbid"`; booth derived from session (§12.2) |
| Double-click | `idempotency_key` unique index → returns the original result, not an error |
| Duplicate network retry | Same mechanism; key is generated once per confirmation screen |
| Unauthorized staff | Session → role → active checks before any mutation |
| Duplicate reward where prohibited | `dedupe_key` unique index, enforced by the database under concurrency |
| Awarding to a token never scanned in this session | `scan_ref` binding (§13.3) |
| Replay of an old award payload | `idempotency_key` already consumed → original result returned, no new points |
| Staff account compromise | Blast radius bounded to one booth and to preset values; every row is attributed and reversible by compensating entry |

---

## 15. Feedback Reward Flow

`POST /api/attendees/feedback` with booth slug, rating and optional comment, in a **single transaction**:

```
BEGIN
  1. INSERT feedback (user_id, booth_id, rating, comment)
  2. attempt INSERT points_log
       source='FEEDBACK', points=event_config.feedback_points,
       entered_by_system=true,
       dedupe_key='FEEDBACK:{user_id}:{booth_id}'
     ├─ success           → awarded_points = N
     └─ unique violation  → SAVEPOINT rollback; awarded_points = 0
  3. recompute total
COMMIT
  4. publish realtime event only if awarded_points > 0
```

**Outcome:** the feedback is always saved; the reward is granted at most once per `(user, booth)`. A second submission returns `200` with `awarded_points: 0` and `already_rewarded: true` — not an error, because submitting more feedback is legitimate behaviour that simply is not paid twice.

**Concurrency.** Two simultaneous submissions (double-tap, or two tabs) both attempt the insert; the unique index guarantees exactly one succeeds. The loser is detected by catching the integrity error on a `SAVEPOINT`, so the enclosing transaction survives and the feedback row is still committed. There is **no** "check if already rewarded, then insert" path — that pattern has a window between the check and the insert and would double-award under exactly the conditions a live demo produces. Frontend button disabling is a UX nicety layered on top and is never relied upon.

---

## 16. Leaderboard

### 16.1 Calculation

```sql
SELECT u.id, u.first_name, u.last_name, u.profession,
       COALESCE(SUM(p.points), 0) AS total_points,
       MAX(p.created_at)          AS last_award_at
FROM users u
LEFT JOIN points_log p ON p.user_id = u.id
GROUP BY u.id
```

The `LEFT JOIN` is what makes **every registered attendee rankable, including those with zero points** — an `INNER JOIN` would silently drop them, and a demo where a newly registered attendee is missing from the list looks broken.

### 16.2 Deterministic ordering and ranking

```sql
ORDER BY total_points DESC,
         last_award_at ASC NULLS LAST,   -- reached the total earlier ⇒ ranked higher
         u.id ASC                        -- final, absolute tiebreak
```

The three keys are **total**, so row order is identical on every query — no flickering between refreshes or between two phones on stage.

Displayed rank uses **competition ranking on points only**:
```sql
RANK() OVER (ORDER BY total_points DESC) AS rank
```
so two attendees on 45 points both show rank 3 and the next shows 5. Ties are shown honestly as ties, while list order remains deterministic via the tiebreakers.

### 16.3 Views

| View | Behaviour |
|---|---|
| Default (attendee) | Top N (`event_config.leaderboard_top_n`, default 10) |
| Current attendee outside top N | Their own row is returned **additionally** in a `current_user` field, with true rank — one query using a window function over the full set, not a second round trip |
| Show all | Paginated full list, keyset pagination on `(total_points DESC, last_award_at ASC, id ASC)` |

`current_user` is always populated for an authenticated attendee, even when they are inside the top N, so the client renders one consistent component and the highlight logic has no special case.

### 16.4 No caching in the demo — justified

At summit scale (hundreds of attendees, low thousands of point rows) this aggregation with the `(user_id)` index completes in single-digit milliseconds. A cache would add an invalidation path, a staleness window, and a second thing that can be wrong on stage — while solving a problem we do not have. **No caching is introduced.**

### 16.5 Production path (not built now)

In order of escalation: (1) materialised view refreshed on award; (2) a `user_scores` projection table updated in the same transaction as the log insert, treated explicitly as a **rebuildable cache** with a reconciliation job; (3) a Redis sorted set for very large events. All three keep `points_log` authoritative — none of them may become a second source of truth.

---

## 17. Realtime Architecture

### 17.1 The scenario to satisfy

Phone A (attendee) has the score screen open. Phone B (staff) scans A and awards +15. Phone A must update total, rank and leaderboard **without a manual refresh**, and may animate `+15 POINTS`.

### 17.2 Decision: Server-Sent Events

| Option | Assessment |
|---|---|
| **SSE — chosen** | One-directional server→client, exactly our shape. Plain HTTP, so it traverses the Next.js rewrite proxy with no protocol upgrade. **Automatic browser reconnection with `Last-Event-ID` is built into `EventSource`** — the reconnect logic we would otherwise write by hand. FastAPI ships first-party support (`fastapi.sse.EventSourceResponse`, `ServerSentEvent` with `id`/`event`/`retry`), so no third-party dependency |
| WebSockets | Rejected: bidirectional capability we do not need, hand-rolled reconnect and heartbeat, an upgrade that proxies can mishandle, and a second auth path. Strictly more failure modes for zero additional capability |
| Polling only | Rejected as primary (a 5 s delay is visibly sluggish on stage) but **retained as fallback**, because it works in every environment |

The deciding factor is that the reconnect behaviour we most need is the part `EventSource` already implements correctly.

### 17.3 Endpoint and events

`GET /api/realtime/stream` — attendee session required. One connection carries **two channels**, and the distinction is a security boundary:

| Channel | Subscription key | Carries | Visible to |
|---|---|---|---|
| **Personal** | `user_id` from the session | `points_awarded` — your points, your total, your rank | Only that attendee |
| **Broadcast** | global topic | `leaderboard_changed` — a bare signal, **no names, no scores, no user ids** | Every connected attendee |

The subscription key for the personal channel comes from the **session**, never from a query parameter, so **a client cannot request another user's stream**.

The broadcast channel exists because the requirement is that the *leaderboard* updates live. When attendee X is awarded points, attendee Y's **rank can change without Y earning anything**. A purely per-user stream would leave every other phone in the room showing a stale leaderboard — the failure would be invisible in single-device testing and glaring on stage with two phones. The broadcast event deliberately carries no data beyond a version number: it is a "refetch now" signal, so it leaks nothing about other attendees and cannot itself become a source of truth.

```
event: points_awarded         ← personal channel only
id: 481                       ← points_log.id — monotonic, and already our event id
data: {"points":15,"source":"TASK","booth_name":"AIWorks",
       "total_points":45,"rank":3,"replay":false}

event: leaderboard_changed    ← broadcast to all connected attendees
id: 481
data: {"version":481}         ← signal only; client refetches the leaderboard

event: heartbeat              ← every 20 s, keeps proxies from idling the connection
```

On `leaderboard_changed` the client refetches `/api/leaderboard` (which already includes its own `current_user` rank, §16.3). It **never animates** on a broadcast event — the `+N` animation belongs exclusively to the personal channel, so other people scoring never triggers celebration on your phone.

Using `points_log.id` as the SSE id is deliberate: the event identifier **is** the identifier of the durable fact, so client de-duplication and server backfill share one ordering with nothing to keep in sync.

### 17.4 Reconnect behaviour

`EventSource` reconnects automatically (we send `retry: 5000`). On reconnect the browser sends `Last-Event-ID`; the server reads it and replays **personal-channel** `points_log` rows for that attendee with `id > Last-Event-ID`, marking each `"replay": true`. Missed awards during a Wi-Fi blip therefore still arrive.

Broadcast events are **not** replayed individually — the server sends a single `leaderboard_changed` on every reconnect, because the leaderboard is refetched wholesale anyway and replaying N stale signals would produce N identical refetches.

Additionally, the client performs a **full state refetch** of `/api/attendees/me/score` on reconnect and on `visibilitychange → visible`. This is the safety net that makes the whole design tolerable: even if events were lost entirely, the screen self-heals within a second of coming back.

### 17.5 Duplicate events and animation replay

The client keeps `lastSeenPointsLogId` in memory (**not** `localStorage` — it must not survive a page reload, or a genuine post-refresh event would be suppressed).

It is **initialised from the API, not from zero.** `GET /api/attendees/me/score` returns `last_points_log_id` alongside the total, and the client seeds `lastSeenPointsLogId` with it before opening the stream. Initialising to `0` would leave a window between the initial fetch and the SSE connection: an award landing in that gap is already included in the fetched total, but its event would still pass the `>` check and animate a `+15` the attendee has already been shown. The total would stay correct — the payload is authoritative — but the animation would be a lie about recency.

```
on event (personal channel):
  if (event.id <= lastSeenPointsLogId) → ignore entirely
  lastSeenPointsLogId = event.id
  apply total/rank from payload
  animate "+N" only if (event.replay === false && documentIsVisible)

on event (broadcast channel):
  refetch leaderboard; never animate
```

This is precisely how the "+15 replayed on every reconnect" failure is avoided: backfilled events update state but are flagged `replay: true` and never animate. A phone that was locked during the award shows the correct total when unlocked, without a burst of stale confetti.

### 17.6 Realtime is never the source of truth

Three rules:
1. Events are published **after** the database transaction commits. A rolled-back award cannot emit an event. (Publishing inside the transaction is the classic bug here: the event escapes, the transaction aborts, and the UI shows points that do not exist.)
2. Event payloads carry **server-computed** values, never client-computed deltas.
3. Any disagreement is resolved by refetching from the API. The client never adds `+15` to its own local total as an independent calculation.

### 17.7 Transport and the worker constraint

The demo uses an **in-process async pub/sub broker**: a dict of `user_id → set[asyncio.Queue]`. No Redis, no broker service.

> **Invariant:** the backend runs with **exactly one Uvicorn worker** in the demo. With multiple workers, an award handled by worker 2 would not reach a stream held by worker 1.

This is enforced by the container command and stated in the README rather than left to chance. The production path is Postgres `LISTEN/NOTIFY` — which needs no new infrastructure since Postgres is already present — or Redis pub/sub at larger scale. The `EventBus` is an interface precisely so this swap touches one class.

### 17.8 Fallback

If `EventSource` fails to connect, or after 3 consecutive failed reconnects, the client falls back to polling `/api/attendees/me/score` every 5 s and retries SSE every 60 s. Polled changes update values **without** animation, since a poll cannot distinguish new from missed. On a fallback-polled increase the UI updates silently rather than lying about recency.

---

## 18. Admin Boundary

Admin capabilities: manage interests, booths and their interest weights, create and deactivate staff accounts, manage tasks and preset point values, edit `event_config`, and inspect the points audit trail.

Boundaries:
- All admin routes are under `/api/admin/*` behind `Depends(current_admin)`, which requires `principal_type = STAFF` **and** `role = ADMIN`. A booth staff session receives `403 STAFF_NOT_AUTHORIZED` on every route in the namespace.
- Admins **cannot award or modify points** (§12.3). The audit view is read-only. Corrections, if ever built, are compensating entries attributed to a real account.
- The first admin is created by the seed/CLI path, never by an HTTP endpoint. There is no bootstrap route that creates an admin if none exists — that is a backdoor by another name.
- Admin routes are excluded from the public OpenAPI schema in production and rate-limited (§21.9).
- Editing a booth's interest weights changes future match scores. Nothing is cached (§11.4), so the change takes effect on the next request with no invalidation step.

---

## 19. Demo Reset Strategy

### 19.1 Mechanism

A CLI command run inside the backend container:

```bash
docker compose exec backend python -m app.cli reset-demo --confirm
```

**No HTTP endpoint exists for reset.** Not an authenticated one, not a hidden one. The reason is blunt: an endpoint that destroys all event data is a liability whose worst case (someone triggers it mid-summit) vastly outweighs the convenience it buys, and CLI access is already available to whoever runs the demo.

### 19.2 Guards

The command refuses to run unless **both**:
- `DEMO_MODE=true`, and
- `ENVIRONMENT != production`

and it requires the explicit `--confirm` flag. Failing any check aborts with a non-zero exit and no writes.

### 19.3 Sequence

```
1. Verify guards                      → abort if any fail
2. TRUNCATE ... RESTART IDENTITY CASCADE:
     points_log, feedback, sessions, otp_challenges,
     user_interests, contact_cards, users
   (attendee-generated data only)
3. Upsert reference data from seed/demo_fixtures.py:
     interests, booths, booth_interests,
     tasks, task_preset_points, event_config,
     staff_accounts (fixed emails; passwords from env, never literals)
4. Insert demo attendees with FIXED public_tokens
5. Optionally seed a small baseline of points so the leaderboard is not empty on stage
6. Print a summary: counts, staff logins, demo card URLs
```

### 19.4 Determinism — and the printed-QR requirement

The seed uses **fixed** `public_token` values for demo attendees. This is not cosmetic: pre-printed QR cards and slide screenshots must keep working after every reset. Random tokens would invalidate the printed materials the moment the demo is reset, which is exactly when it matters.

`RESTART IDENTITY` makes ids reproducible, so a reset returns to a genuinely known state rather than an equivalent one.

**Security note:** fixed demo tokens are acceptable **only** because they are demo identities in a demo-mode deployment. Seeded tokens are generated once, committed as fixtures, and the guard in §19.2 prevents that fixture ever loading into production. Real attendees always receive `secrets`-generated tokens.

Staff passwords come from environment variables with no fallback default — the CLI fails loudly if they are unset, rather than seeding a known password.

### 19.5 Seed data must produce genuinely strong matches

Because no cosmetic transform is permitted (§11.3), the demo's headline match percentages are entirely a function of the seed data. Designing that data is therefore a **required deliverable of the seeding work**, not an afterthought.

Constraints on the fixture set:

- At least one demo attendee must have a **genuinely strong** alignment with at least one demo booth — their selected interests are that booth's top-weighted interests — so the computed score naturally lands high.
- The fixture set must also contain **mid and low matches**, so the recommendation list visibly discriminates. A demo where every booth shows 90%+ demonstrates nothing and invites the suspicion that the number is decorative.
- Interest catalogue and booth weights are curated together, so that `coverage` is high (the booth serves most of what the attendee picked) *and* `focus` is respectable (those interests are central to the booth). Both terms matter — §11.3.
- Expected scores for the demo fixtures are **computed by hand and asserted in tests**. If a seed change moves a headline number, the test fails and the demo script is updated — rather than the discrepancy being discovered on stage.

This keeps the demonstration honest: the impressive number is impressive *because the match is real*.

---

## 20. Error Handling

### 20.1 Three distinct layers

| Layer | Form | Example |
|---|---|---|
| **Domain** | Python exception carrying a stable `code` | `DuplicateFeedbackReward` |
| **API** | JSON envelope + HTTP status, produced by one exception handler | `409 {"error": {"code": "DUPLICATE_FEEDBACK", ...}}` |
| **Frontend presentation** | Human copy chosen by code, plus codes that never touch the server | `CAMERA_PERMISSION_DENIED`, `NETWORK_ERROR` |

These stay separate so that server error semantics and user-facing wording change independently, and so that frontend-only conditions are never faked as fictional HTTP responses.

### 20.2 Envelope

```jsonc
{
  "error": {
    "code": "DUPLICATE_TASK_REWARD",
    "message": "This attendee has already earned points at this booth.",
    "details": { "booth": "AIWorks" },
    "correlation_id": "0f7c2a1e-..."
  }
}
```

One shape for every failure, so the client has exactly one parsing path. `message` is safe to display. `correlation_id` is logged server-side with the full traceback, letting a developer find the cause from what the user saw without the user ever seeing internals.

### 20.3 Code catalogue

| Code | Status | Layer | Raised when |
|---|---|---|---|
| `VALIDATION_ERROR` | 422 | API | Pydantic rejects the payload |
| `INVALID_QR` | 404 | Domain | Token unknown, malformed, revoked — or a `scan_ref` whose signature does not verify |
| `SCAN_EXPIRED` | 400 | Domain | `scan_ref` signature is valid but older than 120 s |
| `USER_NOT_FOUND` | 404 | Domain | Attendee referenced but absent |
| `INVALID_POINT_VALUE` | 400 | Domain | Value not in the booth's preset allow-list |
| `DUPLICATE_TASK_REWARD` | 409 | Domain | `dedupe_key` collision on a TASK award |
| `DUPLICATE_FEEDBACK` | 409 | Domain | Feedback reward already granted (surfaced as `already_rewarded`, §15) |
| `STAFF_NOT_AUTHORIZED` | 403 | Domain | Wrong role, inactive account, or admin-only route |
| `INVALID_CREDENTIALS` | 401 | Domain | Login failure — never distinguishes unknown email from wrong password |
| `UNAUTHENTICATED` | 401 | API | Missing, expired or revoked session |
| `ACCOUNT_LOCKED` | 429 | Domain | Too many failed logins |
| `OTP_INVALID` | 400 | Domain | Wrong, expired, consumed, or attempt-exhausted code — one code for all four |
| `EMAIL_ALREADY_REGISTERED` | 409 | Domain | Registration email conflict |
| `UNKNOWN_INTEREST` | 400 | Domain | Interest slug not in the catalogue |
| `RATE_LIMITED` | 429 | API | Throttle exceeded |
| `INTERNAL_ERROR` | 500 | API | Anything unhandled — generic message only |
| `CAMERA_PERMISSION_DENIED` | — | Frontend | `getUserMedia` denied/unavailable |
| `NETWORK_ERROR` | — | Frontend | Fetch failed / offline |

### 20.4 Rules

- A catch-all handler converts any unhandled exception to `INTERNAL_ERROR` with a generic message. **Stack traces are never serialised to a client**, and `DEBUG` is false outside local development.
- `INVALID_QR`, `INVALID_CREDENTIALS` and `OTP_INVALID` are each deliberately coarse — each collapses several internal causes so the API cannot be used as an oracle.
- Integrity errors are translated by constraint name into domain errors; a raw `IntegrityError` reaching the handler is a bug, and is logged as one.
- The frontend maps code → copy via a single lookup, with a generic fallback for unrecognised codes so an older client degrades gracefully instead of rendering nothing.

---

## 21. Security Model

**21.1 Password hashing.** Argon2id via `argon2-cffi` (`t=2, m=64 MiB, p=1`, tuned to ~100 ms on target hardware). Only encoded hashes are stored. Plaintext passwords are never stored, never logged, and never returned. `verify_and_update` rehashes on parameter change.

**21.2 QR tokens.** 256-bit CSPRNG, not derived from any attendee attribute, unique-indexed, revocable, carrying no PII in the payload (§9). Card URLs are `noindex` and excluded in `robots.txt`.

**21.3 Server-side authorization.** Every protected route declares a role dependency. The booth invariant is enforced in four layers (§12.2). The frontend hides controls for usability only; hiding a button is never a security control, and every hidden action is independently rejected server-side.

**21.4 Input validation.** Pydantic v2 models on every request, with `extra="forbid"` on all mutating schemas so unexpected fields are rejected rather than ignored. Constrained types for rating (1–5), points (int, allow-list) and string lengths. Output schemas are explicit, so a model gaining a sensitive column does not silently start leaking it.

**21.5 Secret management.** All secrets via environment variables loaded by `pydantic-settings`. `.env` is git-ignored; only `.env.example` with placeholders is committed. No secret, key, password or token literal appears in source. Startup **fails fast** if a required secret is missing, rather than falling back to a development default — a fallback default is how a demo secret reaches production.

**21.6 CORS.** Because the browser talks only to the Next.js origin (§3.2), CORS is not load-bearing. It is configured with an explicit origin allow-list from configuration, `allow_credentials=true`, and explicit method/header lists. **`allow_origins=["*"]` is never used**, and is incompatible with credentialed requests regardless.

**21.7 Rate limiting.** Applied where abuse is cheap and consequential:

| Endpoint | Limit |
|---|---|
| `POST /api/auth/otp/request` | 3 / 15 min per email, 10 / 15 min per IP |
| `POST /api/auth/otp/verify` | 10 / 15 min per IP (plus the 5-attempt per-challenge cap) |
| `POST /api/staff/auth/login` | 10 / 15 min per IP, plus per-account lockout (§21.8) |
| `GET /api/cards/{token}` | 60 / min per IP |
| `POST /api/staff/scan/resolve` | 60 / min per staff account |
| `POST /api/attendees/register` | 5 / hour per IP |

Demo implementation is an in-process fixed-window counter (consistent with the single-worker invariant, §17.7); production moves to a shared store. Awarding is **not** aggressively rate-limited — a busy booth is normal traffic, and idempotency plus `dedupe_key` already prevent the abuse a limit would target.

**21.8 Brute force.** Staff login: `failed_login_count` increments on failure; at 10 failures `locked_until = now() + 15 min` and further attempts return `ACCOUNT_LOCKED` without evaluating the password. The counter resets on success. OTP: 5 attempts per challenge, 10-minute expiry, single use. Both paths use constant-time comparison and return identical responses regardless of whether the identity exists.

**21.9 Admin routes.** Separate namespace, `current_admin` dependency, excluded from the production OpenAPI schema, rate-limited, and every mutation written to an audit log with the acting account and timestamp.

**21.10 SQL injection.** All queries are built through SQLAlchemy Core/ORM constructs, which parameterise by construction. Raw SQL is permitted only in Alembic migrations and the reset CLI, where it is static with no interpolation of user input. **String-formatted SQL containing request data is prohibited**, and a lint rule enforces it.

**21.11 Sensitive logging.** Never logged: passwords, password hashes, OTP codes, session tokens, QR tokens, full email addresses at info level. A log filter redacts known-sensitive keys. Requests are logged with method, path (tokens masked as `/c/***`), status and correlation id. In demo mode the OTP is logged deliberately — behind the `DEMO_MODE` guard, and stated in the README so it can never be mistaken for an accident.

**21.12 Transport and headers.** **The presentation environment must be served over HTTPS** — this is mandatory, because real camera scanning is a hard requirement and browsers gate `getUserMedia` on a secure context (§13.5). The certificate must be trusted by iPhone Safari and Android Chrome; the mechanism that provides it is deliberately replaceable. Cookies `HttpOnly`, `SameSite=Lax`, `Secure` when the scheme allows. `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, and a Content-Security-Policy without `unsafe-eval`.

**21.13 Proportionality.** Deliberately out of scope for the demo: MFA for staff, full audit of read access, key rotation, WAF, penetration testing, SOC2-style controls. These are production concerns. What is *in* scope is the refusal to ship a known flaw — no plaintext passwords, no client-trusted authorization, no guessable tokens, no reset endpoint.

---

## 22. Demo vs Production Boundaries

| Concern | Demo | Production | Cost to change |
|---|---|---|---|
| OTP delivery | Logged / shown in admin, behind `DEMO_MODE` | Email provider | New adapter; contract unchanged |
| Realtime transport | In-process pub/sub, 1 worker | Postgres `LISTEN/NOTIFY` or Redis | One `EventBus` implementation |
| Rate limiting | In-process counters | Shared store | One limiter implementation |
| Leaderboard | Live aggregate, no cache | Projection table or materialised view | Additive; `points_log` stays authoritative |
| Matching | `weighted-v1` | `embedding-v1` via pgvector | New service, same API contract (§11.8) |
| Reset | CLI, guarded | Not present at all | Guard already refuses production |
| Demo QR tokens | Fixed in seed fixtures | Always random per attendee | Same code path; fixtures do not load |
| Staff credentials | Seeded from env vars | Admin-created, per person | Same table and flow |
| Secrets | `.env` (git-ignored) | Secret manager | `pydantic-settings` reads either |
| Workers | Exactly 1 (invariant) | N, once `EventBus` is swapped | Blocked on the transport change only |
| HTTPS | Mandatory; mechanism chosen against three properties (§13.5) and deliberately not vendor-locked | Managed TLS at the edge | Configuration only — no application code depends on which mechanism provides the certificate |
| QR scanning | Real camera; manual entry is an emergency fallback | Real camera only | Fallback may simply be removed |

The pattern: demo shortcuts are **implementations behind interfaces**, never structural simplifications. Nothing listed above requires a schema change or a frontend change.

---

## 23. Known Product Decisions / Open Questions

### 23.1 Decided (recorded so they are not silently revisited)

1. `points_log` is append-only; corrections are compensating rows.
2. Feedback may be submitted repeatedly; the reward is granted once per `(user, booth)`.
3. Admins cannot award points.
4. Staff see only name and profession when resolving a card.
5. Attendee interests are unweighted.
6. No caching in the demo.
7. Exactly one backend worker in the demo.
8. No HTTP reset endpoint.
9. Sessions are opaque and server-side, not JWTs.

### 23.2 RESOLVED — Phase 1 final decisions

These four were open at the end of Phase 1 and have since been decided. They are recorded here so the reasoning is not relitigated, with the sections that implement them.

| # | Question | **Resolution** | Implemented in |
|---|---|---|---|
| R1 | Venue HTTPS / camera access | **Real camera scanning is mandatory.** The presentation environment must serve HTTPS acceptable to iPhone Safari and Android Chrome. No paid or vendor-specific tunnel is selected; the mechanism is defined by three properties and stays replaceable. Manual token entry is a development and emergency fallback only, never the primary flow. Milestone **M-CAM** verifies real-device camera access before presentation day | §13.5, §21.12 |
| R2 | Match scores and the "94%" example | **Formula unchanged; displayed score is always the calculated score.** No cosmetic multiplier or transform of any kind. The 94% was illustrative. Strong headline matches come from seed data designed so real alignments score high under the real formula; expected fixture scores are asserted in tests | §11.3, §19.5, §24.5 |
| R3 | Public networking card contact exposure | **One server-chosen contact action:** LinkedIn when present, email as fallback. **Phone is never exposed publicly. Sponsor-consent state is never exposed publicly.** Staff QR resolution is unchanged and still returns name and profession only | §9.3, §9.3.1 |
| R4 | `scan_ref` — keep or simplify | **Kept, with reliability treated as equal in weight to its security value.** The client obligation is to echo one opaque string — no parsing, no timers, no client-side expiry logic. A seven-case test matrix covering generation, expiry, tampering, wrong-staff use, repeat awards, the happy path, and presentation latency must pass before Phase 2 is complete | §13.3, §20.3 |

### 23.3 Still open — needing a product answer

1. **`allow_repeat_task_points`** — demo default `false`. The mechanism supports either; only the default is pending.
2. **Feedback reward amount** — placeholder 5 points in `event_config`.
3. **Preset values** — `+5/+10/+15` globally, or per booth? Schema supports both; demo uses global.
4. **OTP delivery for the demo** — logged, or a real provider? Affects infra setup only.
5. **Leaderboard privacy** — full names, or first name plus last initial? Currently full names.
6. **Zero-point attendees in the "show all" view** — included today; confirm this is wanted.

None of these six blocks Phase 2: each has a working default, and each is a configuration or seed-data change rather than a structural one.

---

## 24. Architecture Invariants

Non-negotiable. A change that violates one of these is an architecture change requiring an update to this document, not an implementation detail.

1. **`points_log` is the sole source of truth for points.** No denormalised total is authoritative. Any projection is a rebuildable cache.
2. **The frontend never authorizes itself.** Every hidden control is independently enforced server-side.
3. **Staff booth identity comes from the authenticated staff account.** `booth_id` is never accepted from a client, and the award schema cannot express it.
4. **QR codes never contain raw internal user IDs or PII.** Only a 256-bit opaque token.
5. **Match percentages are computed, never random and never LLM-generated.** Same input, same score, always. **The score displayed in the UI is exactly the score `MatchingService` returned** — no cosmetic multiplier, curve, floor or display transform may be applied at any layer.
6. **A feedback reward cannot be duplicated for the same `(user, booth)`** — enforced by a unique index, not by application checks.
7. **Point values are validated server-side against a stored allow-list.** Range checks are insufficient.
8. **Demo reset is not publicly accessible** and refuses to run outside demo mode.
9. **Business logic lives in the service/domain layer**, never in React components, route handlers, or repositories.
10. **Realtime events are never the source of truth.** They notify; the database decides; the client reconciles.
11. **Uniqueness invariants are enforced by database constraints**, never by read-then-write.
12. **Realtime events are published only after the transaction commits.**
13. **Passwords, OTP codes and session tokens are stored only as hashes.**
14. **No secret has a source-code fallback default.** Missing configuration fails at startup.
15. **`points_log` rows are never updated or deleted by application code.** The only exception is the `TRUNCATE` in the guarded demo-reset CLI (§19), which is not application code and cannot execute outside demo mode.
16. **Every mutating request schema sets `extra="forbid"`.**
17. **The backend runs exactly one worker** until `EventBus` is backed by a shared transport.
18. **Broadcast realtime events carry no attendee data.** Anything fanned out to all connections is a bare refetch signal; personal data travels only on a session-keyed channel.
19. **Phone numbers and sponsor-consent state never appear in any public response.** The public card exposes one server-chosen contact action — LinkedIn, or email as fallback — and nothing else (§9.3.1).
20. **Staff card resolution returns name and profession only.** Widening the public card never widens what the staff scanner receives.
21. **Real camera scanning is the primary staff flow**, and the presentation environment is served over HTTPS. Manual token entry exists only as a development and emergency fallback and is never the default path.
