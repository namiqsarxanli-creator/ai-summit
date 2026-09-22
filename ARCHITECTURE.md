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
    │   {card_token, points,                  │                               │
    │    idempotency_key, scan_ref}           │                               │
    ├──────────────────────►├───────────────►│ PointsService.award_task()    │
    │                       │                │  A. pure: parse, scan_ref     │
    │                       │                │     HMAC + expiry (no DB)     │
    │                       │                │ ┌─ BEGIN ────────────────────┐│
    │                       │                │ │B0. SHARED RESET FENCE ─────┼┼─► advisory lock
    │                       │                │ │    before any DB read      ││
    │                       │                │ │B1. authn + role (authoritative)
    │                       │                │ │B2. booth_id ← staff session││
    │                       │                │ │B3. IDEMPOTENCY ────────────┼┼─► pk(staff,key)
    │                       │                │ │    (completed? replay&stop)││
    │                       │                │ │B4. resolve token → ACTIVE  ││
    │                       │                │ │B5. points ∈ booth presets? ││
    │                       │                │ │B8. INSERT points_log ──────┼┼─► unique(dedupe_key)
    │                       │                │ │B11. persist snapshot       ││
    │                       │                │ └─ COMMIT (fence releases) ──┘│
    │                       │                │  C. signal POST-COMMIT        │
    │◄── {awarded: 15, new_total: 45} ────────┤                               │
                                             │
Attendee phone ◄── SSE: state_changed (signal only) ──┘
                └─► GET /api/attendees/me/score   ← authoritative values
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
*Constraints:*
```sql
CHECK (points > 0 AND points <= 100)
UNIQUE (booth_id, points) WHERE booth_id IS NOT NULL   -- per-booth presets
UNIQUE (points)           WHERE booth_id IS NULL       -- event-wide defaults
```

> **Corrected (review finding).** A plain `UNIQUE (booth_id, points)` does **not** de-duplicate the global rows: in SQL, `NULL` is not equal to `NULL`, so two rows with `booth_id = NULL, points = 5` both insert. The allow-list would then contain a duplicate `+5`, rendering a duplicated button and making the preset set ambiguous. Two partial unique indexes express the actual intent.
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
| Field | Type | Notes |
|---|---|---|
| `id` | bigint PK IDENTITY | **Identity only. Not a commit-order cursor — see the warning below** |
| `user_id` | FK→users NOT NULL | who earned |
| `booth_id` | FK→booths **NULL** | null only for future non-booth sources |
| `source` | enum `TASK` \| `FEEDBACK` NOT NULL | extensible |
| `entry_type` | enum `AWARD` \| `REVERSAL` NOT NULL DEFAULT `AWARD` | see reversal semantics below |
| `points` | int NOT NULL | positive for `AWARD`, negative for `REVERSAL` |
| `task_id` | FK NULL | composite FK with `booth_id` — see below |
| `reverses_points_log_id` | FK→points_log **NULL, UNIQUE** | set iff `entry_type = REVERSAL` |
| `entered_by_staff_id` | FK→staff_accounts **NULL** | NULL ⇔ system-generated |
| `entered_by_system` | boolean NOT NULL DEFAULT false | |
| `dedupe_key` | text **NULL, UNIQUE** | TASK repeat policy only (§14.5) |
| `note` | text NULL | |
| `created_at` | timestamptz NOT NULL DEFAULT now() | wall clock, **not** an ordering guarantee |

*Constraints and indexes:*
```sql
CHECK (points <> 0)
CHECK ((entry_type = 'AWARD'    AND points > 0 AND reverses_points_log_id IS NULL)
    OR (entry_type = 'REVERSAL' AND points < 0 AND reverses_points_log_id IS NOT NULL))
CHECK ((source = 'TASK'     AND booth_id IS NOT NULL
                            AND entered_by_staff_id IS NOT NULL
                            AND entered_by_system = false)
    OR (source = 'FEEDBACK' AND booth_id IS NOT NULL
                            AND entered_by_staff_id IS NULL
                            AND entered_by_system = true))

-- F: feedback reward uniqueness is STRUCTURAL, not string-based
UNIQUE (user_id, booth_id) WHERE source = 'FEEDBACK' AND entry_type = 'AWARD'

UNIQUE (dedupe_key)                   -- TASK repeat policy; NULLs do not collide
UNIQUE (reverses_points_log_id)       -- a row can be reversed at most once
FOREIGN KEY (task_id, booth_id) REFERENCES tasks (id, booth_id)   -- see below
INDEX  (user_id)                      -- leaderboard aggregation
INDEX  (user_id, created_at DESC)     -- attendee point history
INDEX  (booth_id, created_at DESC)
```

> ### ⚠ `points_log.id` is an identity, not an event cursor
>
> **PostgreSQL identity/sequence allocation order is not commit order.** Transaction A may take id 481, transaction B take 482, and B commit *first*. A reader that has seen 482 and then treats "everything ≤ 482 is already known" will **permanently discard 481**.
>
> Therefore `points_log.id` may be used for:
> - **identity** — "is this the same award I already processed?" (set membership), and
> - **referencing** a specific row.
>
> It must **never** be used as:
> - a high-water mark or cursor (`WHERE id > last_seen`),
> - a total ordering of events,
> - an SSE resume token.
>
> This corrects an earlier design in §17 and is now invariant §24.22.

**Task/booth consistency.** `tasks` carries `UNIQUE (id, booth_id)` purely so `points_log` can declare the composite foreign key above. Without it, a row could reference a task belonging to a *different* booth, and nothing in the schema would object. With it, that state is unrepresentable.

**Append-only, and what a reversal is.** No `UPDATE` and no `DELETE` in application code. A correction is a new `REVERSAL` row with negative points referencing the row it reverses. `reverses_points_log_id` is unique, so a row cannot be reversed twice, and the `entry_type` predicate keeps reversal rows out of the feedback-uniqueness index — so reversing a feedback award correctly makes the attendee eligible again rather than being blocked by the index. The demo exposes no correction UI; the schema needs no change to add one.

`entered_by_staff_id` is populated from the **authenticated session**, never from the request body.

### 7.12.1 `idempotency_records` (added entity — F02)

Idempotency is **not** a column on `points_log`. It needs to be resolvable *before* any mutable validation runs and must survive a request that fails after commit, so it is its own record.

| Field | Type | Notes |
|---|---|---|
| `staff_account_id` | FK→staff_accounts NOT NULL | the key is scoped to its issuer |
| `idempotency_key` | uuid NOT NULL | client-supplied |
| `request_fingerprint` | text NOT NULL | SHA-256 over the canonical immutable request (§13.4) |
| `points_log_id` | FK→points_log NULL | the award this record completed |
| `response_snapshot` | jsonb NULL | the exact response body originally returned |
| `created_at`, `completed_at` | timestamptz | |

*Constraints:* **`PRIMARY KEY (staff_account_id, idempotency_key)`**.

Scoping the key to the staff account is what makes it safe: one staff member's key can never collide with another's, and a key cannot be replayed across accounts.

> **There is no `status` column, and no `IN_PROGRESS` state.**
>
> The record is claimed and filled inside **one transaction** together with the `points_log` insert, so no other transaction can ever observe a half-finished record: it sees either nothing (the claiming transaction has not committed, or rolled back) or a complete record with its snapshot. A status flag would describe a state that is unobservable by definition, and an earlier draft used it to justify a crash-recovery branch that cannot occur — see §13.4.
>
> `points_log_id` and `response_snapshot` are nullable **only** because they are written a few statements after the claim, within that same transaction. The invariant *"a visible record is always complete"* is guaranteed by transaction atomicity rather than by a `CHECK`, since a `CHECK` would have to hold mid-transaction and PostgreSQL does not support deferrable `CHECK` constraints. A `DEFERRABLE INITIALLY DEFERRED` constraint trigger could assert it at commit time; it is not required for the demo and is noted only so the omission is deliberate rather than overlooked.

### 7.13 `event_config` (added entity)

Single row enforced by `CHECK (id = 1)`.

| Field | Type | Bound |
|---|---|---|
| `feedback_points` | int NOT NULL DEFAULT 5 | `CHECK (BETWEEN 1 AND 100)` |
| `allow_repeat_task_points_default` | boolean NOT NULL DEFAULT false | |
| `leaderboard_top_n` | int NOT NULL DEFAULT 10 | `CHECK (BETWEEN 1 AND 100)` |
| `dataset_generation` | bigint NOT NULL DEFAULT 1 | monotonically incremented by demo reset (§19) |
| `maintenance_mode` | boolean NOT NULL DEFAULT false | mutations rejected while true (§19) |
| `updated_at`, `updated_by_staff_id` | | |

**Bounds are enforced in the database, not only in the admin form.** These values feed point awards and page sizes; an unbounded `feedback_points` set to a large value by a mistyped admin input would corrupt the leaderboard in a way the append-only log cannot cleanly undo. A `CHECK` makes the bad state unreachable regardless of which code path writes it.

*Justification for the table:* the feedback reward and the repeat-task rule are product decisions that may need to change **between demo runs, at the venue, without a redeploy**. Code would mean a rebuild during an event; environment variables would mean a container restart and no audit of who changed what.

### 7.14 Concurrency summary

Every rule that must hold under concurrent requests is backed by a **unique index**, not by a read-then-write check:

| Rule | Mechanism | Constraint name |
|---|---|---|
| One live contact card per attendee | partial unique `(user_id) WHERE revoked_at IS NULL` | `uq_contact_card_live_user` |
| No duplicate feedback reward | partial unique `(user_id, booth_id) WHERE source='FEEDBACK' AND entry_type='AWARD'` | `uq_points_feedback_user_booth` |
| No repeat task reward (when disallowed) | unique `dedupe_key` | `uq_points_dedupe_key` |
| No duplicate award per idempotency key | PK `(staff_account_id, idempotency_key)` | `pk_idempotency_records` |
| A row is reversed at most once | unique `reverses_points_log_id` | `uq_points_reverses` |
| One interest selection per pair | PK `(user_id, interest_id)` | `pk_user_interests` |
| One global preset per value | partial unique `(points) WHERE booth_id IS NULL` | `uq_preset_global_points` |
| Booth staff always have a booth | `CHECK` | `ck_staff_role_booth` |
| Task belongs to the awarding booth | composite FK `(task_id, booth_id)` | `fk_points_task_booth` |

**Constraint violations are dispatched by name, never by order.** The application wraps each authoritative insert in a `SAVEPOINT`, catches `IntegrityError`, reads `err.orig.diag.constraint_name`, and maps **that specific name** to a domain error. A single statement can violate more than one unique index, and which one PostgreSQL reports first is not contractual — branching on "the first error" would produce the wrong user-facing message intermittently. An unmapped constraint name is a bug and raises `INTERNAL_ERROR` rather than being guessed at.

A `SELECT` that checks for an existing row before inserting is **explicitly forbidden as an enforcement mechanism**; it may only be used to produce a nicer message before the authoritative insert is attempted.

---

## 8. Attendee Registration & Authentication

### 8.1 Registration lifecycle — provisional until email is proven (F06)

> **Corrected (review finding).** The earlier design issued a full attendee session immediately on registration. That grants an authenticated identity — and a live public networking card — to whoever typed an email address, including someone typing **another person's** email. Registration now creates a *provisional* record, and authentication begins only after the email is proven.

