# FACEMARK

Event networking and gamification platform for an AI Summit. Attendees
open the site in their phone's Safari or Chrome (nothing to install), register
with an email OTP, get a personal QR contact card, receive booth
recommendations matched to their interests, earn points at booths, and
follow a live leaderboard. Booth staff scan attendee QR codes and award
points; admins manage the event dataset.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the system design and
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) for the milestone plan
this repository is built against. Both are locked reference documents.

**Current status:** milestones M01–M18 are implemented (backend, frontend,
admin console, demo reset, PWA/UI polish, hardening); M19 is the summit
rehearsal. The UI is available in Azerbaijani, English and Russian.

---

## How points are awarded (activities)

Points are tied to a concrete **activity** (`tasks`) of a booth, not to a number
the staff member types or picks.

* **Rule on the server:** every activity has `points` (1-100), set by an admin.
  The award request carries only `card_token`, `task_id`, `rule_revision`,
  `idempotency_key` and `scan_ref` - there is no point field, and a body that
  carries one is refused, so a client cannot choose, inflate or round an award. (Fixed points per activity was
  chosen over per-activity presets: the activity *is* the rule, there is nothing
  for staff to pick, nothing to tamper with, and the confirm screen can state
  the exact value. The old booth-level presets remain in the database and admin
  console for compatibility but no longer influence awards.)
* **Checks, all server-side:** the staff account is an active booth staff member
  and the activity belongs to THEIR booth (another booth's, or an unknown id, is
  the same `TASK_NOT_FOUND`), is active (`TASK_INACTIVE`) and has a point value
  (`TASK_NOT_CONFIGURED`); the QR and `scan_ref` are valid and unexpired; the
  attendee is active.
* **Repeat policy per (attendee, activity):** one award per attendee and
  activity by default. The most specific setting wins: the activity's
  `allow_repeat_points`, else the booth's `allow_repeat_task_points` (which now
  supplies the default for *its activities*; it no longer means "one award per
  booth"), else the event default (`false`). Admins change it per activity in
  the admin console; every change is audited.
* **What staff confirmed is what is paid (`rule_revision`):** every activity in
  `GET /api/staff/tasks` carries a server-issued `rule_revision`, derived from
  its points and its *resolved* repeat policy (so a change to the activity, the
  booth flag or the event default all move it). The scanner sends back the
  revision it showed. If the rule changed in between, **nothing is awarded**:
  the server answers `409 TASK_RULE_CHANGED` with the current rule, the screen
  shows "Before +7 / Now +9" and asks for a fresh tap, which uses a **new**
  idempotency key. A network retry of the same confirmation keeps its key and
  revision. A revision confirms a rule; it can never set one, and a completed
  award still replays after the rule changed.
* **Older cached staff pages:** a request without `rule_revision`, or with a
  `points` field, creates nothing and is refused before any database access with
  `403 CSRF_REJECTED` (`details.reason = "client_outdated"`). That existing code
  is used on purpose: every earlier staff page already shows it as "Please reload
  the page and try again". Current pages show "No points were awarded" and a
  *Reload the page* button.
* **Double payment is impossible:** a unique index on `(user, activity)` for
  once-only awards (plus the string `dedupe_key`), a `FOR SHARE` lock on the
  activity row while the award commits, and idempotency records. The same
  `idempotency_key` + same attendee + same activity replays the original result
  (even if the activity was deactivated or re-priced since); the same key with
  another activity or attendee is refused (`IDEMPOTENCY_KEY_REUSE`).
* **History:** each new `points_log` row stores `task_id`. Rows from before
  activities existed keep `task_id = NULL` and are shown as "Booth-level award
  (before activities)"; nothing is attached to an invented activity.

## Reversing points (administrators)

An administrator can cancel **one AWARD in full**, with a reason: the *Points ledger*
section shows **Reverse…** on every award that has not been reversed, which opens a
confirmation screen of its own (what is reversed, what will and will not happen, a
required 3-300 character reason). `POST /api/admin/points-log/{id}/reverse` takes only
`{reason, idempotency_key}`.

* **The ledger stays append-only.** The original entry is never changed or deleted. A
  new **REVERSAL** entry is added: the negative of the original's points, the same
  attendee, booth, activity and source, `reverses_points_log_id` pointing at the original,
  the administrator recorded as the one who entered it, the reason in `note`. All of that
  is derived on the server from the original - the request cannot name an amount, an
  attendee or a booth (`extra="forbid"`).
* **Once only, however it is attempted.** The unique `uq_points_reverses` (a second
  reversal of one row violates it), a row lock on the original, and `idempotency_records`
  (the same key replays the first answer, also after a lost response or a double click;
  the same key for another entry or reason is `IDEMPOTENCY_KEY_REUSE`). A second request
  with another key gets `409 POINTS_ALREADY_REVERSED`. **A REVERSAL cannot be reversed**
  (`409 POINTS_NOT_REVERSIBLE`): a mistaken reversal is corrected by awarding again.
* **FEEDBACK reversals.** A FEEDBACK AWARD must be staff-less and "system" (the
  `source_attribution` CHECK), which cannot name the administrator who cancels it.
  Migration `0012_points_reversal` adds exactly one more allowed form - a FEEDBACK
  **REVERSAL** with `entered_by_staff_id` set and `entered_by_system = false` - so the
  administrator is recorded. The two original forms are unchanged: every existing row, and
  every award, means what it meant. TASK reversals already fit the existing form.
