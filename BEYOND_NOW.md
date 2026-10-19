# AI Summit 2026 · Beyond Now — authorised additions

This document records what the Beyond Now game added to FACEMARK on top of the
original booth-only game described in ARCHITECTURE.md and IMPLEMENTATION_PLAN.md.
Where those documents restrict the product to booth tasks, the organiser's
Beyond Now documents (participant flow, game rules, game idea; Oct 2026) and the
decisions below are the current specification for these modules.

Nothing here has been deployed, migrated, seeded or switched on anywhere. Every
module is inert on an upgraded database until an administrator configures it.

## Confirmed decisions

| Date | Decision |
|---|---|
| handoff | Playground: at most 3 simultaneously active advance bookings; cancelling restores the ability to book. |
| handoff | Offline scans made before 17:30 and synchronised later are accepted only after a Game Master's review; a phone's clock alone is never proof. |
| handoff | Matching: one shared masterclass +10, two +15 (compatibility, not points). |
| handoff | A masterclass waiting place reserves its time against Playground (and now mentor) bookings. |
| handoff | Mobile website (Safari/Chrome), white/red robot design kept. |
| 16 Oct 2026 | Legacy booth TASK/FEEDBACK points stay as history and do not count in the competition ranking. |
| 16 Oct 2026 | Profile +5 = ACTIVE account + sector + ≥1 topic + ≥1 "looking for" + ≥1 "offering"; company and LinkedIn optional. |