**States.** `users.status` is an enum with a `CHECK`-guarded lifecycle:

```
PROVISIONAL ──(OTP verified)──► ACTIVE
     │
     └──(expired, 24 h)──► purged by cleanup
```

`users.activated_at timestamptz NULL` is set exactly when the transition happens, and `CHECK (status = 'PROVISIONAL' OR activated_at IS NOT NULL)` keeps the two in agreement.

#### Step 1 — `POST /api/attendees/register`

Accepts first name, last name, email, phone, profession, optional LinkedIn URL, interest slugs, and sponsor consent. In **one transaction** it creates the `PROVISIONAL` user, its interest rows, its consent audit row, and its contact card, then issues an OTP challenge.

Server-side rules:
- Email normalised (trim, lowercase); `citext` uniqueness enforced by the database (never a 500).
- Interests resolved **by slug against active rows**; unknown slugs → `UNKNOWN_INTEREST`, rather than silently dropped, so an attendee is never told they matched on something that does not exist.
- `sponsor_consent` is a required explicit boolean with **no default**. Absence is a validation error, not an implied value. The `users` row and its `consent_audit` row are written **in the same transaction**, so a consent decision can never exist without its audit entry.
- LinkedIn URL validated for scheme and host shape.

**It returns no session.** The response says only that a code has been sent.

#### Step 2 — duplicate email behaviour

Resolved by existing state, and deliberately **non-enumerating** — all three branches return the identical response body:

| Existing record | Behaviour |
|---|---|
| none | Create `PROVISIONAL`, send activation OTP |
| `PROVISIONAL` | **Overwrite** the provisional profile with the new submission and send a fresh OTP. The unverified data has no standing, and the second submitter may well be the genuine owner |
| `ACTIVE` | Create nothing. Send a **login** OTP to the registered address. The submitted profile fields are discarded |

The `ACTIVE` branch is what prevents registration from becoming an account-takeover vector *and* from becoming a membership oracle: an attacker submitting a victim's address learns nothing and causes only a login code to be delivered to the victim's own inbox.

#### Step 3 — activation transaction

On correct OTP (§8.2), in one transaction: consume the challenge, set `status = ACTIVE` and `activated_at`, activate the contact card, and create the session. Either everything commits or nothing does — there is no window where the card is public but the account inactive.

#### Capability gating

| Capability | PROVISIONAL | ACTIVE |
|---|---|---|
| Attendee session exists | ❌ | ✅ |
| Public card `/c/{token}` resolves | ❌ `INVALID_QR` | ✅ |
| Staff can scan and award points | ❌ `INVALID_QR` | ✅ |
| Booth matching / recommendations | ❌ (requires a session) | ✅ |
| Appears on the leaderboard | ❌ | ✅ |

**The card token is generated at registration but is inert until activation.** Keeping generation early means the token is stable across the activation step; gating resolution on `status = ACTIVE` means an unverified registration cannot be scanned for points. Leaderboard exclusion matters specifically because §16's `LEFT JOIN` over all users would otherwise list unverified registrations.

#### Abandoned registrations

`PROVISIONAL` users older than 24 h are purged, together with their interests, contact card and challenges, by `python -m app.cli purge-provisional` (also invoked by demo reset). Because they hold a `UNIQUE` email, leaving them forever would permanently block a genuine owner from registering that address. The purge is idempotent and safe to run repeatedly.

### 8.2 Attendee authentication — email + OTP (no password)

The architecture is OTP-based from the start; only the *delivery channel* is simplified for the demo.

**`otp_challenges`** (entity introduced by this section, justified as the OTP store):

`id`, `email` citext NOT NULL, `user_id` FK NULL, `purpose` enum `ACTIVATION` \| `LOGIN` NOT NULL, `code_hash` text NOT NULL, `created_at`, `expires_at`, `consumed_at` NULL, `attempt_count` int NOT NULL DEFAULT 0, `request_ip` text.

*Index:* partial unique `(email, purpose) WHERE consumed_at IS NULL` — at most one live challenge per email and purpose, so "supersede the previous code" is enforced by the database rather than by a best-effort update.

`purpose` exists so an activation code cannot be replayed as a login code, or vice versa; verification matches on purpose as well as value.

| Concern | Decision |
|---|---|
| Code format | 6 digits, generated with `secrets.randbelow(1_000_000)`, zero-padded |
| Storage | **Argon2id hash only.** A 6-digit code has a 10⁶ space; storing it in plaintext makes a database read an account takeover |
| Expiration | 10 minutes, enforced in SQL (`expires_at > now()`), never by a client clock |
| Single use | The verifying transaction begins with `SELECT … FOR UPDATE` on the challenge row, then `UPDATE … WHERE consumed_at IS NULL` and checks the affected row count. The row lock serialises concurrent verifications of the same challenge, so two simultaneous correct submissions cannot both activate — exactly one wins and the other sees `OTP_INVALID` |
| Attempt limit | 5 failed attempts invalidates the challenge. `attempt_count` is incremented **inside the same locked transaction**, so parallel guesses cannot race past the cap by each reading a stale counter — the failure mode that would otherwise turn a 5-attempt limit into an unbounded one under concurrency |
| Request throttle | Per-email and per-IP rate limit on issuance (§21.7) |
| Enumeration | `POST /api/auth/otp/request` returns **the same response whether or not the email is registered**. Otherwise the endpoint is a membership oracle for the attendee list |
| Superseding | Requesting a new code marks prior unconsumed challenges for that email as consumed |

**Demo simplification, explicitly bounded:** when `DEMO_MODE=true`, the OTP is written to the server log and surfaced in the admin console instead of being emailed. The endpoint contract, hashing, expiry, single-use and attempt-limit logic are **identical** in both modes. `config.py` raises at startup if `DEMO_MODE=true` while `ENVIRONMENT=production`. Adding real delivery later is an email adapter, not a redesign.

### 8.3 Sessions

On **OTP verification** (attendee) or **password login** (staff) — never on mere registration (§8.1): generate `secrets.token_urlsafe(32)`, store **only its SHA-256 hash** in `sessions`, and set it as a cookie:

`HttpOnly; SameSite=Lax; Path=/; Secure (when served over HTTPS); Max-Age = 12h`

`HttpOnly` means XSS cannot exfiltrate the session. Same-origin proxying (§3.2) is what allows `SameSite=Lax`. Logout revokes server-side (`revoked_at`), so the token is dead immediately — the property JWTs cannot offer without extra machinery.

**Session death must propagate to open streams.** Revocation or expiry has no effect on an SSE connection that is already open, so a logged-out tab could otherwise keep receiving personal events indefinitely. Every stream re-validates its session on each heartbeat tick and closes immediately when the session is revoked, expired, or its principal deactivated (§17.9). Authorization lifetime is therefore bounded by one heartbeat interval, not by how long the socket happens to survive.

#### 8.3.1 CSRF policy

Cookie authentication needs an explicit CSRF stance. Two layers, both server-side:

1. **`SameSite=Lax`** stops cross-site `POST` from carrying the cookie at all in current browsers. This is the primary defence and it is free.
2. **Origin assertion.** Every mutating request (`POST`/`PATCH`/`DELETE`) must carry an `Origin` (or, failing that, `Referer`) header matching the configured application origin. Requests with a missing or mismatched origin are rejected with `CSRF_REJECTED` (403) before the handler runs.

Layer 2 exists because `SameSite` behaviour varies across older browsers and embedded webviews — plausible at a conference where attendees open links from messaging apps. Because the frontend is same-origin (§3.2), legitimate requests always carry a matching `Origin`, so the check costs nothing in normal operation. No CSRF token table is introduced: a synchroniser token would add state and a failure mode without meaningfully improving on Lax + Origin for this threat model.

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

All of: malformed token, unknown token, revoked token, token belonging to a **provisional (unverified) attendee** (§8.1), and token belonging to a deleted attendee produce the **identical** response — `404` with code `INVALID_QR`. Distinguishing them would turn the endpoint into an oracle that separates "never existed" from "revoked". The frontend shows one message: *"This QR code isn't valid. Ask the attendee to reopen their card."*

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
> The approved lever for strong headline matches is **seed data design** (§19.9): demo booths whose top-weighted interests genuinely align with the demo attendees' selected interests produce high scores *under the real formula*, because the match is real. `α` remains 0.7 and is not being tuned for presentation purposes.

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
    """THE canonical award request contract. Defined once, here."""
    model_config = ConfigDict(extra="forbid")
    card_token:      str       # opaque attendee reference (§9)
    points:          int       # validated against the booth's preset allow-list
    idempotency_key: UUID      # client-generated once per confirmation screen
    scan_ref:        str       # opaque; proves this staff member scanned this token
    # deliberately absent: booth_id, staff_id, source, task_id, timestamp
```
`extra="forbid"` means a client sending `booth_id` receives a `422`. The value cannot reach the service even by accident.

> **Corrected (review finding).** An earlier draft of this schema omitted `scan_ref` while §13.3 required it — with `extra="forbid"`, a compliant client would have been rejected with `422`. The block above is now **the single authoritative contract**; §13.1, §13.3, §14.3 and §20.3 all reference it rather than restating it.

**Field authority — where every value in an award actually comes from:**

| Value | Source | Client may supply? |
|---|---|---|
| `card_token` | request body | ✅ (validated, then resolved server-side) |
| `points` | request body | ✅ (must be a member of the booth's preset allow-list) |
| `idempotency_key` | request body | ✅ (scoped to the authenticated staff account) |
| `scan_ref` | request body, **but server-issued** at resolve time | ✅ echo only — unforgeable, §13.3 |
| `booth_id` | `staff.booth_id` via the session | ❌ **never** |
| `entered_by_staff_id` | the session | ❌ **never** |
| `source` | constant `TASK` for this endpoint | ❌ **never** |
| `task_id` | omitted in the demo flow (§7.9) | ❌ **never** |
| `created_at` | database `now()` | ❌ **never** |

Anything in the "never" rows that appears in a request body is rejected by `extra="forbid"`. There is no code path that reads them from input.

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
                                      { card_token, points,
                                        idempotency_key, scan_ref }   ← §12.2
8. Result                          → "+15 awarded to {name}" or a typed error
```

The confirmation step is **not** skippable and not a client-side convenience: the award endpoint is a separate call that a human action triggers. There is no endpoint that scans and awards in one step, because that would make a misread QR an immediate irreversible award.

### 13.2 Preset buttons come from the server

The `+5 / +10 / +15` buttons are rendered from `GET /api/staff/points/presets`, which returns the resolved presets for **the authenticated staff member's booth** (§7.10). Hardcoding them in the frontend would create a second source of truth for allowed values and let a stale client offer values the server rejects.

### 13.3 What the award call carries

The award carries the **card token** again, not a user id — consistent with "QR never contains raw internal user IDs" and meaning the award endpoint performs its own authoritative resolution rather than trusting a value the client held between two calls.

#### `scan_ref` — complete specification

`scan_ref` is a stateless, server-issued, signed envelope. It is created by resolve and verified by award.

**Envelope contents** (payload `P`, a JSON object):