* **One transaction.** The reversal row, the claimed key and the `points.reverse` admin
  audit row commit together or not at all. The reason is stored (`note`, database CHECK
  `<= 300` characters), shown in the ledger list, the audit view and the CSV export
  (`reverses_entry_id`, `reversed_by_entry_id`, `reversal_reason` columns, text neutralised
  like every exported text) - and **never logged**.
* **Live update.** After the commit the attendee's score and the leaderboard are refreshed
  through the normal realtime signal (not for a replay, not for a refusal).
* **After a reversal - can the attendee earn it again?** **No, not automatically.** A
  reversal is a compensating entry: the original AWARD keeps occupying the once-per-attendee
  slot (`dedupe_key`, `uq_points_task_user_once`, and the ledger check at award time), so a
  *once-only* activity is **not** reopened, and the feedback reward at that booth is not
  restored (`uq_points_feedback_user_booth`, ARCHITECTURE.md 7.12). An activity that
  *allows repeats* can be earned again, as it always could. Reopening a once-only
  activity is a separate, deliberate decision that no code here makes; the confirmation
  screen says so before the button works.

## Staff and administrator passwords

* **Change it while signed in** (`/staff/password`, `POST /api/staff/auth/password`): the
  current password, the new one twice. Policy = the console's own: 12-256 characters, not
  the same as the current one. A wrong current password counts like a wrong login
  (`failed_login_count`, 15-minute lock) and is rate limited (5 per 15 minutes per account).
  **Success ends every session of the account, this one too**, and clears the cookie: sign
  in again. A `scan_ref` is bound to the session that issued it, so the scans made before die
  with it. The realtime streams are attendees', not staff's, so there is no staff stream to
  end and none accepts a staff cookie.
* **Forgot it** (`/staff/reset`): `POST /api/staff/auth/password-reset/request` emails a
  one-time code **through the existing OTP outbox** (durable, retried, AES-GCM ciphertext
  while waiting, wiped when sent; the worker `python -m app.worker` must be running). The
  code is its own purpose, `STAFF_PASSWORD_RESET`, separate from attendee activation / login
  codes (migration `0013_staff_password_reset`): every query matches on purpose, so a code of
  one kind never works as the other and issuing one never replaces the other. It is verified
  against an Argon2id hash, single use, valid 10 minutes, 5 wrong tries.
  `POST /api/staff/auth/password-reset/confirm` takes the address, code and the new password
  twice; it ends every session and creates none.
* **No oracle.** The request answers the same whatever the address; the confirmation fails
  with the same `OTP_INVALID` for an unknown address, a deactivated account, a missing /
  expired / used / exhausted code and a wrong one; each path does the same Argon2id work, so
  timing does not tell them apart; limits are keyed on the submitted address (known or not).
  A **deactivated account** can neither start nor finish a reset and is never reactivated.
* **An administrator can start it for someone else** (Staff section, **Send password reset
  email**; `POST /api/admin/staff/{id}/password-reset`): the system emails the code to that
  staff member's **own** address. The administrator never sees the code and there is no way
  to set another person's password (the request has no field for one). Without an email
  outbox the administrator gets `503 PASSWORD_RESET_UNAVAILABLE`; the public request answers
  generically and queues nothing. Staff reset codes are never written to the DEMO_MODE log or
  the "Demo codes" panel.
* **Audit:** `staff.password_change`, `staff.password_reset_requested` (who started it:
  the person or an administrator) and `staff.password_reset` - the account, who, and how
  many sessions ended. Never a password, a hash or a code; each is on the audit view's
  allowlist.

## The attendee's own profile (details, sponsor consent, photo)

On the **Profile** tab a signed-in attendee manages their own record - and only their own: no
request carries an id, and the e-mail (the login identity) cannot be changed here.

* **Details and interests** (`PUT /api/attendees/me/profile`): first name, last name, phone,
  profession, LinkedIn, interests - the same validation as registration. An interest must be in
  the active catalogue or already held (`400 UNKNOWN_INTEREST`, `details.slugs` names the
  offenders). Booth recommendations read the interests live, so they change with the next
  request. Saving what is already saved writes nothing.
* **Two devices.** The form carries the `version` it was loaded at (`expected_version`). If the
  profile changed since, nothing is saved and the answer is `409 PROFILE_CONFLICT` with the
  current profile; the screen shows both versions and keeps what was typed (*Save my changes
  anyway* / *Use the saved version*). An error of any kind never clears the form.
* **Sponsor consent** (`PUT /api/attendees/me/sponsor-consent`, `{granted: bool}`) is its own
  button, not part of the form. The value, its time and wording version, and the
  `consent_audit` row (`source = settings_update`) are one transaction; repeating the value in
  force writes no audit row. The sponsor CSV export follows it at once.
* **Photo** (`PUT /api/attendees/me/photo` with the raw image, `DELETE` to remove). JPEG, PNG
  or WebP **by content** (SVG, GIF, HEIC, animated or damaged files are refused), at most 8 MiB
  and 25 megapixels - both checked before decoding. The stored photo is never the upload: it is
  re-encoded to square 128 px and 384 px JPEGs with all metadata (EXIF/GPS, ICC, comments)
  removed, under a server-made random name. The old files are removed only after the new photo
  is committed; a failed upload leaves the old photo and no stray file.
* **Where it shows.** The leaderboard (small) and the public contact card (large); the upload
  screen says so. A missing or failing photo shows the summit robot. Phone, e-mail and consent
  are in no public response.
