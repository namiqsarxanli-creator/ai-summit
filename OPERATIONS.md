# FACEMARK — operations runbook

> Beyond Now additions (mentor groups, Founder, final results, reminders, pair photos,
> offline scans; their activation steps and safeguards): see BEYOND_NOW.md. The proxy's
> per-route upload limits are exercised by `bash deploy/tests/caddy-upload.test.sh` (Docker).

What keeps the production stack healthy after it is deployed: CI, OTP e-mail
pacing, backup and restore, monitoring and alerts. Deployment itself (first
install, updates, rollback) is in [DEPLOYMENT.md](DEPLOYMENT.md).

Everything here was prepared and tested against **throw-away resources** (see
[What was verified, and what was not](#what-was-verified-and-what-was-not)).
Nothing in this repository has been run against a live production server, and
none of it creates an external account or sends a notification by itself.

## Contents

1. [Automated vs. the operator's job](#automated-vs-the-operators-job)
2. [CI](#ci)
3. [OTP e-mail pacing](#otp-e-mail-pacing)
4. [Backup and restore](#backup-and-restore)
5. [Monitoring and alerts](#monitoring-and-alerts)
6. [Accepted limitations](#accepted-limitations)
7. [What was verified, and what was not](#what-was-verified-and-what-was-not)

## Automated vs. the operator's job

| Thing | Automated by the repository | Still the operator's job |
|---|---|---|
| Code quality | `ruff`, `mypy --strict`, pytest, frontend lint/typecheck/tests/build, migration check on every push (`.github/workflows/ci.yml`) | Turn on branch protection so a red CI blocks merging; read failures |
| Schema | Migrations validated on a temporary database in CI | Run `migrate` **after a backup** on every release (DEPLOYMENT.md) |
| OTP e-mail | Durable outbox, retries, `Retry-After`, a send-rate limit, expiry of stale codes, hourly purge | Provider account, verified domain, **a limit below the plan's**, the provider's own bounce/complaint dashboard |
| Backups | `backup.sh` (dump + photos + manifest + checksums + retention), `restore-drill.sh` | **Schedule them** (cron example provided), **copy off the server, encrypted**, keep the secrets separately, run a drill monthly |
| Monitoring | Liveness/readiness endpoints, `ops-check`, `monitor.sh`, container health checks, privacy-safe logs | **Run `monitor.sh` on a schedule and route its result to a person** (an uptime service or mail); create the accounts; decide who is on call |
| Host | — | Firewall, SSH, OS and Docker updates, disk monitoring of the host, time sync |

## CI

`.github/workflows/ci.yml` has three jobs, all with a timeout:

| Job | Steps |
|---|---|
| **backend** (60 min) | `tests.ci_guard` → `ruff check` → `ruff format --check` → `mypy --strict` → `tests.ci_migrations` → `pytest` → (on failure) log artifact |
| **frontend** (30 min) | `npm ci` → `npm run lint` → `npm run typecheck` → `npm test` → `npm run build` (production settings below) |
| **deploy-config** (40 min) | `bash -n` + offline tests of the backup and monitoring scripts → `shellcheck` → `docker compose config` with fake values → `caddy validate` → builds of both production images |

**Safety rules, enforced rather than promised**

* **No repository secret is used.** Every credential in the workflow is a
  literal fake. `python -m tests.ci_guard` is the first backend step and fails
  the run if `DATABASE_URL` is not the job's own PostgreSQL service (host
  `localhost`/`postgres`, database `ci_*`), if `RESEND_API_KEY` or an
  `OTP_OUTBOX_ENCRYPTION_KEY*` is set, or if `ENVIRONMENT` is `production`.
  It prints variable names, never values.
* **Databases.** The job gets its own `postgres:16-alpine` service, destroyed
  with the job. The suite still creates and drops its own uniquely named
  databases inside it (`backend/tests/suite_db.py`), and `tests.ci_migrations`
  validates the migrations — single head, `upgrade head`, a second no-op
  `upgrade head`, `downgrade base`, `upgrade head` again — **only** in such a
  temporary database. Model/migration drift (`alembic check`) is part of the
  suite (`test_schema_metadata.py`).
* **No real e-mail.** `tests/conftest.py` blanks the provider and encryption
  keys and makes any real outbound `httpx.post` fail the test; the guard
  refuses a real key before that.
* **pytest's own exit code** decides the step: the command runs with
  `set +e`, its exit code is captured, a 60-line tail is printed, and the step
  exits with that code (never through a pipe that could replace it).
* **Failure diagnostics** are the short tail, a read-only list of leftover test
  database *names* (`tests.db_inventory`), and a 7-day artifact with the test
  log and JUnit report. All of it is fake test data.

**Required settings for the production build**

The frontend has two required build-time settings and one optional one (`frontend/next.config.ts`,
`frontend/Dockerfile.prod`):

| Variable | Value | Why |
|---|---|---|
| `BACKEND_INTERNAL_URL` | a valid URL **ending in `/api`**, e.g. `http://backend:8000/api` | Baked into the `/api/*` rewrite at build time. In production Caddy routes `/api` straight to the backend, so the value is only a fallback — but `next build` fails without it |
| `NEXT_OUTPUT` | `standalone` | Produces the self-contained server `Dockerfile.prod` ships (the Docker build sets it itself) |
| `SITE_ORIGIN` | optional, default `https://aisummit.aiworks.az` | The absolute address link previews (Open Graph `og:url`, `og:image`) point at. Set it only when building for another host (staging); an invalid value falls back to the default |

`NEXT_TELEMETRY_DISABLED=1` is set in CI and the image. There is no
`NEXT_PUBLIC_*` variable: nothing about the environment is baked into the
browser bundle. The *backend* has no build step; its production settings are
runtime (DEPLOYMENT.md, "Environment variables"), validated at start-up.

**Reproduce CI locally**

```bash
cd backend && ruff check app tests alembic && ruff format --check app tests alembic \
  && mypy --strict app tests alembic && python -m tests.ci_migrations && pytest -p no:cacheprovider
cd ../frontend && npm ci && npm run lint && npm run typecheck && npm test \
  && BACKEND_INTERNAL_URL=http://backend:8000/api NEXT_OUTPUT=standalone npm run build
cd .. && bash deploy/tests/backup-scripts.test.sh && bash deploy/tests/monitor.test.sh
```

(The backend commands need `DATABASE_URL` pointing at a PostgreSQL server whose
role may `CREATE DATABASE`; the suite only uses it to create its own databases.)

## OTP e-mail pacing

**The problem.** The per-address and per-IP limits (`app/core/ratelimit.py`)
protect one identity. A crowd registering in the same minute stays far below
each of them yet together asks the provider for more requests per second than
its plan allows. Every `429` then burns one of a row's five attempts, and the
attendees at the back of the queue are failed by a limit nobody told them
about.

**What the worker now does** (`app/modules/auth/otp_throttle.py`,
`app/worker.py`, `app/modules/auth/otp_outbox.py`):

* **An aggregate limit.** At most `OTP_DELIVERY_RATE_LIMIT` sends may *start*
  in any `OTP_DELIVERY_RATE_WINDOW_SECONDS` (default **2 per 1 s**, matching
  Resend's default of 2 requests/second per team). The worker asks the limiter
  *before it claims rows*, so a throttled worker claims nothing and holds no
  lease. **Set it below your provider plan's limit** — other senders on the same
  key share it.
* **A burst queues in the database and drains at that rate.** `N` waiting
  codes take about `N / rate` seconds; with the default 2/s, 600 codes take
  5 minutes — inside the 10-minute code lifetime. Beyond `rate × 600` codes in
  one burst, the last ones expire unsent (and are reported, see below). Raise
  the limit only together with the provider plan.
* **`Retry-After` is honoured in full** for the row (the old 60-second cap
  made a retry arrive too early and burn an attempt). A 429 also **pauses the
  whole worker** for that time (capped at 5 minutes; 10 s if the provider gave
  no hint), so the rest of the queue does not walk into the same limit and lose
  its attempts. The worker then simply asks again.
* **Backoff and expiry stay bounded by the code's life.** Retry delay is
  `max(5/15/45/120 s backoff, Retry-After)`. If the next attempt would land
  after the code expires, the row is ended as `expired` at once — a late
  e-mail with a dead code helps nobody. Just before every send the worker
  still re-checks that the challenge is live and unused; a code the attendee
  has since replaced is skipped **without calling the provider and without
  using a send slot**, so impatient re-requests do not double the quota use.
* **Nothing waits inside a transaction.** Pacing and pauses happen between
  database sessions; the idle wait is a plain event wait. A test
  (`test_a_throttled_worker_waits_with_no_transaction_open`) checks
  `pg_stat_activity` while the worker waits.
* **Still one worker.** No Redis or other infrastructure was added: the limit
  has to be exact for *one* process, which memory provides. See
  [Accepted limitations](#accepted-limitations).

**Settings**

| Variable | Default | Meaning |
|---|---|---|
| `OTP_DELIVERY_RATE_LIMIT` | `2` | sends allowed to start per window (1–500) |
| `OTP_DELIVERY_RATE_WINDOW_SECONDS` | `1` | window length (1–3600 s) |

Both are passed to the worker by `docker-compose.prod.yml`; change them in
`.env.production` and `$DC up -d worker`.

**What the pacing does not do:** it does not know the provider's *daily or
monthly* quota. A plan that runs out answers 429 for hours; the worker pauses
5 minutes at a time, rows expire after 10 minutes, and the monitoring below
shows `expired:otp_expired` and `paused by provider rate limit`. Watch the
quota in the provider's dashboard.

## Backup and restore

### What is backed up, and how the two halves stay consistent

A backup is **one directory**, `facemark-<UTC timestamp>/`:

| File | Content |
|---|---|
| `db.dump` | `pg_dump -Fc` of the database (schema, data, `alembic_version`) |
| `media.tar.gz` | the profile photos (`avatars/<key>-128.jpg`, `-384.jpg`); in-flight `.tmp-*` upload files are excluded |
| `media.sha256` | SHA-256 of every photo file inside the archive |
| `manifest.env` | revision, row counts, digests of attendee ids and photo keys, how many referenced photos are present/missing, sizes — **no personal data** |
| `SHA256SUMS` | checksums of the dump, archive, per-file digests, manifest and missing-photo list |
| `missing-photos.txt` | referenced photo keys missing from the captured archive |
| `COMPLETE` / `DEGRADED` | exactly one marker; degraded copies require explicit acceptance for restore |

**Consistency.** Before the dump, the backup takes the exclusive PostgreSQL
media advisory lock (`419078677`) and holds the same session until the archive
finishes. Application photo deletion and the orphan janitor take a shared lock.
When a backup owns the exclusive lock, deletion is skipped without blocking the
request; old files remain as orphans for a later janitor run. New files are
immutable and are written before their database reference commits.

The script confirms ownership before releasing the lock and checks every
referenced rendition against the archive. Missing files or an unconfirmed lock
produce a `DEGRADED` marker and exit **4**, never `COMPLETE`. Manual deletions,
old application versions and damaged storage are outside the lock's guarantee.
Deploy the cooperating application code before relying on this backup protocol.

Connection loss releases the database lock; a hung session is bounded by
`MEDIA_LOCK_MAX_HOLD` (default 1800 seconds). A lost lock makes the copy degraded.
Legacy format-1 copies remain explicitly restorable but are not counted as
known-good by retention or freshness checks.

### Testing the protocol without production data

`bash deploy/tests/backup-scripts.test.sh` checks refusal paths, retention,
freshness, concurrent lock acquisition and kernel lock release after SIGKILL.
`APP_IMAGE=<built-backend-image> bash deploy/tests/backup-real.test.sh` creates
its own PostgreSQL container, network and media volume. It exercises real dump,
archive and restore, photo cleanup between dump and archive, pre-existing missing
photos, and loss of the media-lock session. It removes only its generated
resources. It uses a minimal fixture schema; the full backend suite separately
tests the media guard against migrated application tables and actual photo routes.
The deployment CI job runs this integration exercise after building its backend.

Local verification on 2026-10-04: the full backend suite passed 1493 tests
(exit 0); lint, formatting and strict typecheck passed for 193 files. Offline
backup checks passed 55 cases and monitoring checks passed 19 cases in Linux.
The real isolated backup exercise passed all three scenarios, including
restoring snapshot-referenced files after concurrent photo cleanup, explicit
degraded restoration, and loss of the media-lock session with no missing files.
The production image built and ran this exercise as its configured non-root user.
GitHub Actions and deployment on the actual production host remain unverified.

### Running it

```bash
export BACKUP_DIR=/srv/facemark-backups          # absolute; created 0700
deploy/backup/backup.sh                           # 0 complete | 1 failed | 3 offsite failed | 4 degraded
deploy/backup/check-backup.sh                     # freshness, for monitoring
deploy/backup/restore-drill.sh                    # restore the newest backup into throw-away targets
```

Defaults assume the production stack (`docker-compose.prod.yml`, volume
`facemark-prod_media_data`); see the header of `backup.sh` for the variables.
The scripts never read `.env.production`: they run `pg_dump`/`pg_restore`
**inside the db container** over its local socket (no password) and tar the
volume read-only from a throw-away container of the same image. Install the
schedule from `deploy/backup/crontab.example` (nightly backup, monthly drill,
hourly freshness check).

**A failed backup never costs the last good one.** Everything is built in a
private `.tmp-…` directory and published with a single `mv`; any failure
removes only that scratch directory and closes the lock session. Retention runs *only after*
a successful backup, only deletes complete, well-named directories older than
`RETENTION_DAYS` (14), and never goes below `KEEP_MIN` (3) complete backups.
A second concurrent run is refused using the Linux kernel's `flock`; no PID
file or stale-lock guessing is used. Install util-linux on the operator host. Disk space
is checked first (`MIN_FREE_MB`, default 1024).

Degraded copies never count toward `KEEP_MIN`. With no known-good copy, the newest
`KEEP_MIN` degraded copies are retained for diagnosis/recovery. A newer degraded
attempt makes freshness monitoring warn even when an older good copy is recent.
To restore one deliberately, pass `--accept-degraded` to `restore.sh` or
`restore-drill.sh`; successful verification still returns exit 3, including a
degraded copy whose only issue is an unconfirmed lock. The default drill selects
only known-good copies.

### Retention, permissions, off-server storage

* **Retention.** Defaults: 14 days, at least 3 backups. A backup contains
  attendees' data; deleting an attendee does not delete them from existing
  backups until those age out. Keep `RETENTION_DAYS` as short as your recovery
  needs allow.
* **Permissions.** The scripts run with `umask 077`: directories `0700`, files
  `0600`. `backup.sh` refuses a `BACKUP_DIR` that other users can enter (on a
  filesystem that cannot hold `0700` — only drills on Windows — set
  `ALLOW_LOOSE_PERMS=1`). **Treat every backup as a secret**: besides names,
  e-mail addresses and phone numbers it holds each attendee's public card
  token in clear (`contact_cards.public_token`) and password hashes.
* **Off-server copies are the point of a backup.** A backup on the same disk
  dies with the server. Set `OFFSITE_HOOK` to an executable that copies
  `$1` (the new backup directory) somewhere else and exits non-zero on failure
  (`deploy/backup/offsite.example.sh` shows rclone-crypt, restic and rsync
  variants; it exits 1 until you edit it). Without a hook `backup.sh` warns on
  every run. The copy must be **encrypted** (client-side), and its key kept
  apart from the backups. A failed hook makes `backup.sh` exit `3`; the local
  backup stays.

### Secrets are NOT in the backup — keep them separately

A backup restores *data*. To run it you also need, stored **outside the server
and outside the backups** (a password manager, a sealed envelope with the
organiser):

| Secret | If lost |
|---|---|
| `.env.production` as a whole (DB password, `SCAN_REF_SECRET`, `RESEND_API_KEY`, `OTP_OUTBOX_ENCRYPTION_KEY`, `DOMAIN`, `ACME_EMAIL`) | Recreate it. A new `POSTGRES_PASSWORD` is fine on a restore into a fresh database volume (the dump holds no role passwords) |
| `OTP_OUTBOX_ENCRYPTION_KEY` | Harmless: codes live 10 minutes. Generate a new one (DEPLOYMENT.md, key rotation); queued codes fail as `payload_unreadable` |
| `SCAN_REF_SECRET` | Outstanding signed scan references stop verifying; staff rescan. Generate a new one |
| `RESEND_API_KEY` | Issue a new key in the provider dashboard |
| The off-server **backup encryption key / repository password** | **The off-server backups are unreadable. Without it they are worth nothing** — store it separately and test the recovery path |
| `caddy_data` volume (TLS certificates) | Re-issued automatically; frequent re-issue hits Let's Encrypt rate limits — back it up if you can |

No key is ever written into a backup, a manifest or a log.

### Restoring, and what the script refuses to do

`deploy/backup/restore.sh` restores into a **new, separately named** database
and media store and then verifies it. It cannot overwrite anything:

* target database: must be named `facemark_restore_<letters/digits/_>`, must not
  exist, must not be the live database or the one the backup came from;
* target media: a Docker volume named `facemark-restore-<…>` that does not
  exist, **or** an absolute directory whose last name starts with
  `facemark-restore-` and that is missing or empty (no `..`);
* the backup must carry `COMPLETE` and match `SHA256SUMS`; `DEGRADED` requires
  `--accept-degraded` and always returns warnings;
* there is no `--force`; an unknown option is an error;
* if anything fails, the database/volume/directory *this run* created are
  removed again (`--keep-on-failure` leaves them for inspection).

Verification (each line is `ok`/`FAIL`; any FAIL aborts with exit 1): migration
revision equals the manifest's; row counts of `users`, `booths`, `points_log`,
`feedback`, `contact_cards`, `staff_accounts`; SHA-256 of the set of attendee
ids and of the set of referenced photo keys; every restored photo file against
`media.sha256`; the number of files; rows with their files. Exit `3` means
verified with warnings (degraded or legacy backup, even if no photo is missing).

**Promoting a restore to production** is deliberately manual (it overwrites
the live data, so it needs a human who has read this):

1. Announce downtime. `$DC stop proxy backend frontend worker` (the database
   keeps running).
2. Keep what you are replacing: `deploy/backup/backup.sh` (a fresh backup of the
   current state), or at least rename: `ALTER DATABASE facemark RENAME TO
   facemark_before_restore` (after `pg_terminate_backend` of any session).
3. Move the restored database into place: `ALTER DATABASE facemark_restore_… RENAME
   TO facemark`. `restore.sh` restored it as a non-superuser role with the same
   name as the restore database, which still owns it: connected to the renamed
   database, run `REASSIGN OWNED BY "facemark_restore_…" TO <POSTGRES_USER>`, then
   `ALTER DATABASE facemark OWNER TO <POSTGRES_USER>` and
   `DROP ROLE "facemark_restore_…"`. For the photos, run a one-off container that copies the restore
   volume into `facemark-prod_media_data` and ends with
   `chown -R 10001:10001 /data/media`.
4. `$DC --profile ops run --rm migrate alembic current` — it must show the
   revision your code expects (`ops-check` also warns on a mismatch);
   run `migrate` if the backup is older than the code.
5. `$DC up -d`, then `deploy/smoke.sh https://<DOMAIN>` and
   `deploy/monitoring/monitor.sh --public https://<DOMAIN>`.
6. Delete the `…_before_restore` copy only after the event is safely over.

### The drill

`restore-drill.sh` restores a backup (the newest by default) into generated
`facemark_restore_drill_<time>_<random>` / `facemark-restore-drill-…` targets,
runs every check, and removes exactly those targets (`KEEP=1` keeps them). Run
it after setting backups up, monthly, and before each release. A backup nobody
has restored is a hope.

## Monitoring and alerts

### Liveness vs. readiness

| Endpoint | Question | Touches the DB? | Use it for |
|---|---|---|---|
| `GET /api/health/live` | Is this *process* answering? | **No** | uptime probe; a watchdog that restarts a hung process |
| `GET /api/health/ready` | May traffic be served? | Yes (`SELECT 1`, 10 s connect timeout) | uptime probe; load-balancer style checks. `503` + `{"status":"unavailable"}` when the database does not answer |
| `GET /api/health` | The original contract (`{"status":"ok"}` iff the database answers); used by the backend container's Docker health check | Yes | unchanged |

A database outage must not make a watchdog restart the backend (it would not
help), so restart decisions use *live*; "send traffic?" decisions use *ready*.
All three are public and say only up/down — **no versions, counts, hosts or
error text**. Detail is available only from a shell on the server.

### The operator's tools

```bash
export DC="docker compose --env-file .env.production -f docker-compose.prod.yml"

# Everything at once (containers, public endpoints, DB, migrations, outbox,
# worker loop, media, backups). Exit 0 ok / 1 warning / 2 critical:
BACKUP_DIR=/srv/facemark-backups deploy/monitoring/monitor.sh --public https://<DOMAIN>

# The pieces:
$DC exec -T backend python -m app.cli ops-check --media          # DB, migrations, outbox, media
$DC exec -T worker  python -m app.cli ops-check --no-db --worker # the worker's own loop
```

`ops-check` is read-only. Its output is made of counts, ages, fixed reason
codes and error *class* names — never an address, a code, a token or a
connection string (tests assert this).

| Check | Source | WARN | CRIT |
|---|---|---|---|
| `database` | `SELECT 1`, 5 s connect timeout | — | unreachable |
| `migrations` | `alembic_version` vs the code's head | differs | unreadable |
| `outbox-backlog` | pending rows that are due now (age of the oldest), retrying rows, expired leases | oldest due ≥ 60 s, or an abandoned lease | oldest due ≥ 180 s (worker stopped or blocked) |
| `outbox-failures` | `failed`/`expired` rows in the last 15 min (`challenge_consumed` ignored: it is normal) | ≥ 1 failed, or ≥ 1 expired unsent | ≥ 10 failed, ≥ 5 expired unsent, or a **systemic** reason: `provider_auth_rejected`, `payload_unreadable` |
| `worker` | status + heartbeat files the worker writes each turn | 3+ failed polls in a row, or paused by a provider rate limit | loop stalled > 60 s, no successful poll for 120 s, 10+ failures, or no heartbeat |
| `media-write` | creates and removes one probe file | — | not writable |
| `media-space` | free fraction of the volume's filesystem | < 15 % | < 5 % |

`monitor.sh` adds container state/health/restart counts (`db backend frontend
worker proxy`), the public `live`/`ready` probes (and says "process up but the
database not answering" when only *ready* fails), and the backup age.
`PING_OK_URL` (optional, yours) is requested **only when the overall result is
OK**, so a silent server raises the alert on your side.

**Media write failures** are visible three ways: `ops-check --media`
(`media-write` CRIT), the log line `MEDIA_WRITE_FAILED: a profile photo could
not be stored (ENOSPC|EROFS|EACCES…)` at the moment an upload fails (errno name
only, never a path), and the existing start-up warning `MEDIA_ROOT is not
writable`.

**Not exposed publicly:** worker state, queue depths, media state and
migration revisions. There is deliberately no metrics endpoint. If you add one
later, put it on an internal-only listener, not behind the public proxy.

### Alert conditions and what to do

Machine-readable examples (probe URLs, thresholds, log patterns) are in
`deploy/monitoring/monitoring.example.yml`; nothing applies them for you.

| Condition | Severity | Meaning |
|---|---|---|
| `live` fails 3× | critical | nothing is serving → [A1](#a1-the-site-is-down-liveness-fails) |
| `ready` fails 2× while `live` is OK | critical | database → [A2](#a2-database-unavailable-readiness-fails-liveness-ok) |
| `worker` CRIT / container unhealthy | critical | no OTP e-mail is sent → [B1](#b1-the-otp-worker-is-stopped-or-stalled) |
| `outbox-backlog` WARN/CRIT, `expired:otp_expired`, `paused by provider rate limit` | warning/critical | delayed or expiring codes → [B2](#b2-otp-e-mails-are-delayed-or-expiring) |
| systemic failure (`provider_auth_rejected`, `payload_unreadable`) | critical | every send fails → [B3](#b3-otp-provider-rejects-everything) |
| `media-write` CRIT, `MEDIA_WRITE_FAILED` in the log, `media-space` WARN/CRIT | critical/warning | photo uploads fail → [C1](#c1-photo-uploads-fail-media-volume) |
| backup older than 26 h / backup job exit 1 or 3 | warning/critical | → [D1](#d1-a-backup-failed-or-is-stale) |
| `migrations` WARN | warning | schema and code disagree — run `migrate` (after a backup) or finish the rollback (DEPLOYMENT.md) |
| certificate < 14 days | warning | Caddy renews by itself; if not, check ports 80/443 and `$DC logs proxy` |

#### A1. The site is down (liveness fails)

1. `$DC ps` — which container is not `healthy`/`running`?
2. `$DC logs --tail 100 proxy backend frontend` — look for crash loops, bind
   errors, a failed certificate.
3. Host: disk full (`df -h`), memory (`free -m`), Docker daemon (`systemctl status docker`).
4. `$DC up -d` brings back stopped containers. Do not `down -v`.
5. Still down after a deploy: roll back (DEPLOYMENT.md, "Rollback").

#### A2. Database unavailable (readiness fails, liveness OK)

1. `$DC ps db` and `$DC logs --tail 100 db`.
2. Disk full is the usual cause (`df -h`, `docker system df`). Free space — old
   backups on the same disk first — then `$DC restart db`.
3. The application recovers by itself when the database returns (the pool
   pre-pings); the OTP outbox loses nothing during an outage.
4. Data damage or an unrecoverable volume: restore ([Backup and restore](#backup-and-restore)).

#### B1. The OTP worker is stopped or stalled

1. `$DC ps worker`; `$DC logs --tail 100 worker`.
2. `$DC restart worker` is always safe: rows are leased, a crashed worker's
   rows return to the pool, and the provider idempotency key prevents duplicates.
3. If it dies immediately, the log names the cause (a configuration error is
   printed by variable name only).
4. Until it is back, attendees cannot receive codes; codes expire after 10 minutes.
   After recovery, anyone who was stuck requests a new code.

#### B2. OTP e-mails are delayed or expiring

1. `ops-check`: is `outbox-backlog` growing, or is the worker `paused by
   provider rate limit`?
2. **Paused / 429:** the provider is limiting you. Check its dashboard for the
   plan limit and the daily/monthly quota. Lower `OTP_DELIVERY_RATE_LIMIT`
   below the plan's limit if other senders share the key. A used-up quota
   cannot be fixed here — upgrade the plan.
3. **Large registration burst:** the queue drains at `OTP_DELIVERY_RATE_LIMIT`
   per window. If codes expire unsent, raise the limit *only* to what the plan
   allows, or ask attendees to wait and request one new code (never several —
   each replaces the last).
4. **Backlog with a healthy worker and no 429:** provider outage or network
   problem — `$DC logs worker` shows `provider_unavailable`/`network_error`
   reason codes. Check the provider's status page.
5. The Admin screen "Email delivery" lists recent deliveries with masked
   addresses and reason codes.

#### B3. OTP provider rejects everything

* `provider_auth_rejected`: the API key was revoked/typed wrongly, or the sending
  domain is no longer verified. Fix `RESEND_API_KEY` / `OTP_FROM_EMAIL` (DNS, SPF/DKIM)
  and `$DC up -d backend worker`.
* `payload_unreadable`: `OTP_OUTBOX_ENCRYPTION_KEY` changed or was removed while
  codes were queued. Restore the right key (DEPLOYMENT.md, rotation). Affected
  codes were ≤ 10 minutes old; attendees request new ones.

#### C1. Photo uploads fail (media volume)

1. `$DC logs backend | grep MEDIA_WRITE_FAILED` — the errno name tells the cause:
   `ENOSPC` full disk, `EROFS` read-only, `EACCES` wrong owner.
2. `ENOSPC`: free disk space (old backups, `docker system prune` of *unused*
   images — never volumes), then retry.
3. `EACCES`: `docker run --rm -v facemark-prod_media_data:/m alpine chown -R 10001:10001 /m`.
4. Only photo uploads are affected; the application answers `503
   PHOTO_STORAGE_UNAVAILABLE` and the attendee keeps their current photo.

#### D1. A backup failed or is stale

1. Read the last lines of the backup log (`/var/log/facemark-backup.log`). Exit
   `1` means nothing was published and earlier backups are intact; common
   causes: no disk space, database container not running, media volume missing.
2. Exit `3`: a complete backup exists, but its off-server hook failed. Fix the
   remote, then run the hook by hand on that backup.
   Exit `4`: a DEGRADED copy exists. Check `degraded_reason`, `media_lock`, and
   `photos_missing_files` in the manifest; preserve the last known-good copy.
3. Run `deploy/backup/restore-drill.sh` after fixing, to prove the next backup
   restores.

### Logs

* `docker-compose.prod.yml` rotates container logs (10 MB × 5 per service).
* **What is masked:** card tokens in paths (`/c/***`, `/api/cards/***`), export
  tickets, and — in the backend's own logs — the admin search terms in query
  strings (`?q=***`, `?email=***`). **Caddy masks the entire query string**
  (`/api/admin/attendees?***`), because administrators search by name, e-mail
  and phone with GET parameters; cookies and `Authorization` are dropped.
  Database-error text that quotes values (`Key (email)=(…)`, `Failing row
  contains (…)`) and bare e-mail addresses in tracebacks are scrubbed.
* **What is never logged at all:** OTP codes (outside `DEMO_MODE`, which production
  refuses), encryption keys, the provider key, provider response bodies, full
  addresses (the worker logs delivery *ids*, states and reason codes).
* Log lines worth alerting on, all free of personal data: `MEDIA_WRITE_FAILED`,
  `MEDIA_ROOT is not writable`, `worker poll failed`, `OTP provider rate limit:
  sending paused`, `Unhandled exception`.

## Network reachability: Cloudflare and a blocked server address

**What happened (2026-10-18).** The droplet's original address 178.128.203.85
became unreachable from Azerbaijan - every ISP and mobile network tried - while
it answered normally from Europe and the US. The server, Compose stack and
DigitalOcean (no cloud firewall) were fine: traffic died inside the Azerbaijani
networks. A DigitalOcean Reserved IP (129.212.255.142) on the same droplet was
reachable at once; the `aisummit` DNS record now points at it.

**Protection: Cloudflare in front.** The `aisummit` record is proxied by
Cloudflare, so attendees connect to Cloudflare's addresses, never the server's.
What that needs, and where it lives:

* `deploy/Caddyfile` trusts Cloudflare's published ranges (`trusted_proxies`)
  and takes the attendee address from `CF-Connecting-IP` only from them. Every
  upstream gets exactly one X-Forwarded-For; a header a client forges is never
  believed (test: `deploy/tests/caddy-real-ip.test.sh`). Re-check the ranges at
  https://www.cloudflare.com/ips/ before each event and update the one
  `trusted_proxies` line if they changed.
* Cloudflare SSL/TLS mode **Full (strict)**: Cloudflare checks Caddy's Let's
  Encrypt certificate, which keeps renewing over HTTP-01 through the proxy.
* Nothing to change for SSE (a frame every 15 s, Cloudflare's idle limit is
  100 s) or for photo uploads (9 MB here, Cloudflare allows 100 MB).

**If an address is blocked again (minutes, nothing is deleted):**

1. DigitalOcean -> Networking -> Reserved IPs -> Add a Reserved IP -> assign to
   the `aisummit` droplet. Free while assigned. The old address keeps working.
2. Test it from an affected network:
   `curl --resolve aisummit.aiworks.az:443:<new ip> https://aisummit.aiworks.az/api/health`
   and `ssh root@<new ip>`.
3. Cloudflare -> aiworks.az -> DNS -> `aisummit` A record -> Edit -> the new
   address -> Save (keep the proxy status as it is).
4. SSH for deploys always goes to the server's own address (Cloudflare does not
   carry SSH): use the reserved IP.

Keep the reserved IP assigned: unassigning it sends the site back to a blocked
address. Check reachability from local networks (several mobile operators and
the venue Wi-Fi) before the event; the server's own monitor cannot see a block
that happens outside it.

## Accepted limitations

* **In-memory rate limits reset on restart.** The request rate limiter
  (`app/core/ratelimit.py`: OTP requests per e-mail/IP, staff login, photo
  uploads…) and the worker's send throttle live in process memory by design
  (ARCHITECTURE.md invariant #17: one backend worker). A restart of the
  backend empties every window, so an attacker could obtain one extra window's
  worth of attempts after each restart — they cannot cause a restart, and the
  per-identity *persistent* gates (OTP attempt counter, staff `failed_login_count` /
  `locked_until`, the OTP challenge's lifetime) are in the database and survive.
  A restart of the *worker* empties its send window: at most one window
  (2 sends by default) goes out immediately, then pacing resumes; if the provider
  is already limiting, its `429` re-engages the pause. This is accepted for a
  single-server event deployment. A shared store (Redis) becomes necessary only
  if the backend is ever run as more than one process or the throttle must hold
  across several workers — no deployment so far has shown that need.
* **Run one worker.** Each worker enforces the send limit on its own; two would
  double the rate the provider sees. They would never send a row twice.
* **`accepted` is not "delivered".** Bounces and spam placement are invisible
  here (provider webhooks would be the next step).
* **Backups share the server's fate until copied off.** See above.
* **The quick maintenance answer may be up to a second old; the gate is not.**
  Before its transaction a mutation looks at `maintenance_mode` from the
  once-a-second `event_config` read that also feeds `X-Dataset-Generation`
  (`app/modules/realtime/service.py`), for a quick 503 without a query. That
  value is never trusted for a write: every HTTP mutation reads the flag again,
  fresh, inside its fenced transaction right after the fence
  (`gate_on_maintenance` in `app/db/session.py`), and refuses with
  `MAINTENANCE` while it is on. So a failed reset (maintenance left ON, fence
  released, possibly in another process that never notified this one) and
  `maintenance` switched on by hand both keep every write out at once. CLI
  commands and background jobs are not gated, as before.
* **No metrics endpoint / dashboards.** Status comes from `ops-check`,
  `monitor.sh`, container health and logs.

## What was verified, and what was not

**Verified, against throw-away resources only** (details in the change report;
never the live database, volumes or stack):

* Backend: `ruff`, `ruff format --check`, `mypy --strict`, the full pytest
  suite, and the new tests for pacing, `Retry-After`, expiry, waiting without
  an open transaction, `ops-check`, liveness/readiness, the CI guard and log
  privacy.
* `tests.ci_migrations` against a temporary database (`upgrade head`, no-op
  re-upgrade, `downgrade base`, `upgrade head`).
* Frontend lint, typecheck, tests and the production build with the CI settings.
* `docker compose … config`, `caddy validate`, `actionlint` and `shellcheck`.
* A real backup → verified restore exercise: a temporary database
  (migrated, seeded), temporary photo volumes, the scripts run end to end, plus
  negative cases (refused targets, tampered/incomplete backups, failed backup
  leaving earlier backups intact, locks, retention).
* A throw-away Caddy container proving the access log shows `?***`, `/c/***`
  and `/api/cards/***` for the test requests.

**Not verified** — needs a real environment:

* The GitHub Actions workflow has not run on GitHub (it passed `actionlint`; its
  commands were run locally).
* Anything on a real production server: the cron schedule, the off-server hook
  (`offsite.example.sh` is an example that exits 1), real Resend limits and
  `Retry-After` behaviour (tests use a fake provider; no real e-mail was sent),
  restore of a *production-size* dataset (time, disk), `monitor.sh` against
  `docker-compose.prod.yml` containers (its logic was tested against stand-in
  `docker`/`curl`, its pieces against the development containers).
* Alert delivery to a person (no monitoring account exists).


## Beyond Now PDF follow-up: release and configuration

Run `python -m app.cli competition-report` in the intended deployment environment
for a read-only activation/readiness report (exit 1 means missing configuration).
It neither seeds the programme nor changes flags. Read every `info` line as well:
Founder qualification, exclusions and reminders still need an operator decision.
Configure the confirmed 13 November 2026 programme in Asia/Baku; 17:30 local
close is 13:30 UTC. Do not substitute demo speakers or guessed sponsor details.

The day display URL is `/screen/live`; `/api/screen/live` exposes short names,
points and approved photo URLs only. It polls at 15 seconds, pauses when hidden
and backs off on failure. Competition awards are reconciled on those reads;
there is no new anonymous SSE channel. Close transitions to review; publication
transitions to the frozen final. No configured close means no competition award.

Leaderboard responses now expose `name` rather than `first_name`/`last_name`.
Ship the matching backend/frontend together and reload open attendee and staff
tabs. No new migration is needed for this follow-up. Preserve existing backup,
release and rollback procedures; do not deploy unrelated unfinished backup work.

`BOOTH_MATCH_PROFILE_INTERESTS` defaults false. Legacy saved interests are retained
but ignored in booth scoring; an explicit true restores their union with card
topics. This is not a new booth matching formula. Real-device, live email and
production-sized load checks remain separate release gates.