| Key | Meaning |
|---|---|
| `v` | format version, currently `1` — lets the format change without ambiguity |
| `sid` | `staff_accounts.id` of the staff member who scanned |
| `ses` | `sessions.id` of the session that scanned |
| `tok` | SHA-256 of the `card_token` (hex) — **never the token itself**, so the envelope is not a second copy of the QR secret and is safe in logs |
| `iat` | issue time, **Unix seconds, assigned by the server** from `now()` — never supplied or influenced by the client |

**Canonical serialization.** `P` is serialised as UTF-8 JSON with keys sorted ascending, no insignificant whitespace, no trailing newline. The result is base64url-encoded **without padding** to produce `B`.

**Signature construction.**

```
B   = base64url_nopad( canonical_json(P) )
sig = base64url_nopad( HMAC_SHA256(key = SCAN_REF_SECRET, msg = B) )
scan_ref = B + "." + sig
```

The HMAC covers **`B`, the exact string that is transmitted**, not a re-serialization of the parsed object. Signing the parsed form would mean verification depends on reproducing byte-identical JSON, and any serializer difference becomes a silent verification failure. `SCAN_REF_SECRET` comes from configuration with no fallback default (§21.5).

**Verification, in order:**

1. Split on the final `.`; malformed shape → `INVALID_QR`.
2. Recompute the HMAC over `B` and compare with `hmac.compare_digest` (constant time). Mismatch → `INVALID_QR`.
3. Parse `P`. `v != 1` → `INVALID_QR`.
4. **Future-timestamp rejection:** `iat > now + 5 s` → `INVALID_QR`. A clock-skew allowance of 5 s is permitted; beyond that the envelope claims to have been issued in the future, which a correctly behaving server cannot produce. Treated as tampering, not as expiry.
5. **Expiry:** `now - iat > 120 s` → `SCAN_EXPIRED`.
6. **Staff binding:** `sid` must equal the authenticated staff account id → `STAFF_NOT_AUTHORIZED`.
7. **Session binding:** `ses` must equal the current session id → `STAFF_NOT_AUTHORIZED`.
8. **Token binding:** `sha256(request.card_token)` must equal `tok` → `INVALID_QR`.

**Binding is to the staff *session*, not merely the staff account — decided deliberately.** Account-only binding would let an envelope issued before logout be redeemed after logout, or an envelope minted in one device's session be used from another. Session binding means revoking a session (logout, expiry, admin deactivation) invalidates every outstanding scan for free, with no revocation list and no stored state. The cost is that a staff member whose session expires between scan and award must scan again — correct behaviour, since their authorization genuinely ended.

Being stateless, `scan_ref` needs no table, no cleanup job, and no shared state across workers.

> **Stated bound — `scan_ref` is not single-use.** Within its 120 s window the same envelope can be presented more than once. This is deliberate: single-use would require a server-side consumption record, reintroducing exactly the shared state the stateless design avoids, and it would break the legitimate retry in §13.4 case 8.
>
> The bound is safe because it grants no privilege the staff member lacks. A second presentation with the **same** idempotency key replays the original response; with a **different** key it is an ordinary new award, which the repeat policy then judges — blocked by `dedupe_key` when repeats are disallowed (the demo default), and permitted when they are allowed, which is what "repeats allowed" means. In no case can a staff member award outside their own booth, exceed the preset allow-list, or act after their session ends.

It binds *this staff member* to *this token* for *this short window*. Without it, a staff client could award against any token it obtained by other means — a photograph of someone's card, a value pasted from a log — without ever having scanned it in their own session. It also bounds how long a scan stays actionable, so a phone left on a confirmation screen during a coffee break cannot award an hour later. Cost: one response field and one signature check.

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
| 8 | **Reuse after expiry** | Same key + same fingerprint, replayed after the 120 s window → **replays the original success**, does *not* return `SCAN_EXPIRED` (§13.4) |
| 9 | **Wrong session** | `scan_ref` valid for the account but issued to a different session → `STAFF_NOT_AUTHORIZED` |
| 10 | **Future timestamp** | Forged `iat` in the future → `INVALID_QR`, no row written |

Cases 4, 9 and 10 are the security-critical ones; cases 2, 6, 7 and 8 are the demo-critical ones. Case 7 is measured rather than assumed, because a mechanism that is secure but visibly slow on stage still fails the presentation.

### 13.4 Idempotency (F02)

> **Corrected twice.** The first design relied on a unique index on `points_log.idempotency_key` and caught the violation — which never delivered the promised retry, because `scan_ref` expiry and preset validation ran first, the key was unbound to its caller, and a retry violated two unique indexes at once.
>
> The second design fixed the ordering but introduced an **impossible state**: it declared the idempotency record, the `points_log` insert and the completion to be one atomic transaction, and then described recovering from a crash that left a committed award beside an `IN_PROGRESS` record. Under one transaction that state cannot exist. The recovery branch it justified — re-finding the award by `(staff, token, points, time window)` — was also unimplementable, because `card_token` is deliberately **not** stored in `points_log`, and `dedupe_key` is `NULL` whenever repeat awards are enabled.
>
> Both the impossible state and the invalid recovery lookup are removed below. The model is now a plain read-then-atomic-write.

#### The rule

**Idempotency is resolved immediately after authentication and authorization, and before every mutable validation.** "Mutable" means anything that can legitimately change between the original request and its retry: `scan_ref` freshness, preset configuration, repeat policy, QR revocation state.

#### Key binding and fingerprint

A key is identified by `(staff_account_id, idempotency_key)` — the primary key of `idempotency_records` (§7.12.1). It carries a **request fingerprint** over the immutable logical operation:

```
fingerprint = sha256( canonical_json({
    "v": 1,
    "tok": sha256(card_token),
    "points": points,
}) )
```

The fingerprint deliberately **excludes** `scan_ref` (time-bound by design), the booth (derived, not supplied), and all configuration. It answers exactly one question: *is this the same logical award?* Storing `sha256(card_token)` rather than the token keeps the attendee reference out of a second table while still making the comparison exact.

#### Protocol

> **Fence ordering (§19.4).** Every step that reads reset-sensitive data — including the idempotency lookup, the session lookup and the attendee resolution — happens **after** the shared reset fence is held. Only pure, dataset-independent work may precede it.

**Stage A — pre-fence: pure work only, no database.**

| Step | Work | Why it is safe before the fence |
|---|---|---|
| A1 | Parse and schema-validate the request body (§12.2) | Syntax only |
| A2 | Extract the session cookie | A string; **not** yet an authenticated identity |
| A3 | `scan_ref` structural + cryptographic checks — split, HMAC verify, version, future-`iat`, expiry (§13.3 checks 1–5) | Pure crypto and clock arithmetic against a configured secret. Touches no table |
| A4 | `scan_ref` token binding — `sha256(request.card_token) == tok` (§13.3 check 8) | Compares two values already in hand |
| A5 | Compute the request fingerprint | A hash of request fields |

Nothing in Stage A resolves an entity, and nothing in it can be invalidated by a reset. A maintenance-mode fast-path check may also run here; it is **advisory only** (§19.4) and never the authoritative gate.

**Stage B — inside one transaction, fence held first.**

```sql
BEGIN;
  -- B0. THE FENCE. First statement. Before ANY reset-sensitive read.
  SELECT pg_advisory_xact_lock_shared(FACEMARK_RESET_FENCE);   -- §19.4

  -- B1. AUTHORITATIVE identity: session row → staff account → active?
  --     scan_ref checks 6-7 (sid/ses binding) are evaluated here, because
  --     they compare against this authenticated identity.
  -- B2. booth_id := staff.booth_id                      (derived, §12.2)

  -- B3. IDEMPOTENCY LOOKUP
  SELECT * FROM idempotency_records
   WHERE staff_account_id = :staff AND idempotency_key = :key;
  --   fingerprint matches  → COMMIT (read-only) and return the stored
  --                          snapshot with replayed = true.  STOP.
  --   fingerprint differs  → 409 IDEMPOTENCY_KEY_REUSE.      STOP.
  --   no row               → continue

  -- B4. Resolve card_token → attendee; require status = ACTIVE
  -- B5. points ∈ presets(booth)
  -- B6. repeat policy → dedupe_key

  -- B7. CLAIM the key (first write of the transaction)
  INSERT INTO idempotency_records
         (staff_account_id, idempotency_key, request_fingerprint, created_at)
  VALUES (:staff, :key, :fingerprint, now());
  --  ↳ unique violation on pk_idempotency_records ⇒ see "Concurrent same-key"

  -- B8.  INSERT INTO points_log (...) RETURNING id;
  -- B9.  SELECT COALESCE(SUM(points),0) FROM points_log WHERE user_id = :user;
  -- B10. Build the complete canonical response from B8 and B9
  -- B11. UPDATE idempotency_records
  --         SET points_log_id, response_snapshot, completed_at
COMMIT;
```

**Stage C — after commit only.** Publish the realtime invalidation signal (best-effort, §17.7), then return the response.

**Why the replay path still commits.** A replay at B3 performs no writes, but its transaction is committed (or rolled back — either is correct) rather than left open, so the shared fence is released promptly and a waiting reset is not delayed by a read-only request.

**The idempotency lookup moved inside the fence** because `idempotency_records` is truncated by reset (§19.5). Reading it before the fence would mean deciding "this is a new request" against a dataset that could be replaced before the write. The F02 guarantees are unaffected: the lookup still precedes every *mutable* validation (B3 before B4–B6), which is what makes a retry after `scan_ref` expiry replay rather than fail.

**No deadlock with reset.** The fence is the **first** statement of the transaction, so a mutation never holds a row lock, table lock or any other resource while waiting for it. Reset waits only on the advisory lock and never on a mutation's row locks. There is no cycle, in either direction.

**Ordering rules that remain contractual:**

- **B4–B6 run only for a genuinely new logical request.** That is what makes "retried after `scan_ref` expired" return the original success rather than an error.
- **The response is computed (B8–B10) before it is persisted (B11) and before commit.** A replay is byte-identical to the original reply.
- A failure anywhere in Stage B rolls the whole transaction back: no award, no claimed key, nothing to reconcile.

#### Concurrent same-key requests — one defined behaviour

**Chosen behaviour: the second request waits for the first, then replays its committed result.**

This falls out of PostgreSQL's own semantics rather than needing a mechanism. When T2 attempts the claim insert at (a) while T1 holds an uncommitted row for the same primary key, **T2 blocks on the index entry**. Then:

| T1 outcome | T2 resumes and sees | T2 does |
|---|---|---|
| **Commits** | Unique violation — and the row is now visible **complete**, because claim and completion committed together | Roll back its own (empty) transaction, re-read the record, verify the fingerprint, **return the snapshot with `replayed: true`** |
| **Rolls back** | No conflicting row | Proceed normally and become the owner |

Exactly one award is created in both cases. T2 never performs a second `points_log` insert, because the claim at (a) is the **first write** of its transaction — when it fails, nothing else has happened and the rollback is free.

Two properties make this reliable:

- **The conflict is identified by constraint name** (`pk_idempotency_records`), never by "whichever unique constraint PostgreSQL reported first" (§7.14). Other unique indexes on the award path — `uq_points_dedupe_key`, `uq_points_feedback_user_booth` — map to different domain errors and are not conflated.
- **A visible record is always complete.** Because claim and completion are the same transaction, T2 can never read a claimed-but-unfilled row, so "wait then replay" has no half-state to handle.