* **Storage.** `MEDIA_ROOT` (default `/data/media`) on the named volume `facemark_media_data`
  (`media_data` in production): photos survive a container rebuild; no external service.
  `python -m app.cli purge-orphan-photos [--dry-run] [--grace-minutes N]` removes files no
  attendee references. Details and backups: [DEPLOYMENT.md](DEPLOYMENT.md#profile-photos-media-volume).
* **Migration** `0014_profile_photo` adds `users.profile_version` and `users.photo_key`
  (additive; no row rewritten).

## Admin lists: attendees and points ledger

`GET /api/admin/attendees` and `GET /api/admin/points-log` are administrator-only
(401 anonymous, 403 booth staff), never cached (`Cache-Control: no-store`), and
return one page at a time: `{ "items": [...], "next_cursor": "..." | null, "limit": 50 }`.

* **Ordering and paging:** newest first, ordered by `(created_at DESC, id DESC)`.
  The unique `id` is the tie-breaker, so rows with the same timestamp keep one
  fixed order and a page boundary can fall between them without repeating or
  skipping a row. Paging is keyset-based: pass the previous page's `next_cursor`
  as `cursor`; `next_cursor` is `null` on the last page. A row added while you page
  appears at the head and does not shift the pages you are reading.
  `limit` defaults to **50**, maximum **100**.
* **Cursors** are opaque, signed, and bound to the list *and* the filters they were
  issued for. A forged or damaged cursor, one from the other list, or one used
  with different filters/search/dates is a `422`. (The page size is not part of
  the binding.)
* **Attendee filters:** `q` (search; each word must occur in first name, last name
  or e-mail, case-insensitive, `%`/`_` are literal), `status` (`ACTIVE` /
  `PROVISIONAL`), `sponsor_consent` (`true`/`false`), `registered_from`,
  `registered_to`.
* **Ledger filters:** `user_id`, `booth_id`, `task_id` (activity), `staff_id`,
  `source` (`TASK`/`FEEDBACK`), `entry_type` (`AWARD`/`REVERSAL`), `created_from`,
  `created_to`. Rows show the activity's name; rows without one are labelled
  "Feedback reward" or "Booth-level award (before activities)". Nothing in the
  ledger is changed by reading it.
* **Dates are timezone-aware and half-open:** `from <= created_at < to` - the
  start is **inclusive**, the end is **exclusive**, so consecutive ranges tile
  with no gap and no overlap. A value must carry an offset (`2026-09-01T00:00:00Z`
  or `...+04:00`, where the `+` must be sent as `%2B`); a bare local time is a `422`.
  The admin screen turns the picked days into local-midnight instants and makes the
  *end day inclusive* by sending the start of the following day.
* **Validation:** an unknown parameter, a bad enum/number/date, `limit` outside
  1-100, a start that is not before the end, or a bad cursor is a `422`
  (`VALIDATION_ERROR`) - never silently ignored.
* **Privacy:** attendee rows carry id, name, e-mail, status, sponsor consent and
  registration time only - no phone, LinkedIn, profession or card token.
* **Indexes** (migration `0010_admin_lists`, additive): `users(created_at DESC, id
  DESC)`, `points_log(created_at DESC, id DESC)` and partial indexes for the
  activity and staff filters; the attendee and booth ledger filters use the
  existing `points_log` indexes. Search is a case-insensitive substring scan
  (no extension needed), fine at event scale.
* **Screen:** search/filter form, Search, Reset, Previous/Next with a page number,
  loading / empty / error (with retry) states. Changing a filter returns to page 1
  and drops the cursor history; an answer to an older request can never replace a
  newer one, including the refresh after a saved change.

## Admin audit log view

`GET /api/admin/audit` (administrators only; booth staff `403`, anonymous `401`;
`Cache-Control: no-store`) is a **read-only** view of `admin_audit`: every admin
change (booths, interests, activities, presets, event settings, staff accounts, booth
interest weights) and every CSV export (`export.started` / `export.finished` /
`export.rejected`). Viewing it writes nothing - no audit row, no state.

* **Paging:** newest first by `(created_at DESC, id DESC)`, 50 per page (at most 100),
  the same signed, filter-bound cursor as the other admin lists (a cursor from another
  list or other filters is a `422`).
* **Filters** (all validated, unknown parameters are a `422`): `actor_id`, `action`
  (`booth.update`-style), `target_type` (+ `target_id`, which needs it), and a
  timezone-aware `created_from` (inclusive) / `created_to` (exclusive) range.
* **What a row carries:** id, time, acting staff account (id, name), action, target,
  `before` / `after`, the fields that changed, and for exports a typed `result`
  (outcome - started / refused / stream finished / interrupted / failed - reason, rows
  expected / written / matched, row limit, bytes, duration, snapshot time, the NAMES of
  the filters used). The events of one export share an `operation_id`; filtering by
  `target_type=export&target_id=<operation_id>` lists them together.
* **The raw `before_value` / `after_value` JSON is never sent.** Each kind of event has an
  explicit allowlist of fields (`app/modules/admin/audit.py`); a field is shown only if
  it is on the list AND has the plain type the list says (an integer, a short text, a
  boolean, one of a fixed set of words) - a nested object or list is dropped, so a
  password / hash, OTP, QR or card token, export ticket, session, ciphertext or API key
  can neither be named by a field nor smuggled inside an allowed one. Texts that remain
  also go through the log-masking functions. A staff account's **e-mail and full name**
  (present in old `staff.create` / `staff.deactivate` rows) are personal data not needed
  to read the trail and are **hidden**; the response only counts what was withheld
  (`hidden_field_count`). An event kind the server has no allowlist for is shown as
  metadata only (`details: "limited"`). A test fails if the admin service writes an
  action that is not on the allowlist.
* **Existing rows** are never changed or deleted by this feature (no migration touches
  data); every writer in the repository's history stores only the shapes above.
* **Screen:** the *Audit log* admin section - filters, Search / Reset, Previous / Next,
  per-event details with a before / after table (changed fields marked), the export
  result block (it says the server records what it SENT, not whether the browser saved
  the file), and "Show the events of this export" to see an export's events together.
  Times are shown in the viewer's local time zone; every value is rendered as text,
  never as markup. A late answer never replaces the result of a newer filter.
* **Indexes** (migration `0011_audit_view`): `(created_at DESC, id DESC)`,
  `(staff_account_id, created_at DESC, id DESC)`, `(action, created_at DESC, id DESC)`
  and `(target_type, target_id, created_at DESC, id DESC)`; they replace 0007's two
  indexes, which they fully cover.

## Admin CSV exports

Three exports, administrators only (admin screen: **Export CSV** under the filters
of *Attendees* and *Points ledger*; **Export sponsor contacts** is a separate button
on *Attendees*). Files are named `facemark-<kind>-<UTC date-time>Z.csv` - never with
search text or names - and sent `Cache-Control: no-store` as an attachment.

| Export | Rows | Columns |
|---|---|---|
| `attendees` | every attendee matching the screen's search and filters | `id, first_name, last_name, email, status, sponsor_consent, created_at` |
| `points-log` | every ledger row matching the screen's filters | `id, created_at, user_id, attendee_name, booth_id, booth_name, source, entry_type, points, task_id, activity, entered_by_staff_id, staff_name, entered_by_system` |
| `sponsor-contacts` | **only** attendees who are `ACTIVE` **and** have `sponsor_consent = true` at the snapshot - decided by the server, the request cannot change it (any parameter is a `422`) | `first_name, last_name, profession, email, phone, linkedin_url` - nothing else (no id, no card token, no status) |

**Two steps, so the file streams straight to disk.** `POST /api/admin/exports/<kind>`
(JSON body = the same filters as the list; **no cursor and no page size - an export
is the whole matching set**) validates everything, refuses an over-size export, and
returns `{download_url, rows, ...}`: a **one-time ticket** (256-bit, 60 s, bound to
that administrator, held in server memory). The page then points a hidden frame at
`GET /api/admin/exports/download?ticket=...` and the browser downloads it - the file
is never put into page memory or a Blob, and the URL never carries the search text.
Authority (admin session) and the ticket are checked before the first byte.

**What the page knows - the status API.** The frame hides the download's HTTP response
from the page, so a refused or broken download would be invisible. The prepare answer
therefore also carries a `ref` and `status_url`; the page asks
`GET /api/admin/exports/status/<ref>` (admin-only, only for the administrator who prepared
it - anyone else gets 404; rate-limited separately; `no-store`) about once a second. It
returns outcome and counts only - never CSV content, filter values or the ticket - as a
`state` (`ready`, `starting`, `streaming`, `finished`, `interrupted`, `failed`, `rejected`,
`expired`), a `code` and what a retry needs: `same_link` (the ticket is still valid, e.g.
another export was running or maintenance mode was on - the page re-points the frame at the
same link), `new_export` (the ticket is spent or the export failed - the page prepares a new
one with the same filters) or `none`. A refused download (used/expired ticket `410`, busy
`409`, size limit `413`, start-up error `503`/`500`) is recorded there and shown in the
main page with the right retry; a session that ended is a `401` on the status call itself.
The page claims only what it knows: *prepared*, *download requested*, then whatever the
server reports. **"The server finished sending the file" means the server wrote the whole
response - not that the browser received or saved it**; that cannot be known from the page,
and the text says so.

**Snapshot.** The download runs in one `REPEATABLE READ, READ ONLY` transaction on its
own connection. The row count, the header values and every row come from that single
snapshot, so registrations, edits, deletions, consent changes and new awards that
happen while the file is being written cannot duplicate or drop a row. The instant
of the snapshot is sent as `X-Export-Snapshot-At` and recorded in the audit.
**For sponsors this is what "currently consents" means: consent as committed at that
instant.** A withdrawal committed *after* it is not in the file - re-export right
before using a sponsor list, and treat the file as a point-in-time copy.

**Memory and limits.** Rows are read through a server-side cursor in batches of 500
and each batch is encoded and sent before the next is read, so memory depends on the
batch, not on the result size. Bounds (`EXPORT_MAX_ROWS`, default **100000**;
`EXPORT_MAX_SECONDS`, default **120**): a matching set over the row limit is refused
*before it starts* (`413 EXPORT_TOO_LARGE`; narrow the filters); an export that runs
past the time limit is aborted; a client that stops reading is cut off (database
`idle_in_transaction_session_timeout` plus a watchdog) and its connection is released.
One export per administrator at a time (`409 EXPORT_BUSY`); preparing is rate-limited.
Starting an export while a database reset is waiting for the table lock delays that
reset by at most the time limit.

**Logs never carry the ticket.** A ticket in a log line is a credential for personal data
while it is live, so every log hides it, whatever the order of the query parameters and
however the name or the `=` is percent-encoded (`?x=1&ticket=T`, `?%74icket=T`,
`?ticket%3DT`, the whole link nested inside another parameter): the application's own
logs (uvicorn access/error and the app logger; the filter is installed on the loggers *and*
their handlers, and masks exception text too), the proxy (`deploy/Caddyfile`: both the access
log and the runtime/error log use one expression that masks `/c/<card token>`,
`/api/cards/<token>` and `?ticket=<ticket>`; Cookie and Authorization are removed), and
`next dev` (which can only omit a request from its log: every `/api/admin/exports/...`
URL and any `ticket` query parameter is omitted). Card-token masking is unchanged.
**A failed export never looks like a finished one.** Everything that can be checked
(authority, ticket, limits, the snapshot, the first batch) is checked before the
response starts, so those failures are ordinary error responses. If something fails
*after* the response has started the HTTP status can no longer change; the server then
**aborts the connection** instead of ending the stream normally, so the browser reports a
failed download rather than a short file that looks complete. The response also states
the exact row count (`X-Export-Rows`) and the server verifies that exactly that many rows
were written. The screen says "Download started" - never "saved": the server cannot know
that the file reached the disk.

**CSV format.** UTF-8 **with a byte-order mark** (Excel then reads Azerbaijani letters
correctly), RFC 4180 quoting (commas, quotes and line breaks inside a cell are quoted),
CRLF line ends. **Formula injection:** any text a person typed (names, e-mail, profession,
URLs, activity titles) whose first meaningful character is `=`, `+`, `-` or `@` - looking
past spaces, control characters, no-break/zero-width spaces and the BOM that could hide
it - or that starts with a tab or carriage return, is written with a leading apostrophe.
Columns the server generates (ids, point values - including negative reversals - flags,
timestamps) are written as they are. One deliberate exception: a value in the sponsor
`phone` column that is strictly a phone number (`+` then digits, spaces, parentheses,
dots, hyphens only) is kept as typed, since it cannot form a formula; anything else in
that column is neutralised like other text.

**Audit.** `export.started` (administrator, kind, **names** of the filters used - never their
values - snapshot instant, rows expected) and exactly one of `export.finished` /
`export.rejected`. `outcome` is `stream_finished` (the server wrote the whole response -
this says nothing about whether the browser received or the user saved the file),
`interrupted` (the stream was closed early, e.g. the client went away) or `failed`
(with an error class and reason, never the message). Search text, personal data and CSV
content are never recorded. Preparing an export is not audited: nothing has left the
server yet. There is no screen for reading the audit trail yet.

## What is in the box

| Area | Where | Notes |
|---|---|---|
| Attendee app | `frontend/src/app/(attendee)` | Register + email OTP verify, login, profile, dashboard, booth list/detail with match scores, leaderboard, personal QR (`/my-card`) |
| Public contact card | `/c/[token]` | Opened by scanning an attendee QR; `noindex`, `no-referrer` |
| Staff app | `/staff/login`, `/staff`, `/staff/scan` | Booth staff sign in, scan an attendee QR, confirm the attendee, **pick one of the booth's activities**, review (attendee, booth, activity, exact points) and award |
| Admin console | `/admin` | Attendees, booths, interests, tasks (activities: points per award and repeat policy), staff accounts, event config, points log (names the activity of each award) |
| Backend API | `backend/app` | FastAPI modular monolith, PostgreSQL, Alembic migrations (head: `0007_operations`) |
| Realtime | `/api/...` SSE | Invalidation events; the client re-fetches authoritative snapshots |
| PWA | `frontend/public` | Installable, plus a PIN-encrypted offline copy of the attendee's own QR drawing |

The browser only ever talks to the Next.js origin; `/api/*` is proxied
to FastAPI server-side (same-origin, `SameSite=Lax` session cookies).

---

## Prerequisites

| Tool | Version used | Notes |
|---|---|---|
| Docker Desktop / Docker Engine | with Docker Compose v2+ | Required. The backend, frontend and database all run in containers |
| Node.js | v24 | Required for iPhone local HTTPS mode, and to run frontend checks on the host. The frontend container has its own Node install for normal local use |
| npm | v11 | Ships with Node |
| Python | **not required on the host** | The backend's runtime is **Python 3.12**, pinned inside `backend/Dockerfile`. All backend commands (tests, lint, type checks, Alembic) run inside the container |
| Git | any recent version | |

You do **not** need to install PostgreSQL, Python 3.12, or any backend
dependency on your host.

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

Open `.env` and set the values described in
[Configuration](#configuration). **Never commit `.env`** (it is git-ignored);
it holds the Resend API key and the signing secrets.

### 2. Build and start

```bash
docker compose up -d --build
```

This starts three containers, in dependency order:

1. `db` — PostgreSQL 16, waits until `pg_isready` reports healthy
2. `backend` — FastAPI (applies nothing on its own; see migrations below)
3. `frontend` — Next.js

```bash
docker compose ps
```

### 3. Create the schema

```bash
docker compose exec backend alembic upgrade head
```

### 4. Initial data — real event or demo (explicit, never automatic)

Starting the app never seeds or resets the database. Pick **one** of the two
paths below; both are safe to re-run.

**Real event** (`DEMO_MODE=false`) — settings only, no fictional content:

```bash
docker compose exec backend python -m app.cli bootstrap
docker compose exec backend python -m app.cli create-admin --email you@example.org --name "Your Name"
```

`bootstrap` creates only what the system needs to run: the `event_config`
row and the global point presets (5 / 10 / 15). It creates no booths, tasks,
interests, attendees or staff accounts, needs no password, and never
overwrites a value that already exists. `create-admin` asks for the new
administrator's password at a hidden prompt (twice; at least 12 characters).
The password is not accepted as an argument or environment variable, is never
printed or logged, and is stored only as an Argon2id hash. Run it in a real
terminal (do not pass `-T` to `docker compose exec`). If the account already
exists, nothing is changed — the password in particular — and no password is
asked. Add booths, tasks, interests and staff afterwards in the admin console
(`/admin`).

**Demo / rehearsal** (`DEMO_MODE=true`) — the fictional summit:

```bash
docker compose exec backend python -m app.cli seed-demo
```

It creates the demo interests, booths, tasks, presets, the demo staff / admin
accounts (passwords from `SEED_ADMIN_PASSWORD` / `SEED_STAFF_PASSWORD`, no
fallback) and the fictional attendees. It is refused — before touching the
database — unless `DEMO_MODE=true`, and always in production. (The old `seed`
command was split into `bootstrap` and `seed-demo`.)

### Shortcut from PyCharm

Select **Start FACEMARK** in the Run configuration menu and press **Run**.
This starts Docker Desktop if needed, brings the Compose services up with
`http://localhost:3000` as the application origin, waits for
`/api/health`, and opens the browser. It does not seed or reset the database.

---

## Configuration

All settings come from `.env` (template: `.env.example`). Missing required
values fail at startup; no secret has a source-code default.

| Variable | Purpose |
|---|---|
| `ENVIRONMENT` | `development` or `production`. Production hides `/docs`, requires `RESEND_API_KEY`, and refuses `DEMO_MODE=true` |
| `DEMO_MODE` | `false` = real operation (the default in `.env.example`). `true` = rehearsal only: OTP codes are also logged, the admin "Demo codes" tab appears, `reset-demo` and `seed-demo` are enabled |
| `APP_ORIGIN` | The origin the **browser** uses (the frontend, never the backend port). State-changing requests must carry a matching `Origin`; an `https://` value also makes the session cookie `Secure` |
| `OTP_OUTBOX_ENCRYPTION_KEY` | Encrypts OTP codes in the durable email outbox (`generate-otp-key`). Required in production; optional in development, where without it the old in-process sender is used |
| `RESEND_API_KEY`, `OTP_FROM_EMAIL` | Real OTP email delivery through [Resend](https://resend.com). The sending domain (`info@aiworks.az`) must be verified there. With no key and `DEMO_MODE=false`, sending an OTP fails loudly instead of silently dropping the code |
| `SCAN_REF_SECRET` | HMAC key signing staff scan references |
| `TRUSTED_PROXY_CIDRS` | Proxies whose `X-Forwarded-For` is trusted for rate-limit identity. Empty = trust nothing |
| `MEDIA_ROOT`, `MEDIA_MAX_UPLOAD_BYTES`, `MEDIA_MAX_PIXELS` | Optional. Where profile photos are kept (default `/data/media`, a named Docker volume) and the photo limits (8 MiB, 25 megapixels) - see *The attendee's own profile* |
| `EXPORT_MAX_ROWS`, `EXPORT_MAX_SECONDS` | Optional bounds for admin CSV exports (defaults 100000 rows, 120 s) - see *Admin CSV exports* |
| `POSTGRES_*`, `BACKEND_PORT`, `FRONTEND_PORT`, `BACKEND_INTERNAL_URL` | Container wiring; see comments in `.env.example` |
| `SEED_ADMIN_PASSWORD`, `SEED_STAFF_PASSWORD` | Passwords for seeded staff accounts. Change them for anything beyond local use |

### OTP email delivery

OTP emails are sent through Resend from a small background worker so the
request is never blocked and response timing does not reveal whether an
address is registered. Transient failures (network errors, 429, 5xx) are
retried a few times; failures are logged **without** the address or code.
Tests never send real email.

---

## Verification

### Health

```bash
curl http://localhost:8001/api/health      # backend directly
curl http://localhost:3000/api/health      # through the frontend proxy
```
Both return `{"status":"ok"}` (`503` means the backend cannot reach Postgres).

### Quality commands

Frontend — run from `frontend/` (host with Node 24, or
`docker compose exec frontend <command>`):

```bash
npm run lint         # eslint .
npm run typecheck    # tsc --noEmit
npm run test         # vitest run
npm run build        # next build — production build
```

> `npm run build` reads `BACKEND_INTERNAL_URL` at build time. On the host,
> export it first:
> ```bash
> export BACKEND_INTERNAL_URL="http://backend:8000/api"   # Git Bash
> $env:BACKEND_INTERNAL_URL = "http://backend:8000/api"   # PowerShell
> ```

Backend — inside the `backend` container (the `db` container must be healthy):

```bash
docker compose exec backend pytest -q                    # test suite
docker compose exec backend ruff check .                 # lint
docker compose exec backend mypy app tests alembic       # strict type checking
```

**The backend tests never touch your real data.** Every test run creates its
own database on the same PostgreSQL server, named `facemark_test_<purpose>_<12
random hex>` (e.g. `facemark_test_suite_3f9c1a07b2d4`), migrates and seeds it,
and drops it when the run ends; the destructive reset tests and the migration
test get further throw-away databases the same way. They also pin `DEMO_MODE`,
`APP_ORIGIN` and `RESEND_API_KEY` themselves and block outbound email. The
`facemark` database (including real attendees) is only used to connect to the
server and issue `CREATE DATABASE`.

Safety rules of the test-database lifecycle (`backend/tests/suite_db.py`):

* Because every run has its own uniquely named database, **runs can overlap**
  (two terminals, a leftover process) without dropping each other's data.
* A run records the exact name **and PostgreSQL `oid`** of each database it
  creates and can drop **only those**. It refuses anything else: a name it did
  not create, one whose `oid` changed, the application's database, `postgres`,
  templates, or any name not of the form above. It never lists databases and
  drops what matches a prefix, and it drops **nothing at start-up**.
* If migrating or seeding fails, the database that attempt created is dropped
  before the error is shown; an `atexit` hook removes whatever the process
  still owns if it exits without the normal teardown.
* Connection URLs are parsed and rebuilt (not string-edited), so query
  parameters and credentials with special characters survive; credentials are
  redacted from setup errors.

**A run killed with `kill -9`/power loss cannot clean up after itself.** Its
database stays behind (harmless, but it takes disk). To see them - read-only,
nothing is changed:

```bash
docker compose exec backend python -m tests.db_inventory
```

It lists `facemark_test_*` databases (and the three fixed names older versions
used: `facemark_test_suite`, `facemark_m15_reset_test`,
`facemark_m20_migration_test`), how many connections each has, and prints
`DROP DATABASE "<name>";` lines for the idle ones. Removing is **manual and
deliberate**: make sure no test run is in progress, check the list shows "no open
connections", then run the printed command yourself from `psql` connected to the
`postgres` database (e.g. `docker compose exec db psql -U <role> -d postgres`).
There is intentionally no automatic sweep.

### Database migrations

```bash
docker compose exec backend alembic upgrade head     # apply migrations
docker compose exec backend alembic current          # show current revision
```

Seven migrations exist (`0001_baseline` … `0007_operations`). Do not run
`alembic downgrade base` against a database that holds real data.

---

## Operator commands

Run inside the backend container: `docker compose exec backend python -m app.cli <command>`.

| Command | Effect |
|---|---|
| `bootstrap` | Real event: creates only the required initial settings. Idempotent, never overwrites (see above) |
| `create-admin --email E --name N` | Creates the first administrator; password asked at a hidden prompt. Idempotent, never changes an existing account |
| `demo-inventory [--json]` | **Read-only** report of demo-looking staff, booths and attendees in the current database, what depends on them, and what is real. Deletes and deactivates nothing |
| `seed-demo` | Loads the fictional demo summit. Requires `DEMO_MODE=true`; refused in production |
| `generate-otp-key` | Prints a new `OTP_OUTBOX_ENCRYPTION_KEY`; stores nothing |
| `otp-outbox-cleanup [--older-than-days N]` | Expires lapsed OTP deliveries and deletes finished records older than N days (default 30) |
| `purge-orphan-photos [--dry-run] [--grace-minutes N]` | Removes profile-photo files no attendee references, only those older than the grace period (default 60 minutes); touches no other name. `--dry-run` reports only |

The OTP email worker is a separate long-running process: `python -m app.worker` (the `worker` Compose service).
| `purge-provisional` | Deletes PROVISIONAL (never-verified) attendees older than 24 h |
| `reset-demo --confirm` | **Destroys and rebuilds the entire dataset.** Refused unless `DEMO_MODE=true`, `ENVIRONMENT` is not production, and `--confirm` is given. Never run it against a database with real attendees |
| `maintenance --off` | Clears maintenance mode after a failed reset has been investigated |

---

## Use on an iPhone as a home-screen app

For phone mode, run
`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start-phone-app.ps1`
from the repository root while the computer and iPhone use the same Wi-Fi.
This starts a local HTTPS proxy (`scripts/phone-https-proxy.mjs`) and points
`APP_ORIGIN` in `.env` at the computer's current Wi-Fi address. The private
key stays in the git-ignored `.env.phone` directory; the app and database
remain on this computer.

On the iPhone, open the certificate profile URL printed by the launcher
(`http://<computer-ip>:3000/phone-ca.mobileconfig`) in **Safari**. In
**Settings → Profile Downloaded**, install **FACEMARK Local HTTPS**. Then
enable trust in **Settings → General → About → Certificate Trust Settings**.
Only install this profile on your own device. You can remove it later under
**Settings → General → VPN & Device Management**.

Open the HTTPS URL printed by the launcher (`https://<computer-ip>:3443/`) in
Safari. Once it loads without a certificate warning, tap
**Share → Add to Home Screen → Add**. While online in the installed app,
sign in, open **QR kodum**, choose a 6–12 digit PIN and tap
**Offline QR saxla**. After that, opening the installed app without a
connection shows an offline screen where the PIN unlocks an encrypted copy
of the QR drawing. The app stores no offline session, profile, points or
booth data. Sign-out removes the saved QR. The QR may be obsolete after
revocation or reset; another device still needs a connection to the FACEMARK
server to resolve it.

For live features, the computer must remain running and both devices must
use the same Wi-Fi. If the Wi-Fi IP changes or the 30-day local certificate
expires, run the launcher again, install the new profile, and save the
updated offline QR before going offline. While phone mode is active the HTTPS
origin is also used for browser sign-in and QR links; `http://localhost:3000`
is then only for viewing the page on the computer.

> Phone mode rewrites `APP_ORIGIN` in `.env`. Restore
> `APP_ORIGIN=http://localhost:3000` (or run the PyCharm launcher, which
> overrides it for that run) to go back to desktop use.

---

## Going to production

The setup above is the **development** stack. A real server uses a separate,
hardened stack: `docker-compose.prod.yml` (multi-stage images, HTTPS reverse
proxy with automatic certificates, only ports 80/443 published, one backend
worker, explicit migration step) configured through `.env.production`
(template: `.env.production.example`).

```bash
export DC="docker compose --env-file .env.production -f docker-compose.prod.yml"
bash deploy/preflight.sh .env.production
$DC build && $DC up -d db
$DC --profile ops run --rm migrate
$DC --profile ops run --rm cli bootstrap
$DC --profile ops run --rm cli create-admin --email you@your-domain.tld --name "Your Name"
$DC up -d && bash deploy/smoke.sh https://<DOMAIN>
```

**[`DEPLOYMENT.md`](DEPLOYMENT.md)** is the full guide: every environment
variable, first deployment, releasing an update, rollback (and why it depends on
the database schema), backups, and what still needs a real-device check.
**[`OPERATIONS.md`](OPERATIONS.md)** covers running it afterwards: CI, the OTP
e-mail send limit, backup and restore (database and photos together, with a
restore drill), monitoring, alert conditions and what to do about each. In
production the backend refuses to start on an `http://` origin, `DEMO_MODE=true`,
a development/placeholder `SCAN_REF_SECRET`, missing OTP settings, an empty or
public `TRUSTED_PROXY_CIDRS`, or a misspelled `ENVIRONMENT`.

Demo data in an existing database is never removed automatically: run
`demo-inventory`, create the real admin/booths/staff first, then deactivate the
demo ones in the admin console.

---

## Shutdown

```bash
docker compose down
```
Stops and removes the containers; the named volume `facemark_postgres_data`
(and therefore your database contents) is preserved.

`docker compose down -v` also **deletes the database volume and every
registered attendee**. Only use it when you intend to start from nothing.

---

## Troubleshooting

**Docker daemon not running.**
`docker compose up` fails with something like `error during connect... the
system cannot find the file specified`. Start Docker Desktop and wait for it
to report "running" before retrying. There is no host-native fallback.

**PostgreSQL port conflict (`5433` already in use).**
Change `POSTGRES_PORT` in `.env` to a free port and re-run
`docker compose up -d`. (`5433` is the default because `5432` is commonly
taken by another local Postgres.)

**`.env` file missing.**
The backend fails at startup with a Pydantic `ValidationError` naming the
missing field (deliberate). Copy `.env.example` to `.env` and try again.

**Backend cannot connect to the database.**
`GET /api/health` returns `503`. Check `docker compose ps` (is `db`
`Up (healthy)`?) and that the `POSTGRES_*` values in `.env` match what the
volume was initialised with — changing them later does not change an
existing volume.

**Frontend proxy cannot reach the backend.**
`curl http://localhost:3000/api/health` fails but
`curl http://localhost:8001/api/health` works. `BACKEND_INTERNAL_URL` must be
`http://backend:8000/api` (the Compose service name, not `localhost`). Restart
the frontend after changing `.env`:
`docker compose up -d --force-recreate frontend`.

**OTP email does not arrive.**
Check that `RESEND_API_KEY` is set, the sending domain is verified in
Resend, and the backend logs (`docker compose logs backend`) for
`OTP email delivery failed`. Messages that land in spam usually point at
missing SPF/DKIM records for the sending domain.

---

## Repository Layout

```
FACEMARK/
├── ARCHITECTURE.md          # locked system architecture (authoritative)
├── IMPLEMENTATION_PLAN.md   # locked milestone plan
├── README.md
├── docker-compose.yml       # DEVELOPMENT: db + backend + frontend
├── docker-compose.prod.yml  # PRODUCTION: + HTTPS proxy, hardened, no bind mounts
├── DEPLOYMENT.md            # production deploy / update / rollback guide
├── OPERATIONS.md            # CI, OTP e-mail pacing, backup/restore, monitoring and alerts
├── .github/workflows/       # CI (ci.yml)
├── deploy/                  # Caddyfile, preflight.sh, smoke.sh, backup/, monitoring/, tests/
├── .env.production.example  # production environment template (placeholders)
├── .env.example             # environment template (tracked); .env is git-ignored
├── scripts/                 # start-app.ps1, start-phone-app.ps1, phone-https-proxy.mjs
├── backend/
│   ├── Dockerfile, pyproject.toml, alembic.ini
│   ├── alembic/versions/    # 0001_baseline … 0007_operations
│   ├── app/
│   │   ├── main.py          # app factory, routers, /api/health
│   │   ├── cli.py           # seed, purge-provisional, reset-demo, maintenance
│   │   ├── core/            # settings, rate limiting, security helpers
│   │   ├── db/              # engine/session, reset fence, models registry
│   │   ├── seed/            # fixtures + idempotent seeder
│   │   └── modules/         # admin, attendees, auth (incl. OTP delivery), booths,
│   │                        # contact_cards, feedback, interests, leaderboard,
│   │                        # matching, points, realtime, staff
│   └── tests/
└── frontend/
    ├── Dockerfile, package.json, next.config.ts, vitest.config.ts
    ├── public/              # PWA manifest, service worker, offline page, brand images
    └── src/
        ├── app/             # routes: (attendee), c/[token], staff, admin
        ├── components/      # auth, card, dashboard, leaderboard, matching, nav,
        │                    # profile, registration, staff, ui
        └── lib/             # API client, i18n (az/en/ru), QR, scoring, realtime
```