Rules defined in code where the documents left a choice (each is documented in
the module's docstring and covered by tests):

* **Mentor groups** (`app/modules/mentors`): an administrator configures each
  session (topic, mentor as announced, start; 20 minutes; group 1–6; waiting list
  0–20). One active (confirmed or waiting) mentor place per attendee at a time;
  booking and cancelling close at the start; a freed seat goes to the first
  person waiting. The host (MENTOR_HOST scope for that session) scans each
  attendee from 5 minutes before the end to 10 minutes after; a waiting person is
  accepted only while the group stays within its size. +5 once in total.
* **Founder** (`app/modules/competition/founder.py`) — **PROVISIONAL, NOT
  CONFIRMED by the organiser; must be decided before activation**: a zone counts when the
  person holds a live (unreversed) award in it — Main Stage = PANEL or QMETER,
  Masterclass, AI Playground, Connection Hub = CONNECTION or MENTOR. Profile is
  not a fifth zone. The +10 is derived: re-computed in the same transaction as
  every award or reversal; a correction that removes a zone reverses it; new
  evidence earns it again. It cannot be reversed by hand. Lucky Draw eligibility
  = live Founder award + competition eligibility.
* **Final result** (`app/modules/competition/final.py`, migration 0023):
  versioned snapshots taken after the close by a Game Master or administrator,
  published explicitly; publishing a newer version supersedes the older one. A
  database trigger makes a snapshot immutable. Public names are first name +
  surname initial with the attendee's own photo or the robot. The Lucky Draw
  list is an eligibility list (CSV from a snapshot); no draw mechanic is invented.
* **Reminders** (migration 0024): email through the existing provider/worker,
  only when `event_config.reminders_enabled` is on AND the attendee opted in.
  12 November 18:00 (event time) and 10 minutes before each confirmed item.
* **Pair photos** (migration 0025): connected pairs only; the partner accepts;
  the big screen needs both people's own screen consent AND a moderator's
  approval; either person can withdraw (files deleted). No points.
* **Offline scans** (migration 0026): see the decision above. A submission
  within 2 minutes of the phone's time and before the close is applied like a
  live scan; anything later waits for review and is then evaluated at the
  reviewed time by the same scan rules.

## Migrations (all additive; each downgrade refuses while its history exists)

| Revision | Adds |
|---|---|
| 0016_interest_card | interest card fields |
| 0017_masterclasses / 0018_masterclass_labels | masterclass programme |
| 0019_playground | Playground slots and bookings (capacity trigger) |
| 0020_competition | competition sources, volunteer role, scopes, panels, scans, attendance |
| 0021_people | blocks, meeting requests, Match Corner tables, meetings, connections |
| 0022_mentors | mentor sessions and bookings (group-size trigger) |
| 0023_final_results | versioned final results (immutability trigger) |
| 0024_reminders | `event_config.reminders_enabled`, `users.reminder_consent_at`, reminder outbox |
| 0025_pair_photos | pair photos |
| 0026_offline_scans | offline scans with replay protection |

## Activation steps (for the operator, only after the release is approved)

1. Back up (OPERATIONS.md, "Backup and restore") and rehearse the restore.
2. `alembic upgrade head` on a COPY first; `python -m app.cli competition-report`
   (read-only) to see what the activation would change.
3. Configure the programme in the admin panel: masterclass event and the
   competition close (17:30 +04:00), panels, Playground slots per booth, Match
   Corner tables, mentor groups, staff scopes (door volunteers, Game Masters,
   mentor hosts). Exclude the organiser team, volunteers, sponsors and speakers
   from the competition.
4. `python -m app.cli competition-backfill-profile --confirm` (awards +5 for
   cards already complete), then turn the competition ranking on.
5. Reminders stay OFF until the operator decides to send real email.

## Runtime safeguards

* `FOUNDER_RULE_CONFIRMED` (setting, default `false`). While false: no Founder
  bonus is awarded or reversed by `recompute`; the attendee sees the four zones
  but no Founder / Lucky Draw status; final snapshots record
  `founder_rule_confirmed: false` and name no Founder; the Lucky Draw CSV is
  refused (`founder_rule_unconfirmed`). Set it to `true` only after the organiser
  has confirmed the qualification rule; the next award or reversal of each
  person then brings their bonus in line, and a NEW snapshot must be taken.
* `event_config.reminders_enabled` (default off) and per-attendee consent.

## Independent audit fixes (17 Oct 2026)

* **Pair-photo bytes** are served only by `GET /api/media/pair/{key}-{size}.jpg`,
  authorised on every request against the row as it is now: the two people, an
  administrator (moderation), or anyone while the photo is accepted, both screen
  consents are given and it is approved. `private, no-store`, no ETag/304. The
  public avatar route serves a key only while it is someone's CURRENT
  `users.photo_key` (checked before any 304), so a pair photo's key - or a file a
  running backup keeps after a withdrawal or replacement - is never public through
  an old avatar URL. Copies a viewer already downloaded cannot be recalled.
* **Offline review**: the scan keeps the card's stable id (`card_id`, never the
  QR secret). Approval states a `verified_at` (the phone's claimed time is kept
  beside it) and a reason; it re-checks in the same transaction that the card is
  live, its owner active, and the scanning account active with its scope, then
  applies the scan at the verified time through the normal scan rules. The
  scanning account can never approve or reject its own scan, whatever else it
  holds. Refusals are recorded as FAILED with a reason and audited, with no
  attendance or points left behind. Rows from before migration 0027 have no card
  evidence: the reviewer must re-scan the attendee's current card. Submissions
  carry the phone's `queue_owner` and `dataset_generation`; the server refuses a
  queue of another account or an earlier dataset.
* **Browser offline queue** (`frontend/src/lib/offlineQueue.ts`): every change is
  one IndexedDB readwrite transaction (serialised across all tabs of the origin;
  a scan counts as queued only after the transaction COMMITTED), per-entry
  updates and a per-entry lease. localStorage is not used for the queue: in real
  Chrome, even under a Web Lock, two tabs lost a write (50 reported, 49 kept).
  Without IndexedDB the queue is unsupported and the desk says the scan was NOT
  saved. A failed write in enqueue/lease/retry/claim/delete is reported, never
  treated as done. Retries on network/408/425/429/5xx with backoff and
  Retry-After; 401 keeps the queue; entries are filed under the account and
  dataset, others are quarantined with an explicit claim/delete. An entry older
  than 24 h is no longer sent automatically; it is shown as expired and only a
  person deletes it. The limit of 200 is admission control for NEW scans only: a
  saved or migrated scan is never trimmed - a change may drop an entry only by
  naming it (answered, or deleted by a person), any other loss aborts the
  write. A backlog over 200 is kept and drains; new scans are refused (and the
  desk says so) until it is below the limit. The first-version key is removed
  only after every entry in it was committed, and only if all of it was readable.