`AWARD_IN_PROGRESS` is **not** part of this contract. It is retained in the error catalogue solely as an operational safety valve: mutating transactions set `lock_timeout` (2 s), and if the wait is exceeded — which implies a stuck or pathologically slow transaction, not normal contention — the request returns `AWARD_IN_PROGRESS` so the client can retry rather than hanging. Normal operation never emits it.

#### Defined behaviour for every reuse scenario

| Scenario | Result |
|---|---|
| Same key + same request | **200**, stored snapshot, `replayed: true`. No new row |
| Same key + different `points` | **409 `IDEMPOTENCY_KEY_REUSE`** — fingerprint mismatch, never a silent second award |
| Same key + different attendee/token | **409 `IDEMPOTENCY_KEY_REUSE`** — same reason |
| Same key + different staff account | Different primary key ⇒ a **different record**; proceeds normally. Safe because keys are scoped per account |
| Same key **after `scan_ref` expiry** | **200, replay.** Phase 1 completes before `scan_ref` is examined in Phase 2 |
| Same key **after QR revocation** | **200, replay.** The award was valid when it committed; revoking a card does not rewrite history |
| Same key **after preset change** | **200, replay.** Configuration at retry time has no bearing on a completed operation |
| Key never seen, but the repeat policy forbids the award | **409 `DUPLICATE_TASK_REWARD`** — a genuinely different attempt, correctly refused |

#### The canonical response — and what it deliberately omits

```jsonc
{
  "points_log_id":  481,          // ledger identifier
  "points_awarded": 15,
  "resulting_total": 45,          // SUM over the ledger, computed in-transaction
  "attendee_name":  "Leyla A.",   // for the staff confirmation toast
  "booth_name":     "AIWorks",
  "awarded_at":     "2026-09-22T17:04:11Z",
  "replayed":       false         // stored as false; set to true when replayed
}
```

Every field is a fact about **this award**, fixed at commit time, so the snapshot stays true forever.

**`rank` is deliberately excluded.** Rank is a function of every other attendee's score and changes whenever anyone else earns points. It is not an immutable property of an award, and storing it would make the snapshot state a historical rank as though it were current — the exact "half-historical, half-live" ambiguity that made Option B unacceptable. The staff award screen does not display rank; the attendee's own screen obtains rank from the authoritative snapshot endpoint (§17.4), which is where live values belong.

#### Why no crash-recovery branch exists

`points_log` and the completed `idempotency_records` row commit together or not at all. Therefore:

- **Crash before commit** → neither exists. The key is unused and a retry is an ordinary first attempt.
- **Crash after commit** → both exist, complete and consistent. A retry replays.

There is no third state, so there is nothing to reconcile, no lazy repair path, and no background job. The invalid `(staff, token, points, window)` lookup is removed outright: `points_log` does not store `card_token` (by design, §9), and `dedupe_key` is `NULL` whenever repeat awards are enabled — so that lookup could never have been implemented reliably. Atomicity makes it unnecessary.

The client still generates the UUIDv4 **when the confirmation screen opens**, not per click, so every retry of that award carries the same key.

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

**The order is contractual, not merely an optimisation.** Identity checks come first, idempotency resolves before anything mutable, and only then does validation run. Reordering steps 3 and 4 would reintroduce F02.

| # | Stage | Step | On failure |
|---|---|---|---|
| A1 | **Pre-fence — pure** | Parse and schema-validate the body (§12.2) | `VALIDATION_ERROR` |
| A2 | Pre-fence — pure | Extract the session cookie (a string, **not** an identity) | — |
| A3 | Pre-fence — pure | `scan_ref` structural + cryptographic checks (§13.3 checks 1–5) | `INVALID_QR` / `SCAN_EXPIRED` |
| A4 | Pre-fence — pure | `scan_ref` token binding, `sha256(card_token) == tok` (§13.3 check 8) | `INVALID_QR` |
| A5 | Pre-fence — pure | Compute the request fingerprint | — |
| A6 | Pre-fence — advisory | Maintenance fast-path (§19.4) — a courtesy rejection, **never** the authoritative gate | `MAINTENANCE` |
| **B0** | **▶ FENCE** | **BEGIN**, then `pg_advisory_xact_lock_shared(FACEMARK_RESET_FENCE)` as the **first statement, before any reset-sensitive read** | `MAINTENANCE` on `lock_timeout` |
| B1 | Fenced — identity | **Authoritative** session → staff account → active check; `scan_ref` sid/ses binding (§13.3 checks 6–7) | `UNAUTHENTICATED` / `STAFF_NOT_AUTHORIZED` |
| B2 | Fenced — identity | `booth_id = staff.booth_id`, derived, never read from input (§12.2) | — |
| B3 | Fenced — **resolve** | **IDEMPOTENCY LOOKUP** — match → return the stored snapshot with `replayed: true` and **stop**; fingerprint differs → | `IDEMPOTENCY_KEY_REUSE` |
| B4 | Fenced — validate | Resolve the token → attendee; unknown, revoked, or `PROVISIONAL` (§8.1) → | `INVALID_QR` |
| B5 | Fenced — validate | `points ∈ presets(booth)` — allow-list membership, not a range check. `12` is rejected even though it lies between 10 and 15 → | `INVALID_POINT_VALUE` |
| B6 | Fenced — validate | Repeat policy → compute `dedupe_key` (§14.5) | — |
| B7 | Fenced — write | **Claim the idempotency key** — first write of the transaction | conflict → wait-then-replay (§13.4) |
| B8 | Fenced — write | Insert `points_log`; violations dispatched by constraint name (§7.14) | mapped domain error |
| B9 | Fenced — write | Compute the resulting total | — |
| B10 | Fenced — write | Build the complete canonical response | — |
| B11 | Fenced — write | **Persist it** as the idempotency snapshot | — |
| B12 | Fenced — write | **COMMIT** — award, claim and snapshot become visible together; the fence releases here | — |
| C1 | After commit | Publish the realtime invalidation signal (§17.7) | best-effort, never fails the award |
| C2 | After commit | Return the response | — |

Four ordering rules are contractual, not incidental:

- **The fence precedes every reset-sensitive database read** — including authentication (B1) and the idempotency lookup (B3). Nothing between B0 and B12 can observe a dataset that is replaced before the write. This is invariant §24.29.
- **Authentication at A2 is not authentication.** Only B1, inside the fence, authorises the mutation. A pre-fence cookie read is a string extraction.
- **B4–B6 run only for a genuinely new logical request**, which is what makes "retried after `scan_ref` expired" replay rather than fail.
- **The response is computed (B9–B10) before it is persisted (B11) and before commit (B12).** A replay is byte-identical to the original reply.

A failure anywhere in Stage B rolls back the whole transaction: no award, no claimed key, nothing to reconcile, and the fence released.

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
| `TASK`, repeats **disallowed** | `"TASK:{user_id}:{booth_id}"` |
| `TASK`, repeats **allowed** | `NULL` |
| `FEEDBACK` | `NULL` — feedback uniqueness is **structural** (§7.12), not string-based |
| `REVERSAL` (any source) | `NULL` — a reversal must never be blocked by the policy of the row it reverses |

Because Postgres unique indexes **do not collide on NULLs**, a NULL key means unconstrained, while a non-NULL key is enforced atomically by the index. Flipping the config changes behaviour for future awards with no migration and no code change.

> **Refinement (review finding).** Feedback de-duplication was previously expressed as a `dedupe_key` string, putting a stated invariant in the same mutable mechanism as a configurable policy — one service-layer bug in key construction could have silently disabled it. It is now a **partial unique index on `(user_id, booth_id) WHERE source='FEEDBACK' AND entry_type='AWARD'`**, which holds regardless of what any service computes. `dedupe_key` is now exclusively the TASK repeat-policy mechanism.

#### Transition semantics — what happens when the policy is flipped

The policy is evaluated **at award time only**, and `dedupe_key` is frozen into the row. The consequences are asymmetric and must be stated:

| Transition | Effect on existing rows | Effect on future awards |
|---|---|---|
| disallow → **allow** | Old rows keep their non-NULL keys | New awards get `NULL` keys and are unconstrained. **An attendee blocked under the old policy can now earn again**, because the new row's key is NULL and collides with nothing |
| allow → **disallow** | Old rows keep `NULL` keys | The *first* award after the flip writes `TASK:{u}:{b}` and succeeds **even if the attendee already earned there** under the permissive policy, since the earlier rows have no key to collide with. Subsequent attempts are then blocked |

Neither transition rewrites history, and neither is retroactive. The allow→disallow case permits exactly one extra award per attendee/booth pair that had already earned — acceptable for a demo, and the honest description of what the mechanism does. A production change that must be strictly retroactive requires a documented backfill that writes keys for existing rows, which would fail loudly on any pair that already has duplicates rather than silently discarding them.

Changing this setting is an admin action and is written to the admin audit log with the previous and new values.

### 14.6 Threat/failure matrix

| Threat | Defence |
|---|---|
| Arbitrary point values | Allow-list membership (§14.3 step 8); `INVALID_POINT_VALUE` |
| Client-supplied `booth_id` / `staff_id` / `source` | Fields do not exist; `extra="forbid"`; all derived server-side (§12.2 field-authority table) |
| Double-click | Idempotency resolution (§13.4) → original result replayed, not an error |
| Duplicate network retry, response lost | Same mechanism; resolves **before** any mutable validation, so a retry after `scan_ref` expiry still replays |
| Two concurrent identical requests | The second blocks on the idempotency primary key, then replays the first's committed snapshot (§13.4). Exactly one award |
| Mutation racing a demo reset | Shared advisory fence held for the whole mutating transaction; reset's exclusive acquire waits it out (§19.4) |
| Key reused for a different operation | Fingerprint mismatch → explicit `IDEMPOTENCY_KEY_REUSE`, never a silent second award |
| Key replayed by a *different* staff account | Key is scoped by `staff_account_id`; one account cannot address another's record |
| Unauthorized staff | Session → role → active checks before any mutation |
| Duplicate reward where prohibited | `dedupe_key` unique index (TASK) / partial unique index (FEEDBACK), enforced by the database under concurrency |
| Awarding to a token never scanned in this session | `scan_ref` staff **and session** binding (§13.3) |
| `scan_ref` reuse after logout | Session binding — a revoked session invalidates every outstanding envelope |
| Forged `scan_ref` timestamp | Future-`iat` rejection with a 5 s skew allowance (§13.3) |
| Awarding to an unverified attendee | Token resolution requires `status = ACTIVE` (§8.1) |
| Staff account compromise | Blast radius bounded to one booth and to preset values; every row is attributed and reversible by a `REVERSAL` entry |

---

## 15. Feedback Reward Flow

`POST /api/attendees/feedback` with booth slug, rating and optional comment, in a **single transaction**:

```
BEGIN
  0. SELECT pg_advisory_xact_lock_shared(FACEMARK_RESET_FENCE)  -- §19.4, FIRST statement
  0b. AUTHORITATIVE auth: session → attendee, require status = ACTIVE
      resolve booth by slug                                     -- both inside the fence
  1. INSERT feedback (user_id, booth_id, rating, comment)      -- always succeeds
  2. SAVEPOINT reward
     attempt INSERT points_log
       source='FEEDBACK', entry_type='AWARD',
       points=event_config.feedback_points,
       entered_by_system=true, dedupe_key=NULL
     ├─ success                                   → awarded_points = N
     └─ violation of uq_points_feedback_user_booth → ROLLBACK TO reward
                                                     awarded_points = 0
  3. recompute total
COMMIT
  4. publish realtime invalidation only if awarded_points > 0
```

**Outcome contract** — one response shape for both cases, so the client has no branch to get wrong:

```jsonc
{ "feedback_id": 91, "awarded_points": 5, "already_rewarded": false, "total_points": 50 }
{ "feedback_id": 92, "awarded_points": 0, "already_rewarded": true,  "total_points": 50 }
```

Both are **`200`**. A repeat submission is not an error: submitting more feedback is legitimate behaviour that simply is not paid twice. `already_rewarded` is the field the UI keys on; `DUPLICATE_FEEDBACK` exists in the error catalogue for completeness but is **not** returned by this endpoint.

**Concurrency.** Two simultaneous submissions (double-tap, two tabs) both attempt the reward insert; the partial unique index guarantees exactly one succeeds. The loser is caught by constraint **name** on a `SAVEPOINT`, so the enclosing transaction survives and the feedback row still commits. There is **no** "check if already rewarded, then insert" path — that pattern has a window between check and insert and would double-award under exactly the conditions a live demo produces. Frontend button disabling is a UX nicety layered on top and is never relied upon.

Feedback requires an **ACTIVE** attendee session (§8.1), so an unverified registration cannot generate rewards.

The reset fence at step 0 is not specific to awards: **every mutating transaction in the system acquires it, before its first reset-sensitive read** — feedback, registration, OTP issuance and activation, login, card rotation, admin configuration changes (full table in §19.4). Note that the attendee lookup and booth resolution at step 0b are inside the fence for the same reason as the award path: they are reads the write depends on, and validating them against a dataset that is then replaced is exactly the race §19.4 exists to close. A single omission would reopen it for that one path, so the fence belongs in the shared unit-of-work helper (`db/session.py`) rather than being written out per endpoint.

---

## 16. Leaderboard

### 16.1 Calculation

```sql
SELECT u.id, u.first_name, u.last_name, u.profession,
       COALESCE(SUM(p.points), 0) AS total_points,
       MAX(p.created_at)          AS last_award_at
FROM users u
LEFT JOIN points_log p ON p.user_id = u.id
WHERE u.status = 'ACTIVE'            -- provisional registrations are not attendees yet (§8.1)
GROUP BY u.id
```

The `LEFT JOIN` is what makes **every activated attendee rankable, including those with zero points** — an `INNER JOIN` would silently drop them, and a demo where a newly activated attendee is missing from the list looks broken.

The `status` filter is required, not cosmetic: without it, an unverified registration made with someone else's email address would appear publicly on the leaderboard under that person's name.

Because `points` may be negative on a `REVERSAL` row, `SUM` naturally reflects corrections with no special handling.

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

**Pagination consistency.** The underlying data changes while a visitor pages through it, so a row can legitimately move between pages and be seen twice or missed. Keyset pagination (rather than `OFFSET`) keeps the anomaly bounded to rows whose totals actually changed, instead of shifting every subsequent page. Every leaderboard response carries `as_of` (server timestamp) and `dataset_generation` (§19); the client displays `as_of` on the "show all" view and **discards a page whose `dataset_generation` differs from the first page's**, restarting the listing. No snapshot isolation or materialised cursor is introduced — for a demo-scale list this is the honest, cheap behaviour, and the alternative would be a cache with its own staleness problem (§16.4).

`current_user` is always populated for an authenticated attendee, even when they are inside the top N, so the client renders one consistent component and the highlight logic has no special case.

### 16.4 No caching in the demo — justified

At summit scale (hundreds of attendees, low thousands of point rows) this aggregation with the `(user_id)` index completes in single-digit milliseconds. A cache would add an invalidation path, a staleness window, and a second thing that can be wrong on stage — while solving a problem we do not have. **No caching is introduced.**

### 16.5 Production path (not built now)

In order of escalation: (1) materialised view refreshed on award; (2) a `user_scores` projection table updated in the same transaction as the log insert, treated explicitly as a **rebuildable cache** with a reconciliation job; (3) a Redis sorted set for very large events. All three keep `points_log` authoritative — none of them may become a second source of truth.

---

## 17. Realtime Architecture

> **Corrected (review findings F03, F04).** The earlier design used `points_log.id` as an SSE event cursor, discarding events with `id <= last_seen` and resuming with `WHERE id > Last-Event-ID`. Sequence allocation is not commit order, so a lower id committing after a higher one would have been **permanently discarded** by both rules. The protocol below replaces state-carrying, ordered events with **invalidation signals plus authoritative refetch**, which is simpler *and* correct.

### 17.1 The scenario to satisfy

Phone A (attendee) has the score screen open. Phone B (staff) scans A and awards +15. Phone A must update total, rank and leaderboard **without a manual refresh**, and may animate `+15 POINTS`.

### 17.2 Three concepts, deliberately separated

Conflating these caused F03. They are now distinct:

| Concept | Carried by | Used for | **Never** used for |
|---|---|---|---|
| **Ledger identity** | `points_log.id` | Naming a row; asking "have I already animated this award?" (set membership) | Ordering, cursors, resume tokens |
| **Notification identity** | SSE `event: …` name + a per-connection sequence number | Delivering a "something changed" hint | Carrying authoritative state |
| **State freshness** | A full authoritative snapshot fetched over HTTP | Everything the UI displays | — |

**Ledger ids are safe as identities and unsafe as cursors.** Set membership (`has this id been animated?`) is order-independent and therefore immune to out-of-order commits. That is why the animation rule below uses a set, not a high-water mark.

### 17.3 Decision: Server-Sent Events as an invalidation channel

| Option | Assessment |
|---|---|
| **SSE — chosen** | One-directional server→client, exactly our shape. Plain HTTP, so it traverses the Next.js rewrite proxy with no protocol upgrade. Native `EventSource` reconnection. FastAPI ships first-party support (`fastapi.sse.EventSourceResponse`), so no third-party dependency |
| WebSockets | Rejected: bidirectional capability we do not need, hand-rolled reconnect and heartbeat, an upgrade that proxies can mishandle, and a second auth path |
| Polling only | Rejected as primary (a 5 s delay is visibly sluggish on stage) but **retained as fallback** |
| Durable event log / broker | **Rejected.** Kafka, Redis Streams and RabbitMQ are all excluded (§2.6). Periodic reconciliation gives us the same user-visible guarantee at a fraction of the complexity — see §17.8 |

SSE is used **only** to say *"refetch"*. It never carries a total, a rank, or a leaderboard.

### 17.4 The snapshot / subscribe / reconcile protocol

```
1. SUBSCRIBE FIRST, THEN SNAPSHOT
     open EventSource  →  wait for the `ready` event
     then GET /api/attendees/me/score
   Ordering matters: subscribing first means any award landing during the
   fetch produces a signal that arrives *after* the snapshot, triggering a
   refetch. Fetching first would leave a gap in which a change is missed.

2. ON ANY SIGNAL          →  refetch the authoritative snapshot
3. ON RECONNECT           →  refetch (no event replay, no resume token)
4. ON VISIBILITY REGAINED →  refetch
5. EVERY 20 s, EVEN WHILE CONNECTED →  refetch   (reconciliation floor)
   "Refetch" in steps 2-5 means BOTH the personal snapshot AND the
   leaderboard whenever the leaderboard view is mounted — a rank can
   change with no personal event, because someone else scored.
6. IF NO FRAME FOR 45 s   →  treat the stream as dead, close, reconnect, refetch
```

**Step 5 is the correctness backstop.** It is what makes every F04 failure mode self-healing without a durable event system: a lost publication, a buffering proxy, a half-open socket, or a backend restart all resolve within 20 seconds regardless of whether any event was ever delivered. Everything else in this protocol is latency optimisation on top of it.

**Authoritative snapshot** (`GET /api/attendees/me/score`):

```jsonc
{
  "total_points": 45,
  "rank": 3,
  "recent_awards": [ { "id": 481, "points": 15, "booth_name": "AIWorks",
                       "source": "TASK", "created_at": "…" } ],
  "as_of": "2026-09-22T17:04:11Z",
  "dataset_generation": 7
}
```

`recent_awards` is the most recent N (default 10) rows for this attendee, ordered by `created_at DESC, id DESC` **for display only** — the client never treats that order as a delivery guarantee.

### 17.5 Events

```
event: ready            data: {"dataset_generation":7,"heartbeat_seconds":15}
event: state_changed    data: {"scope":"personal"}      ← your points changed; refetch
event: leaderboard_changed  data: {}                    ← broadcast; refetch leaderboard
event: heartbeat        data: {"t":"…"}                 ← every 15 s
event: session_invalid  data: {"reason":"revoked"}      ← stream closes; client re-authenticates
event: dataset_reset    data: {"dataset_generation":8}  ← demo reset (§19); client hard-reinitialises
```

No event carries points, totals, ranks, names, or any attendee data. The personal channel is keyed by `user_id` **from the session**, never from a query parameter. The broadcast channel is data-free by construction (invariant §24.18), so fanning it out to every connection discloses nothing.

Events deliberately have **no `id:` field**, so `EventSource` never sends `Last-Event-ID` and no resume semantics exist to get wrong.

### 17.6 Animation without ordering

The client keeps `animatedAwardIds: Set<bigint>`, seeded from the **first** snapshot's `recent_awards` (so awards already reflected on arrival are never animated) and held in memory only.

```
after every refetch:
  for award in snapshot.recent_awards:
      if award.id not in animatedAwardIds:
          if pageHasBeenOpenSince(award.created_at) and documentIsVisible:
              animate("+" + award.points)
          animatedAwardIds.add(award.id)
  replace total/rank with snapshot values   ← always, unconditionally
```

This is correct **regardless of commit order**: an award that commits late still appears in a later snapshot, is still absent from the set, and is still animated exactly once. Set membership has no notion of "too old", which is precisely the property the id-as-cursor design lacked.

Displayed state is always assigned from the snapshot, never accumulated locally — the client never computes `total += 15`.

> **Stated bound.** `recent_awards` is a window of the last N (10). If an award commits so late that more than N others have landed for the same attendee in the meantime, it will never enter a snapshot window and its **animation** is skipped. The **total and rank remain exactly correct**, because they come from `SUM` over the whole ledger, not from the window. This is an acceptable cosmetic bound rather than a correctness defect, and at demo scale — one staff member awarding at a time — it is unreachable. Widening N is the remedy if it ever matters; growing a durable per-client delivery log to close a missed animation is not warranted.

### 17.7 Realtime is never the source of truth

1. Signals are published **after** the database transaction commits (§14.3 step 13). A rolled-back award cannot emit one.
2. Publication is **best-effort**: a failure to publish is logged and never fails the award. The 20 s reconciliation covers it.
3. No event payload carries authoritative state, so a lost, duplicated, reordered or forged event cannot corrupt what the UI shows.
4. Any disagreement is resolved by refetching.

### 17.8 Why this needs no durable messaging