* **Rollout of the queue (operational dependency)**: entries a page of the FIRST
  queue version left in localStorage are moved into IndexedDB by the next page
  (merged by `client_scan_id`) and stay quarantined - they carry no owner, so
  they are never filed under the current account automatically. A still-open
  page of the first version that posts without `queue_owner`/
  `dataset_generation` is answered 503 `CLIENT_UPDATE_REQUIRED` with
  `Retry-After: 60` (that version keeps a scan on >= 502), never a refusal, so
  nothing is applied unbound and nothing is lost. Staff must RELOAD every open
  staff tab after the deploy; until then those tabs keep retrying. Deploy the
  backend first; if the new page meets an older server, a 404/405 without an
  error envelope or a 422 VALIDATION_ERROR is retried, not discarded.
* **Final publication** is refused while an offline scan waits for review
  (`offline_reviews_pending`) or when the ranking/Founder list computed now
  differs from the draft (`stale_snapshot`). Snapshot and publication take SHARE
  locks on users, offline_scans and points_log (the writers' own order), so no
  write can commit between the check and the decision. There is no override; if
  the organiser ever needs one, that is a business decision to make explicitly.
* **Occurrence time**: `points_log.occurred_at` (migration 0027) records when a
  scored activity happened, as verified; Founder uses it, so a pre-close activity
  recorded after the close still counts. Historical rows (NULL) are read by
  `created_at`, exactly what they were judged by before. Nothing is rewritten.
  The ranking tie-break uses `reached_at`: the latest `COALESCE(occurred_at,
  created_at)` of awards that still stand. Fully reversed awards and reversals
  do not date the total. A verified offline activity therefore keeps its verified
  time even if it is recorded after the close. The same rule is used by ranking,
  pagination, the shared snapshot and final publication.
* **Caddy**: `POST /api/attendees/me/pair-photos` has its own exact 9MB matcher;
  every other route keeps 1MB. `deploy/tests/caddy-upload.test.sh` exercises the
  real proxy in an isolated network.

### Turning the Founder rule on (only after the organiser decides)

1. Record the organiser's decision; if it differs from the provisional rule in
   `competition/founder.py`, change the code first and test it.
2. Set `FOUNDER_RULE_CONFIRMED=true` in the production env file (Compose forwards
   it to the backend and worker; it defaults to false) and restart those services.
3. `python -m app.cli founder-recompute` (dry run: what would change), then
   `python -m app.cli founder-recompute --confirm`. This reaches everyone,
   including people who will receive no further award.
4. Take a NEW final snapshot (an earlier one names no Founder and is stale).

## Backup and restore of pair photos

`deploy/backup/backup.sh` counts `pair_photos.photo_key` as referenced (never as
an orphan) and records `pair_photo_rows`, `pair_photos_with_file`,
`pair_photo_keys_sha256` and `pair_photos_state_sha256` (id, status, key, both
screen-consent flags, moderation). `restore.sh` verifies both digests and that
every referenced pair-photo file was restored; a missing referenced file makes
the backup DEGRADED. The media janitor (`python -m app.cli purge-orphan-photos`) treats
pair-photo keys as in use. Nothing deletes a file only because
`users.photo_key` does not name it. Manifests from before 0025 are verified as
before. Pair photo files are served like profile photos: by an unguessable URL
given only to the pair (and, after both consents and moderation, the screen).

## FACEMARK — Organiser Panel (`/staff/organiser`)

One workspace for the FACEMARK team's tools on the day. It is reached by its own
link and from the admin console (never from attendee pages or the staff page),
and it grants nothing by itself: `GET /api/staff/organiser`
(competition/organiser.py) admits an administrator or an account holding a live
GAME_MASTER scope, refuses everyone else (booth staff, door and mentor
volunteers, attendees) and tells the screen which sections this account may
use. Every action inside a section goes to that tool's own endpoint, which
checks its own role or scope again:

| Section | Who | Reused |
|---|---|---|
| Overview | organiser | `/api/screen/live` (state, close, what is missing), `/api/staff/final` (late scans, latest version) |
| Scanning | organiser; the desks follow the account's scopes | `AttendanceDesk` (door entry/exit, mentor end, offline queue) |
| Qmeter | GAME_MASTER | `AttendanceDesk` Qmeter desk: name + phone, panel, what was checked, an explicit confirmation of the person; manual check, no Qmeter integration |
| Points corrections | ADMIN | the ledger list and `PointsReversal` (reason, confirmation screen, append-only REVERSAL, audit) |
| Final results | ADMIN or GAME_MASTER | `FinalReview` (draft, review, publish; safeguards unchanged; Founder stays gated) |
| Venue screens | organiser | links to `/screen/live`, `/screen/final`, `/screen/photos`; panel QR codes for administrators |

## External gates not passed by this work

Real-device tests (iOS Safari, Android Chrome: camera, keyboard, safe areas,
photo upload), a representative load test on the production-sized server, a
restore rehearsal with these tables, and the data-transition of the live
database. None of them can be certified from desktop emulation or unit tests.


## PDF conformance follow-up (18 October 2026)

* **Names:** leaderboard entries now carry `name` (first name + surname initial),
  not `first_name`/`last_name`. Own profiles and authorised staff identity reads
  keep their existing contracts. Deploy backend and frontend together and reload
  old attendee tabs; this is a response-contract change, not a schema migration.
* **Venue display:** `/screen/live` reads the public, `Cache-Control: no-store`
  `/api/screen/live` every 15 seconds while visible. It shares the ranking
  snapshot, shows top ten scoring attendees, recent standing awards, a countdown
  based on server time, and approved, mutually consented pair photos (one at a
  time, rotating every eight seconds; removed feed entries disappear on refresh). Failures
  retain the last display and retry at 5/10/20/30 seconds. Refreshes never overlap.
  After close it shows review, and after publication delegates to the published
  final. No public anonymous SSE stream was added. This is bounded polling, not
  instantaneous push. `/screen/final` and `/screen/photos` remain separate views.
* **Not configured:** missing event, close or competition mode is shown explicitly
  rather than presented as a running competition.
* **Report:** `attended` contains completed sessions; `not_completed` separately
  contains entry-only masterclasses and checked-in Playground sessions. These
  labels do not alter points. UI handles an older server without the new field.
* **Booth matching:** `BOOTH_MATCH_PROFILE_INTERESTS=false` by default ignores old
  hidden `user_interests`; the data is retained. Setting it true restores the
  legacy union. Card topics continue through the existing weighted booth engine;
  this does not invent a sector/goal formula for booths.
* **Readiness:** `python -m app.cli competition-report` retains its activation
  report and adds a read-only checklist. It exits 1 while required configuration
  is missing. Checks include event date/timezone, timetable/deadlines, five
  published masterclasses per block, panels/close, 8-15 Playground booths and
  their quarter-hour slots, mentor group schedule, 4-6 Match Corner tables, and
  active scanner assignments. It prints counts/times/row ids, no contact data.
  Informational Founder/reminder/exclusion lines still require organiser review;
  a passing configuration checklist is not a release or real-device certificate.

Still decisions, not silently implemented: Founder zone qualification; LOGO's
separate opt-in lead flow; strict post-panel-only Qmeter entry; name/phone team
invitation; next-event marketing consent and delivery system.

Confirmed networking rule: a server top-five recommendation in either direction
at mutual QR connection earns 3 points without a meeting reservation. Ordinary
connections earn 1; same-company and best-four limits remain unchanged. Existing
connections retain their recorded classification. Founder qualification remains unconfirmed.