The required guarantee is *"the UI is never stale for long"*, not *"every event is delivered exactly once"*. Since every signal is a hint and every value comes from a refetch, delivery loss degrades **latency** (up to 20 s) rather than **correctness**. A broker would add a service, a failure mode, and operational surface to improve a bound we already find acceptable, in exchange for nothing the user can perceive. Excluded deliberately.

### 17.9 Liveness, stream lifecycle and proxy behaviour

| Concern | Behaviour |
|---|---|
| Heartbeat | Server emits `heartbeat` every **15 s** |
| Dead-stream detection | Client sees no frame of any kind for **45 s** (3 missed) → close, reconnect, refetch |
| Proxy buffering | Response sets `Content-Type: text/event-stream`, `Cache-Control: no-cache, no-transform`, `X-Accel-Buffering: no`, and **compression is disabled for this content type**. A buffering proxy withholds heartbeats too, so it is detected by the 45 s rule rather than appearing as a healthy silent stream |
| Session revoked / expired | Stream re-validates its session **on every heartbeat tick**, emits `session_invalid` and closes. Authorization lifetime is bounded by 15 s, not by socket lifetime (§8.3) |
| Attendee deactivated or purged | Same path |
| Backend restart | All streams drop; clients reconnect and refetch. No server-side state to rebuild |
| Demo reset | `dataset_reset` is emitted, then all streams are closed (§19) |
| Max connection age | Streams are closed by the server after **30 minutes**; the client reconnects transparently. Bounds resource leaks from phones left open on a table all day |

**Path that must be tested before the summit:**

```
Browser (EventSource)
  → TLS terminator / reverse proxy      ← buffering, idle timeout, compression
  → Next.js rewrite proxy               ← must stream, not buffer, the upstream body
  → FastAPI EventSourceResponse
```

**Every hop can buffer or idle-timeout a streaming response, and a buffered SSE stream looks identical to a healthy idle one.** The pre-summit check is explicit: open the attendee screen through the **real presentation URL and proxy chain**, award points from a second device, and confirm the update appears within ~1 s rather than in a delayed burst — and separately confirm that a stream left open for several minutes still delivers heartbeats. This is listed alongside milestone M-CAM (§13.5) and is not satisfied by testing against `localhost`, which bypasses the proxy chain entirely.

### 17.10 Transport and the worker constraint

The demo uses an **in-process async pub/sub broker**: a dict of `user_id → set[asyncio.Queue]` plus one broadcast set. No Redis, no broker service.

> **Invariant:** the backend runs with **exactly one Uvicorn worker** in the demo. With multiple workers, an award handled by worker 2 would not signal a stream held by worker 1.

Note that the consequence of violating this is now **bounded**: with the 20 s reconciliation floor, a mis-signalled client still self-corrects — it is degraded, not wrong. The single-worker rule remains, but it is no longer the difference between working and broken.

Production path: PostgreSQL `LISTEN/NOTIFY`, which needs no new infrastructure since Postgres is already present. `EventBus` is an interface so this swap touches one class.

### 17.11 Fallback

If `EventSource` fails to connect, or after 3 consecutive failed reconnects, the client falls back to polling every 5 s and retries SSE every 60 s.

**Polling refreshes both the personal snapshot and the leaderboard**, since in fallback mode there is no broadcast channel to signal that another attendee's score changed. Polled changes update values and animate new award ids under the same §17.6 rule — the rule is source-independent, so no separate fallback logic exists.

---

## 18. Admin Boundary

Admin capabilities: manage interests, booths and their interest weights, create and deactivate staff accounts, manage tasks and preset point values, edit `event_config`, and inspect the points audit trail.

Boundaries:
- All admin routes are under `/api/admin/*` behind `Depends(current_admin)`, which requires `principal_type = STAFF` **and** `role = ADMIN`. A booth staff session receives `403 STAFF_NOT_AUTHORIZED` on every route in the namespace.
- Admins **cannot award or modify points** (§12.3). The audit view is read-only. Corrections, if ever built, are compensating entries attributed to a real account.
- The first admin is created by the seed/CLI path, never by an HTTP endpoint. There is no bootstrap route that creates an admin if none exists — that is a backdoor by another name.
- Admin routes are excluded from the public OpenAPI schema in production and rate-limited (§21.9).
- **Every admin mutation and its audit row are written in the same transaction.** An audit log that can be missing entries for committed changes is worse than none, because it is trusted. The same rule applies to attendee consent and its `consent_audit` row (§8.1), and to `event_config` changes, which record the previous and new values.
- Editing a booth's interest weights changes future match scores. Nothing is cached (§11.4), so the change takes effect on the next request with no invalidation step.

---

## 19. Demo Reset Strategy

> **Corrected (review finding F05).** The earlier design truncated tables and upserted fixtures. That leaves live SSE streams attached, sessions valid, in-process broker state populated, admin-created rows orphaned, and — because identity sequences restart — a browser holding pre-reset state that now collides with reused ids. Reset is therefore a **coordinated maintenance operation**, not a data operation.

### 19.1 Mechanism

A CLI command run inside the backend container:

```bash
docker compose exec backend python -m app.cli reset-demo --confirm
```

**No HTTP endpoint exists for reset** — not authenticated, not hidden. An endpoint that destroys all event data is a liability whose worst case (triggered mid-summit) vastly outweighs the convenience, and whoever runs the demo already has CLI access.

### 19.2 Guards

Refuses to run unless **both** `DEMO_MODE=true` and `ENVIRONMENT != production`, and requires explicit `--confirm`. Failing any check aborts with a non-zero exit and no writes.

### 19.3 Dataset generation — how a browser tells old state from new

`event_config.dataset_generation` is a monotonically increasing integer, bumped by every reset. **It never restarts**, even though table identities do.

- Every API response carries `X-Dataset-Generation`.
- The client stores the value it first saw and compares it on every response.
- **On mismatch, the client hard-reinitialises**: clears all in-memory state (including `animatedAwardIds`), drops its session, closes the stream, and returns to the entry screen.

This is what makes reused numeric ids safe. After a reset, `points_log.id = 481` refers to a different award than it did before, and `users.id = 3` to a different person — but any client still holding pre-reset state sees a generation mismatch on its very next request and discards everything. Without this counter, a phone left open across a reset could silently display stale values against reused ids, which is precisely the confusion a rehearsal→presentation reset would produce.

### 19.4 The reset fence

> **Corrected twice.** First: maintenance mode is an application check that runs when a request *enters*, and says nothing about a mutation that passed it microseconds before the flag was set — such a request could resume after the restore and commit into the new dataset. A database-level fence was added.
>
> Second, and the reason for this revision: the fence existed but was **acquired too late**. The award flow resolved the session, the attendee, the booth, the presets and the repeat policy *before* opening its transaction, leaving this race:
>
> ```
> 1. Award passes the maintenance check          (generation N)
> 2. Award validates attendee / preset / policy  (generation N)   ← no fence held
> 3. Reset acquires the exclusive fence
> 4. Reset restores the dataset, generation → N+1
> 5. Reset releases the fence
> 6. Award acquires the shared fence
> 7. Award writes state validated against N into N+1
> ```
>
> Holding the fence during the write is not enough. It must be held during the **reads the write depends on**.

#### The architecture-wide rule

> **Every transaction that mutates reset-sensitive application data must acquire the shared reset fence before its first reset-sensitive database read, and hold it continuously through commit or rollback.**

"Reset-sensitive" means any table demo reset truncates, reconciles or reseeds (§19.5) — in practice all application data except immutable configuration loaded from the environment. Authentication reads `sessions`, so **authoritative authentication for a mutation is itself reset-sensitive work and belongs inside the fence.**

What may precede the fence is only work whose result cannot be invalidated by a reset: request parsing, schema validation, cookie *extraction*, cryptographic verification against a configured secret, and hashing. The maintenance-mode fast-path check may also run there, but it is **advisory only** — a courtesy that returns `503` quickly. It is never the authoritative gate, because between reading the flag and acquiring the fence anything may change. The fence is the gate.

#### Mechanism

One PostgreSQL advisory lock. No new infrastructure — advisory locks are part of the database we already run.

```
FACEMARK_RESET_FENCE  =  a single well-known bigint constant,
                         defined once in app.core.locks
```

| Party | Lock call | Scope | Acquired |
|---|---|---|---|
| **Any mutating transaction** | `pg_advisory_xact_lock_shared(FENCE)` | **transaction-scoped** | First statement of the transaction, before any reset-sensitive read |
| **Demo reset** | `pg_advisory_lock(FENCE)` (exclusive) | **session-scoped**, on a dedicated connection | After enabling maintenance mode, before touching anything |

**Why these scopes.** Mutations use the **transaction-scoped** variant so the lock is released automatically at commit *or* rollback — a crashed request, a timeout or an unhandled exception cannot leak the fence and stall every future reset. Reset uses the **session-scoped** variant because it must hold the fence across steps that are not a single transaction.

Shared locks are mutually compatible, so ordinary mutations never contend with each other and the fence costs nothing in normal operation. Shared and exclusive are incompatible, which is the entire mechanism.

#### Why a mutation can no longer cross the reset boundary

- *Mutation already running when reset starts.* It holds the shared fence. Reset's exclusive acquire **blocks until it commits or rolls back**. The drain is provided by the lock — no polling, no arbitrary sleep.
- *Mutation that passed the maintenance check but has not yet fenced.* Reset may win the fence. The mutation then blocks at B0 **before performing any resolution**, so it cannot validate against the old dataset. When it proceeds, it does all of its authoritative work — authentication included — against the **new** dataset, or its `lock_timeout` (2 s) expires and it returns `MAINTENANCE`. In neither case does stale validation reach a write.
- *Mutation arriving while reset holds the fence.* Rejected immediately by maintenance mode; and even if it slipped past, it blocks at B0 as above.

Because every reset-sensitive read and the write that depends on it are inside one fence-holding transaction, **there is no interval in which validated state can go stale before being used.**

**No deadlock.** The fence is the first statement of a mutating transaction, so a mutation never holds a row or table lock while waiting for it. Reset waits only on the advisory lock, never on a mutation's row locks. No cycle exists in either direction. Authentication cannot deadlock against reset for the same reason: it acquires nothing before the fence.

Reset bounds its own wait with `lock_timeout = 30 s` on the exclusive acquire. Exceeding it means a mutation is genuinely stuck; the command aborts **before any destructive step**, reports which backend holds the fence, and leaves the dataset untouched.

#### Coverage — every mutating path

The fence belongs in the shared unit-of-work helper (`db/session.py`), acquired by the transaction context manager used by all mutating services, not written out per endpoint. A single omission would silently reopen this race for that one path.

| Mutating path | Fence required | First reset-sensitive read it guards |
|---|---|---|
| Task point award | ✅ | session → staff, idempotency record, attendee, presets |
| Feedback submission + reward | ✅ | session → attendee, booth, prior reward |
| Registration / provisional user creation | ✅ | email uniqueness lookup, interest slug resolution |
| OTP issuance | ✅ | user lookup, prior challenge supersession |
| OTP verification / activation | ✅ | challenge row (`FOR UPDATE`), user activation, session creation |
| Staff / admin login (creates a session) | ✅ | staff account lookup, failed-count update |
| Logout / session revocation | ✅ | session row |
| Contact-card rotation / revocation | ✅ | card row |
| Booth / interest / preset / `event_config` admin mutations | ✅ | the rows being changed, plus the audit row |
| Demo reset itself | exclusive side | — |
| **Pure read-only requests** (leaderboard, score snapshot, public card, SSE stream) | ❌ | Not required. They mutate nothing, and a reader sees either the pre- or post-restore dataset — never a partial one, because the restore is one transaction. Forcing readers to queue behind a reset would make the whole site block during it, for no correctness gain |

#### Reset lock lifecycle — dedicated connection, guaranteed cleanup

A session-scoped advisory lock is owned by a **database session**. If that session were a pooled application connection, the lock could be returned to the pool still held, and a later unrelated request would inherit it. The reset therefore owns its connection outright.

```
 1. CLI opens a DEDICATED, NON-POOLED connection            (its own engine /
                                                             NullPool — never the
                                                             application pool)
 2. Set and COMMIT maintenance_mode = true                  (fail-safe, §19.7)
 3. On that same dedicated connection:
        SELECT pg_advisory_lock(FACEMARK_RESET_FENCE)       [lock_timeout 30s]
    ── waits for every in-flight mutation to finish ──
 4. try:
        run the entire reset critical section (§19.5) on that connection
 5. finally:
        if the connection is still usable:
            SELECT pg_advisory_unlock(FACEMARK_RESET_FENCE)
        close / dispose the dedicated connection             ← final backstop
```

The `finally` block is mandatory, and **connection closure is the backstop**: PostgreSQL releases session-scoped advisory locks automatically when the owning session ends, so even an unlock that cannot run — a broken connection, a killed process — cannot leave the fence permanently held. The connection must **never** be returned to the application pool while it may still own the lock.

Maintenance mode is deliberately *not* cleared in `finally`. It is persistent state and is cleared only on the success path (§19.5 step 7), so a failed reset leaves the system closed rather than open.

### 19.5 Sequence

```
 1. Enter maintenance      event_config.maintenance_mode = true, committed FIRST,
                           so it holds even if a later step fails
 2. Reject new mutations   middleware returns 503 MAINTENANCE for every mutating
                           route; reads continue to serve
 3. ACQUIRE THE FENCE      pg_advisory_lock(FACEMARK_RESET_FENCE)   [exclusive,
                           session-scoped, dedicated connection, lock_timeout 30s]
                           ── blocks until every in-flight mutation has finished ──
                           From here on, no mutation can perform a reset-sensitive
                           read, let alone a write.
 4. ── A/B. SINGLE ATOMIC RESTORE TRANSACTION ───────────────────────────────
      a. TRUNCATE ... RESTART IDENTITY CASCADE:
           points_log, idempotency_records, feedback, sessions,
           otp_challenges, consent_audit, user_interests,
           contact_cards, users
         ↳ truncating `sessions` IS the authoritative invalidation of every
           attendee, staff and admin session (§19.6)
      b. RECONCILE reference data to the fixture set — not merely upsert:
           DELETE booths / interests / booth_interests / tasks /
                  task_preset_points / staff_accounts
           whose natural key is absent from the fixtures,
           then UPSERT every fixture row
      c. Restore event_config values from fixtures
      d. Insert demo attendees (status = ACTIVE) with FIXED public_tokens
      e. Optionally seed baseline points so the leaderboard is not empty
      f. dataset_generation = dataset_generation + 1
    ── COMMIT ────────────────────────────────────────────────────────────────
 5. ── C. Post-restore in-process cleanup ─────────────────────────────────────
      a. Emit `dataset_reset` to every open SSE stream, then close them all
      b. Drop every queue and subscriber set in the in-process EventBus, so no
         queue outlives the dataset it refers to
 6. Verify                 re-read dataset_generation and fixture counts;
                           abort to the failure path if they disagree
 7. ── D. Exit maintenance  maintenance_mode = false   (ONLY on success)
 8. ── E. RELEASE THE FENCE pg_advisory_unlock(FACEMARK_RESET_FENCE), then
                           close the dedicated connection   (in `finally`, §19.4)
 9. Report                 counts, staff logins, demo card URLs, new generation
```

**Why this order is safe — the two intervals that must not exist:**

| Must never happen | Prevented by |
|---|---|
| Maintenance OFF while the dataset is still changing | Maintenance is cleared at step 7, strictly after the restore commits (4) **and** after in-process cleanup (5) and verification (6). Nothing mutates the dataset after step 4 |
| The fence released while destructive work is unfinished | The fence is released at step 8, last. Every destructive action — restore, stream termination, broker clearing — is complete before it |

The order maintenance-off (7) **then** fence-release (8) is deliberate. The reverse would open an instant in which the fence is free while the application still believes it is in maintenance: harmless in effect, but it would mean the two gates disagree, and a mutation that had already passed the advisory maintenance check could fence and proceed while the flag still read `true`. Clearing the flag first makes the transition monotonic — the system is fully ready before anything is allowed in.

**Stream termination moved after the restore (step 5), not before it.** Terminating streams first is unnecessary: streams are read-only and cannot mutate, and the fence already excludes mutations from step 3 onward. A client reading during the restore sees either the pre- or post-restore dataset and never a partial one, because 4a–4f are one transaction. Terminating *after* the commit is strictly better, because it also catches any stream that connected during the restore window — a pre-restore termination could not.

Sequences for truncated tables are restarted by 4a; reference tables keep theirs, which is harmless because `dataset_generation` disambiguates ids across resets (§19.3).

**Step 7b is a correction from an earlier draft.** Upserting alone leaves behind anything an admin created during a rehearsal — an extra booth, a spare preset, a staff account — so the "known presentation state" would silently differ from the fixture set in exactly the way rehearsals cause. Reconciliation makes the outcome a function of the fixture file alone.

### 19.6 Session rule — all sessions are invalidated, without exception

> **Corrected (review finding F05).** An earlier draft claimed a non-fixture operator session was "left alone so the operator is not locked out", while the same sequence truncated the entire `sessions` table. Both could not be true.

**The rule: reset revokes every session — attendee, booth staff and admin alike.** There is no preserved-session exception anywhere in this document.

The invalidation is the `TRUNCATE` of `sessions` inside the atomic restore transaction (§19.5 step 4a). It is deliberately **not** a separate pre-restore revoke step: a single mechanism inside the transaction means session invalidation either happens with the restore or not at all, and there is no window in which sessions have been revoked but the dataset was then rolled back.

This is safe because **the reset operator does not need a web session**. Reset is a CLI command executed inside the backend container (§19.1); it authenticates through container access, not through the application. Preserving a web session would have bought nothing and cost the guarantee that no credential survives a dataset change.

After reset, every browser client must reinitialise and authenticate again: attendees via OTP, staff and admins via password. Combined with the `dataset_generation` check (§19.3), a client that was open across the reset is forced to discard its state *and* re-authenticate before it can act, so no pre-reset identity or cached value can be reused against the new dataset.

#### A pre-reset award retried after the reset

This is the one case where reset and idempotency (§13.4) interact, so the outcome is stated explicitly rather than inferred:

1. The retry carries a session cookie whose `sessions` row no longer exists. Authoritative authentication at **B1 — inside the fence** — fails with `UNAUTHENTICATED`. The request stops there, before the idempotency lookup at B3.
2. The client sees `401` plus a changed `X-Dataset-Generation`, discards its state, and re-authenticates.
3. Had it somehow authenticated, B3 would find no idempotency record — that table is truncated too — and would treat the request as new. **That is correct, not a double-award:** the original `points_log` row was truncated by the same transaction. There is no award to duplicate.

The F02 guarantees are not weakened. They concern a stable dataset, and across a reset boundary the award, its idempotency record and the session that authorised it are removed **together, in one transaction**. Any other combination — for example truncating `points_log` while retaining `idempotency_records` — would let a replay assert an award that no longer exists, which is precisely why both tables are in the same truncate list (§19.5 step 4a).

### 19.7 Failure behaviour

Step 4 is one transaction: a failure anywhere inside it rolls back completely, so the database is never left half-reset.

**Maintenance mode deliberately stays ON after any failure.** The fence is released by the `finally` block (§19.4) so the operator can investigate without a stuck lock, but the maintenance flag is persistent state and is cleared only on the success path. The command exits non-zero and reports that the system is held in maintenance. This is fail-safe rather than fail-open: a partially-reset or unverified system must not resume accepting mutations on its own.

| Failure point | State afterwards |
|---|---|
| Fence acquire times out (step 3) | Nothing touched; dataset unchanged; maintenance **ON**; fence released; the report names the backend that holds it |
| Restore transaction (step 4) | **Full rollback** — dataset, sessions and generation all unchanged; maintenance **ON**; fence released |
| In-process cleanup (step 5) | Dataset is already correctly restored. Stream/broker clearing is idempotent and destructive-only; maintenance **ON**; re-running is safe. Clients are forced to reinitialise by the generation change regardless (§19.3) |
| Verification (step 6) | Dataset restored but unverified; maintenance **ON** — deliberately not cleared, so nothing enters a state the operator has not confirmed |
| Unexpected exception anywhere | `finally` unlocks and closes the dedicated connection; maintenance **ON** |
| Process crash / SIGKILL | No `finally` runs, but PostgreSQL releases the session-scoped lock when the connection dies; `maintenance_mode = true` persists in the database |

Recovery is an explicit operator action:

```bash
python -m app.cli reset-demo --confirm          # retry (idempotent)
python -m app.cli maintenance --off             # deliberate manual release
```

### 19.8 Determinism and the printed-QR requirement

The seed uses **fixed** `public_token` values for demo attendees. This is not cosmetic: pre-printed QR cards and slide screenshots must keep working after every reset. Random tokens would invalidate printed materials at exactly the moment it matters. Stable tokens remain stable across generations by design — the generation counter disambiguates *state*, while tokens deliberately survive.

Demo attendees are seeded as `status = ACTIVE` with `activated_at` set, so they are immediately scannable without an OTP round trip on stage. This is a fixture property, not a code path: ordinary registration still requires verification (§8.1).

**Security note:** fixed demo tokens are acceptable **only** because they are demo identities in a demo-mode deployment, and §19.2 prevents that fixture set ever loading into production. Real attendees always receive `secrets`-generated tokens. Staff passwords come from environment variables with no fallback default — the CLI fails loudly if they are unset rather than seeding a known password.

### 19.9 Seed data must produce genuinely strong matches

Because no cosmetic transform is permitted (§11.3), the demo's headline match percentages are entirely a function of the seed data. Designing that data is a **required deliverable of the seeding work**:

- At least one demo attendee must have a **genuinely strong** alignment with at least one demo booth — their selected interests are that booth's top-weighted interests — so the computed score naturally lands high.
- The fixture set must also contain **mid and low matches**, so the recommendation list visibly discriminates. A demo where every booth shows 90%+ demonstrates nothing and invites the suspicion that the number is decorative.
- Interest catalogue and booth weights are curated together so that `coverage` is high *and* `focus` is respectable — both terms matter (§11.3).
- Expected scores for the demo fixtures are **computed by hand and asserted in tests**. If a seed change moves a headline number, the test fails and the demo script is updated, rather than the discrepancy being discovered on stage.

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
| `IDEMPOTENCY_KEY_REUSE` | 409 | Domain | Key reused with a different request fingerprint (§13.4) |
| `AWARD_IN_PROGRESS` | 409 | Domain | **Safety valve only** — `lock_timeout` exceeded waiting on a same-key transaction. Normal concurrency waits and replays instead (§13.4) |
| `MAINTENANCE` | 503 | API | Demo reset in progress; mutations rejected (§19) |
| `CSRF_REJECTED` | 403 | API | Missing or mismatched `Origin` on a mutating request (§8.3.1) |
| `REGISTRATION_NOT_VERIFIED` | 403 | Domain | Action requires an `ACTIVE` attendee (§8.1) |
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

> **Corrected (review finding F07).** The earlier limits were per-IP and small — 5 registrations per hour, 10 OTP requests per 15 minutes. **At a summit, hundreds of attendees share one NAT public IP.** The sixth genuine registration of the day would have been refused, and the demo would appear broken in the most public way possible. Per-IP limiting assumes one IP ≈ one user, which is false in exactly our environment.

**Principle: limit the identity being attacked, not the network path.** The per-identity limit does the security work; the per-IP limit is only a blunt ceiling against a single host flooding the service, set high enough that a venue full of legitimate users never reaches it.

| Endpoint | Primary limit (per identity) | Shared-IP ceiling |
|---|---|---|
| `POST /api/auth/otp/request` | **3 / 15 min per normalised email** | 600 / 15 min |
| `POST /api/auth/otp/verify` | **5 attempts per challenge** (§8.2) + 10 / 15 min per email | 1200 / 15 min |
| `POST /api/staff/auth/login` | **10 / 15 min per account** + lockout (§21.8) | 600 / 15 min |
| `POST /api/attendees/register` | **`UNIQUE(email)` + the ACTIVE-email branch (§8.1)** — the real duplicate protection | 600 / hour |
| `GET /api/cards/{token}` | **30 / min per token** | 1200 / min |
| `POST /api/staff/scan/resolve` | 120 / min per staff account | — |
| `POST /api/staff/points/award` | not limited — a busy booth is normal traffic, and idempotency plus `dedupe_key` already prevent the abuse a limit would target | — |

Card reads are now limited **per token** rather than per IP: that is the resource actually worth protecting, and it does not punish a crowd behind one NAT for sharing an uplink.

**21.7.1 Trusted proxy handling.** The client IP used for any ceiling above is resolved as follows:

1. If the immediate peer address is **not** in the configured `TRUSTED_PROXY_CIDRS`, the peer address is the client IP and **all forwarding headers are ignored**.
2. If it is trusted, walk `X-Forwarded-For` from the **right**, discarding entries that are themselves trusted proxies, and take the first untrusted address.
3. `TRUSTED_PROXY_CIDRS` defaults to **empty** — trust nothing until it is configured.

A public client therefore **cannot spoof `X-Forwarded-For` to choose its own rate-limit identity**, which would otherwise make every limit here trivially bypassable while still penalising honest users. Taking the leftmost XFF entry — the common mistake — is exactly the spoofable choice and is prohibited. Because the deployment sits behind the Next.js rewrite proxy and a TLS terminator (§3.2, §13.5), configuring these CIDRs is a required infrastructure step, not optional: leaving it empty makes every request appear to originate from the proxy and collapses all ceilings onto one bucket.

Demo implementation is an in-process fixed-window counter (consistent with the single-worker invariant, §17.10); production moves to a shared store.

**21.8 Brute force.** Staff login: `failed_login_count` increments on failure; at 10 failures `locked_until = now() + 15 min` and further attempts return `ACCOUNT_LOCKED` without evaluating the password. The counter resets on success. OTP: 5 attempts per challenge, 10-minute expiry, single use. Both paths use constant-time comparison and return identical responses regardless of whether the identity exists.

**21.9 Admin routes.** Separate namespace, `current_admin` dependency, excluded from the production OpenAPI schema, rate-limited, and every mutation written to an audit log with the acting account and timestamp.

**21.10 SQL injection.** All queries are built through SQLAlchemy Core/ORM constructs, which parameterise by construction. Raw SQL is permitted only in Alembic migrations and the reset CLI, where it is static with no interpolation of user input. **String-formatted SQL containing request data is prohibited**, and a lint rule enforces it.

**21.11 Sensitive logging.** Never logged: passwords, password hashes, OTP codes, session tokens, **QR/card tokens**, **`scan_ref` values**, and full email addresses at info level. A log filter redacts known-sensitive keys by name at the formatter level, so a stray `logger.info(request_body)` cannot leak them.

QR tokens need specific care because they appear in a **URL path** (`/c/{token}`), which is the part of a request that proxies, access logs and error trackers record by default:

- The application's own access log masks the segment as `/c/***`.
- The reverse proxy's access log must be configured to apply the same masking — a required infrastructure step, since a token in a proxy log is a permanent capability sitting in plain text.
- Card pages send `Referrer-Policy: no-referrer`, so the token is not leaked to a third-party site when an attendee taps the LinkedIn contact action (§9.3.1). This is stricter than the site-wide `strict-origin-when-cross-origin` precisely because the path itself is the secret.
- `scan_ref` is never logged even though it embeds only a token *hash*, because logging it would make the signature reusable for its 120 s lifetime. In demo mode the OTP is logged deliberately — behind the `DEMO_MODE` guard, and stated in the README so it can never be mistaken for an accident.

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
| R2 | Match scores and the "94%" example | **Formula unchanged; displayed score is always the calculated score.** No cosmetic multiplier or transform of any kind. The 94% was illustrative. Strong headline matches come from seed data designed so real alignments score high under the real formula; expected fixture scores are asserted in tests | §11.3, §19.9, §24.5 |
| R3 | Public networking card contact exposure | **One server-chosen contact action:** LinkedIn when present, email as fallback. **Phone is never exposed publicly. Sponsor-consent state is never exposed publicly.** Staff QR resolution is unchanged and still returns name and profession only | §9.3, §9.3.1 |
| R4 | `scan_ref` — keep or simplify | **Kept, with reliability treated as equal in weight to its security value.** The client obligation is to echo one opaque string — no parsing, no timers, no client-side expiry logic. A seven-case test matrix covering generation, expiry, tampering, wrong-staff use, repeat awards, the happy path, and presentation latency must pass before Phase 2 is complete | §13.3, §20.3 |

### 23.2.1 RESOLVED — independent architecture review (Phase 1.1)

Seven HIGH findings, each independently verified against the document before correction.

| ID | Finding | Correction | Sections |
|---|---|---|---|
| **F01** | Award contract contradictory — schema omitted `scan_ref` while `extra="forbid"` would reject it | One canonical schema plus a field-authority table; full `scan_ref` envelope, canonical serialization, HMAC construction, expiry, future-timestamp rejection and **session** binding specified | §12.2, §13.3, §3.3, §13.1 |
| **F02** | Idempotency did not deliver its promise — mutable validation ran first, key unbound to caller, ambiguous constraint reporting. **Re-opened in verification:** the corrected text described an impossible crash state (committed award beside an `IN_PROGRESS` record under one transaction), persisted the snapshot before the total was computed, and proposed an unimplementable recovery lookup | Read-then-atomic-write model. `status`/`IN_PROGRESS` removed — a visible record is always complete. Claim, award, total, snapshot and completion in **one transaction**; response computed before it is persisted. Concurrency: **wait-then-replay**, from PostgreSQL's own primary-key blocking. Crash-recovery branch and `(staff, token, points, window)` lookup deleted | §7.12.1, §13.4, §14.3 |
| **F03** | `points_log.id` used as a commit-ordered cursor — sequence order ≠ commit order, so events could be permanently discarded | Ledger identity, notification identity and state freshness separated. Cursors, resume tokens and high-water marks eliminated; animation uses **set membership**, which is order-independent | §7.12, §17.2, §17.6 |
| **F04** | SSE recovery incomplete — no reconciliation while connected, no dead-stream detection, no buffering mitigation | Snapshot/subscribe/reconcile protocol with a **20 s reconciliation floor**, 15 s heartbeat, 45 s liveness cutoff, subscribe-before-snapshot ordering, anti-buffering headers, bounded stream age, and a required pre-summit proxy test | §17.4, §17.9 |
| **F05** | Reset ignored streams, sessions, broker state and orphaned rows. **Re-opened once:** maintenance mode did not fence a mutation already past the middleware, and one step preserved an operator session another step truncated. **Re-opened again:** the fence existed but was acquired *after* authentication, attendee resolution, preset and policy lookup — so a mutation could validate against generation N and write into N+1 | Fence moved to the **first statement of the mutating transaction, before any reset-sensitive read** — authentication and the idempotency lookup included. Architecture-wide rule plus a per-path coverage table. Reset lock owns a **dedicated non-pooled connection** with `try/finally` unlock and close, connection death as the backstop. Release order fixed: restore → cleanup → verify → maintenance off → fence release | §13.4, §14.3, §19.4–19.7, §24.29 |
| **F06** | Full session issued before email ownership proven | `PROVISIONAL → ACTIVE` lifecycle; no session, public card, scanning, matching or leaderboard presence until verified; duplicate-email branches defined non-enumerably; 24 h purge | §8.1, §9.4, §16.1 |
| **F07** | Per-IP limits hostile to a shared venue NAT | Per-identity primary limits with high shared-IP ceilings; per-token card limit; explicit trusted-proxy resolution that cannot be spoofed | §21.7, §21.7.1 |

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
10. **Realtime events are never the source of truth.** They notify; the database decides; the client reconciles. **No SSE event carries authoritative state** — every displayed value comes from an HTTP snapshot.
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
22. **`points_log.id` is an identity, never a cursor.** It may be used for set membership and row references; it may never be used as a high-water mark, an ordering, or a resume token, because sequence allocation is not commit order.
23. **Idempotency is resolved after authentication and before every mutable validation**, and its key is bound to `(staff_account_id, fingerprint)`. A completed request replays its original response snapshot verbatim. **The award, the key claim and the response snapshot commit in one transaction**, so no partially-completed state is observable and no crash-recovery path exists.
24. **Constraint violations are dispatched by constraint name**, never by which error the database happens to report first.
25. **No attendee capability exists before email ownership is proven.** Registration creates a provisional record; sessions, public cards, scanning, matching and leaderboard presence all require `ACTIVE`.
26. **Rate limits are primarily per identity, never primarily per IP**, because a venue shares one public IP. Forwarding headers are trusted only from configured proxies.
27. **The UI cannot remain stale indefinitely.** Periodic reconciliation runs even while the realtime channel appears healthy.
28. **A client can never confuse pre-reset and post-reset state**, because `dataset_generation` changes on every reset and a mismatch forces hard reinitialisation.
29. **No reset-sensitive mutation may perform authoritative database resolution or business validation before acquiring the shared reset fence.** The fence (`pg_advisory_xact_lock_shared`) is acquired as the first statement of the mutating transaction — before authentication, before entity resolution, before policy lookup — and is held continuously through commit or rollback. Only pure, dataset-independent work (parsing, schema validation, cookie extraction, cryptographic verification, hashing) may precede it. A maintenance-mode check outside the fence is advisory only and is never the authoritative gate.
30. **Demo reset revokes every session, with no exception.** The operator runs the CLI and needs no web session.
