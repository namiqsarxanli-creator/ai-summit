# FACEMARK — production deployment

How to run FACEMARK on a real server, update it, and go back. This covers
`docker-compose.prod.yml`; the development stack (`docker-compose.yml`) is
unchanged and must not be used on a server.

Day-two operations - CI, OTP e-mail pacing, **backup and restore**, **monitoring
and alerts**, and what is automated versus left to the operator - are in
[OPERATIONS.md](OPERATIONS.md).

## What runs

```
Internet ──443/80──▶ proxy (Caddy: HTTPS, headers, log masking)
                       ├── /api/*  ──▶ backend  (FastAPI, ONE worker; REST + SSE)  ──▶ db (PostgreSQL 16)
                       └── /*      ──▶ frontend (Next.js standalone server)             ▲
                                                                                        │ outbox table
                                       worker   (python -m app.worker; sends OTP email) ┘
```

The **worker** is a second process from the same backend image. Registration
and login write the OTP email into a database outbox in the same transaction
as the code; the worker sends it. See
[OTP email delivery](#otp-email-delivery-outbox--worker).

* One public origin, `https://<DOMAIN>`. The browser never talks to the backend
  or the database directly; only the proxy publishes ports (80, 443/tcp+udp).
* Caddy gets and renews the Let's Encrypt certificate by itself.
* `/api/*` goes from Caddy straight to the backend (not through the Next.js
  rewrite), which removes one hop that could buffer the realtime stream. The
  site stays same-origin, so `SameSite=Lax` cookies work unchanged.
* The backend runs exactly **one** worker. The realtime EventBus and the rate
  limiter live in process memory (ARCHITECTURE.md invariant #17); a second
  worker would silently split them. Do not scale `backend` or add `--workers`.
* The database schema is changed **only** by the explicit `migrate` step below —
  never automatically when a container starts.

Containers run as non-root users (backend `10001`, frontend `node`), with all
Linux capabilities dropped, `no-new-privileges`, and read-only root filesystems
(`/tmp` is a tmpfs). There are no source bind mounts. The one writable place
besides `/tmp` is the backend's **media volume** (`/data/media`, the attendees'
profile photos - see [Profile photos](#profile-photos-media-volume)).

## Requirements

* A Linux server with Docker Engine and the Compose v2 plugin.
* A DNS `A` (and `AAAA` if you have IPv6) record for your domain pointing at the
  server, and ports **80** and **443** (TCP, and UDP for HTTP/3) reachable from
  the internet. Let's Encrypt validates over them.
* The sending domain of `OTP_FROM_EMAIL` verified in Resend (SPF/DKIM), and a
  Resend API key. Attendees cannot register without working email.

## Environment variables

Copy `.env.production.example` to `.env.production` on the server, fill it in,
`chmod 600 .env.production`. It is git-ignored. **Never paste its values
anywhere.** `ENVIRONMENT=production` and `DEMO_MODE=false` are fixed inside the
compose file — do not add them.

| Variable | Required | Meaning |
|---|---|---|
| `DOMAIN` | yes | Public host name, e.g. `summit.your-domain.tld` |
| `APP_ORIGIN` | yes | Exactly `https://<DOMAIN>`. Drives the CSRF Origin check and the `Secure` cookie flag |
| `ACME_EMAIL` | yes | Contact address for Let's Encrypt |
| `POSTGRES_PASSWORD` | yes | Database password; letters/digits/`._~-` only (it sits in a URL). Fix it before the first start — it is stored in the database volume |
| `SCAN_REF_SECRET` | yes | ≥ 32 characters, random. Signs staff scan references |
| `RESEND_API_KEY` | yes | Real Resend key. There is no demo fallback in production |
| `OTP_FROM_EMAIL` | yes | `Name <address@verified-domain>` |
| `OTP_OUTBOX_ENCRYPTION_KEY` | yes | 32 random bytes, URL-safe base64 (`cli generate-otp-key`). Encrypts OTP codes while they wait in the outbox. Production refuses to start without it |
| `OTP_OUTBOX_ENCRYPTION_KEY_PREVIOUS` | no | The old key, only during a key rotation (see below) |
| `OTP_DELIVERY_RATE_LIMIT`, `OTP_DELIVERY_RATE_WINDOW_SECONDS` | no | The worker starts at most LIMIT sends per WINDOW seconds (default 2 per 1 s). Keep it **below your e-mail provider's request limit** - [OPERATIONS.md](OPERATIONS.md#otp-e-mail-pacing) |
| `MEDIA_ROOT` | no | Where profile photos are stored. Fixed to `/data/media` by the compose file (the `media_data` volume); do not override it in the env file |
| `MEDIA_MAX_UPLOAD_BYTES`, `MEDIA_MAX_PIXELS` | no | Photo limits (default 8 MiB and 25 megapixels). If you raise the byte limit, raise the 9MB route limit in `deploy/Caddyfile` with it |
| `FACEMARK_VERSION` | no | Tag for the built images (default `local`). Change it each release (see Rollback) |
| `POSTGRES_USER`, `POSTGRES_DB` | no | Default `facemark` |
| `HTTP_PORT`, `HTTPS_PORT` | no | Host ports for the proxy (default 80/443) |
| `INTERNAL_SUBNET`, `PROXY_IPV4` | no | Private network and the proxy's fixed address in it (defaults `172.30.77.0/24`, `172.30.77.10`). Only `PROXY_IPV4` is trusted for `X-Forwarded-For` |

The backend validates all of this at startup and **refuses to start** (with a
message that names variables, never values) if, in production: `APP_ORIGIN` is
not `https://` or is localhost/an IP/an example domain; `DEMO_MODE=true`;
`SCAN_REF_SECRET` is a known development value, a placeholder, or shorter than
32 characters; `RESEND_API_KEY` or `OTP_FROM_EMAIL` is missing or a placeholder;
`TRUSTED_PROXY_CIDRS` is empty or contains a public range; or `ENVIRONMENT` is
anything other than `development`/`production` (a typo such as `prod` is an
error, not "not production").

`TRUSTED_PROXY_CIDRS` is derived by the compose file as `<PROXY_IPV4>/32`. A
client-supplied `X-Forwarded-For` therefore has no effect: Caddy overwrites it
with the real peer address, and the backend only reads it when the connection
comes from that one proxy address. (Verified: forged `X-Forwarded-For`,
`Forwarded`, `X-Real-IP`, `X-Forwarded-Proto` and `-Host` headers do not reach
the backend in usable form.)

## Shortcuts used below

```bash
export DC="docker compose --env-file .env.production -f docker-compose.prod.yml"
```

`$DC config --quiet` validates the file and prints nothing on success; on a
missing variable it names the variable only. **Do not run `$DC config`
without `--quiet` — it prints secrets.**

## First deployment

```bash
git clone <repository> facemark && cd facemark
cp .env.production.example .env.production      # edit it; chmod 600
bash deploy/preflight.sh .env.production          # must say "Preflight passed."

$DC build                                          # builds backend + frontend images
# Generate the OTP outbox key once and put it in .env.production as OTP_OUTBOX_ENCRYPTION_KEY.
# This needs no configuration, so it works before the env file is complete:
docker run --rm --entrypoint python facemark-backend:<FACEMARK_VERSION> -m app.modules.auth.otp_crypto
$DC up -d db                                       # database only
$DC --profile ops run --rm migrate                 # create the schema (alembic upgrade head)
$DC --profile ops run --rm migrate alembic current # must show "(head)"
$DC --profile ops run --rm cli bootstrap           # event settings only, no demo data
$DC --profile ops run --rm cli create-admin --email you@your-domain.tld --name "Your Name"
#                                                    ^ asks for the password at a hidden prompt (needs a terminal)
$DC up -d                                          # proxy + backend + frontend + worker
$DC ps                                             # everything "healthy" (including worker)
bash deploy/smoke.sh https://<DOMAIN>              # checks through the real proxy
```

Never run `seed-demo` on a real server; production refuses it anyway.

Then sign in at `https://<DOMAIN>/staff/login` and create the real booths,
tasks, interests and staff in the admin console (`/admin`).

### Replacing demo data on an existing database

If the database already holds demo staff/booths:

1. `$DC --profile ops run --rm cli demo-inventory` — read-only report of what looks
   like demo data and what depends on it.
2. Create the real admin (`create-admin`), real booths/tasks and real staff.
3. Sign in as the real admin and check it works.
4. Only then deactivate the demo staff and demo booths in the admin console.
   Nothing is deleted or deactivated automatically.

## OTP email delivery (outbox + worker)

OTP emails are not sent from the web request. Registration/login create the OTP
challenge **and** an `otp_deliveries` row in one database transaction (a
rolled-back request leaves no email; a committed one cannot lose it), and the
`worker` service sends it. A restart of the backend or the worker therefore
never loses a queued code.

**States** (`otp_deliveries.status`):

| State | Meaning |
|---|---|
| `pending` | Waiting for its (next) attempt |
| `processing` | Claimed by a worker (a 60 s lease; a crashed worker's rows return to the pool) |
| `accepted` | The **email provider accepted** the message (HTTP 2xx). This is **not** proof of inbox delivery: bounces, spam placement and delays happen afterwards and are not visible here |
| `failed` | Permanently undeliverable (provider rejected it, or 5 attempts were used) |
| `expired` | Never sent, deliberately: the code expired, or it was already used or replaced by a newer code |

* **Retries:** up to 5 attempts with backoff 5 s, 15 s, 45 s, 2 min (honouring a
  provider `Retry-After`), never past the code's 10-minute lifetime. The provider
  idempotency key is created once per delivery and sent on every attempt, so a
  retry after an ambiguous timeout cannot produce two emails.
* **Old codes are never sent late:** just before every send the worker re-checks
  that the challenge is unexpired and unused (requesting a new code supersedes the
  previous one).
* **The code is stored encrypted** (AES-256-GCM, key from the environment) only
  while the delivery is live; every final state wipes it, and the database itself
  refuses a ciphertext on a finished row. Verification still uses the Argon2id hash.
* **Language:** the email is written in the language (`az`/`en`/`ru`) the attendee
  was using when they requested the code; clients that send none get `en`.
* **Logs** carry delivery ids, states and reason codes only - never an address, a
  code, the API key or provider response text.

### Operating it

```bash
$DC ps worker                         # should be "healthy" (it writes a heartbeat file)
$DC logs -f --tail 100 worker         # "OTP delivery 42: accepted (attempt 1, reason=-)"
$DC restart worker                    # safe at any time; queued rows are picked up again
$DC exec -T worker python -m app.cli ops-check --no-db --worker   # its loop, read-only
```

**Admin screen:** sign in at `/admin` and open **Email delivery** (API:
`GET /api/admin/otp-deliveries?status=&email=&before_id=&limit=`, admin only). It
lists newest first, with a summary. "Waiting to be sent now" growing means a
stopped worker. It never shows codes, ciphertext or full addresses
(`n***@gmail.com`).

**Reading a failure** (`reason_code`):

| Code | Do this |
|---|---|
| `provider_auth_rejected` | The API key was refused or the sending domain is not verified. Check `RESEND_API_KEY` and the domain in Resend |
| `provider_rejected_message` | The provider refused this one message (typically an invalid recipient address). Nothing to fix; the attendee can retry with the right address |
| `provider_rate_limited`, `provider_unavailable`, `network_error`, `provider_conflict` | Transient problems that outlasted 5 attempts. Check provider status and the server's outbound network; affected attendees can request a new code |
| `payload_unreadable` | The stored code could not be decrypted: the encryption key was changed or removed while codes were queued. See rotation below |
| `worker_lost` | The worker died repeatedly while sending this row. Look at the worker logs |
| `otp_expired` (state `expired`) | The code lapsed before it could be sent (worker down, or provider failing for ~10 min) |
| `challenge_consumed` (state `expired`) | Not sent because the code had already been used or replaced - normal |

Many `pending` rows that are "due now": the worker is down or wedged
(`$DC logs worker`, `$DC restart worker`). Rows reaching `failed` with `provider_*`:
the provider or the credentials.

### Cleanup and retention

The worker expires lapsed rows continuously and deletes finished rows older than
30 days about once an hour. The same can be run by hand or from cron:

```bash
$DC --profile ops run --rm cli otp-outbox-cleanup --older-than-days 30
```

It never touches a live delivery or an OTP challenge, and a finished row holds no code.

### Encryption key: generate, rotate, lose

* **Generate:** `python -m app.modules.auth.otp_crypto` (or, once the app is configured, `python -m app.cli generate-otp-key`) prints a key and stores nothing.
  Keep it as secret as `RESEND_API_KEY`; never reuse a development key.
* **Rotate:** codes live at most 10 minutes, so rotation is quick. (1) Set
  `OTP_OUTBOX_ENCRYPTION_KEY_PREVIOUS=<old key>` and `OTP_OUTBOX_ENCRYPTION_KEY=<new key>`;
  (2) `$DC up -d backend worker`; (3) after 15 minutes remove `..._PREVIOUS` and
  `$DC up -d backend worker` again.
* **Lost or changed without keeping the old one:** nothing permanent is harmed. Codes
  queued under the missing key fail as `payload_unreadable` (or lapse within 10
  minutes) and those attendees request a new code. No attendee data uses this key.
* **Missing in production:** neither the backend nor the worker starts (the error
  names the variable, never a value).

### Development

Without `OTP_OUTBOX_ENCRYPTION_KEY` the development stack keeps its old in-process
sender and its `worker` service idles. To use the outbox locally: put a key in
`.env`, run `docker compose exec backend alembic upgrade head`, then
`docker compose up -d` (the dev Compose file now has a `worker` service).

### Known limits

* `accepted` is not "delivered". Bounces, complaints and spam placement are not
  tracked (Resend webhooks would be the next step).
* Resend's default limit is about 2 requests/second per team. The worker paces itself
  (`OTP_DELIVERY_RATE_LIMIT` per `OTP_DELIVERY_RATE_WINDOW_SECONDS`, default 2 per second):
  a registration burst queues in the database and drains at that speed, pauses the
  whole worker on a `429` (honouring `Retry-After`) and never sends a code that has
  expired or been replaced. See [OPERATIONS.md](OPERATIONS.md#otp-e-mail-pacing).
  The limit is in worker memory and per worker: **run exactly one worker** (a restart
  empties the window - an accepted limitation, explained there).
* A database outage pauses sending and loses nothing.

## Releasing an update

Order matters: **backup → build → migrate → switch containers → smoke test.**
Migrations `0008_otp_outbox` (the OTP outbox table) and `0009_task_points`
(activity award rules) are additive: run them before the new backend and worker start.
The new backend serves attendee pages against the old schema, but **awarding needs
`0009`**, so migrate before staff use the scanner.

```bash
# 1. Backup: database AND photos together, verified, with retention (OPERATIONS.md).
#    Copy it off the server too.
BACKUP_DIR=/srv/facemark-backups deploy/backup/backup.sh

# 2. New code and a NEW image tag; keep the old images on the server
git pull
sed -i 's/^FACEMARK_VERSION=.*/FACEMARK_VERSION=2026.10.2/' .env.production   # or edit by hand
$DC build

# 3. Migrate (only if the release contains migrations; otherwise a no-op)
$DC --profile ops run --rm migrate

# 4. Switch to the new containers (backend restarts: SSE clients reconnect by themselves)
$DC up -d

# 5. Verify
$DC ps
bash deploy/smoke.sh https://<DOMAIN>
```

Containers are not rebuilt or migrated by `up`/restart; a reboot of the server
simply restarts the same images against the same schema (`restart: unless-stopped`).

## Rollback

Rolling back means starting the **previous images**. Whether that is safe
depends on the **database schema**, because the schema is not rolled back by
swapping images.

1. Find out what the old code expects and what the database has:
   ```bash
   $DC --profile ops run --rm migrate alembic current     # revision the database is at
   # With the OLD tag in .env.production:
   $DC --profile ops run --rm migrate alembic heads       # newest revision the old code knows
   ```
2. Decide:
   * **Same revision, or the new migrations were purely additive** (such as
     `0008_otp_outbox`: old code never reads that table, but it also no longer sends
     email through it, so codes still queued at that moment are not sent - attendees
     request a new one; they would have lapsed within 10 minutes anyway) (new tables,
     new nullable columns) → the old code normally still works. Set
     `FACEMARK_VERSION` back to the old tag and run `$DC up -d --no-build`.
   * **The database is ahead and the new migrations changed or removed things
     the old code uses** (renamed/dropped columns, new NOT NULL, changed
     constraints) → the old code can fail or misbehave. Either fix forward with a
     new release, or restore the pre-release backup (below).
   * Using `alembic downgrade` is possible only for migrations that define a
     safe downgrade, and may destroy data written since. Read the migration
     first; **never** run `downgrade base` against a database with real data.
3. Restore from backup (loses everything written after the backup was taken). Do it
   the safe way: restore into a **new** database and photo volume with
   `deploy/backup/restore.sh`, let it verify them, and only then swap them in by hand
   - [OPERATIONS.md](OPERATIONS.md#restoring-and-what-the-script-refuses-to-do),
   "Promoting a restore". `restore.sh` never overwrites the live database; the old
   `dropdb`/`createdb` one-liner is deliberately gone. Then set `FACEMARK_VERSION` to
   the matching old tag and `$DC up -d --no-build`.

Log privacy of CSV exports: `deploy/Caddyfile` masks the one-time export ticket
(`?ticket=`, any parameter order or percent-encoding) in BOTH Caddy logs - the access log
and the runtime/error log - next to the card-token masking. Check a deployment: request
`/api/admin/exports/download?ticket=<random 43 characters>` through the public URL, then
`docker compose -f docker-compose.prod.yml logs proxy | grep -F <that string>` must print
nothing while the masked line `/api/admin/exports/download?***` is there. (The proxy now
masks the **whole query string** in its logs, because administrators search with GET
parameters - `?q=<name, e-mail or phone>`, `?email=<address>` - and those are personal
data.) (Validate the file with
`docker run --rm -v "$PWD/deploy/Caddyfile:/etc/caddy/Caddyfile:ro" -e DOMAIN=localhost
-e ACME_EMAIL=ops@example.test caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile
--adapter caddyfile`.)

`0012_points_reversal` and `0013_staff_password_reset` (points reversal and staff
passwords). Run `alembic upgrade head` BEFORE starting the new code:

* `0012` replaces the `points_log.source_attribution` CHECK with one that also allows a
  FEEDBACK **reversal** entered by an administrator (the original two forms are unchanged,
  so every existing row is still valid) and bounds `points_log.note` to 300 characters. No
  row is read or written. Downgrade restores the old rule and **refuses** (changing
  nothing) while such an administrator-made FEEDBACK reversal exists.
* `0013` adds `STAFF_PASSWORD_RESET` to the `otp_purpose` enum (its own autocommit step:
  PostgreSQL cannot use a new enum value in the transaction that adds it). Downgrade deletes
  the short-lived staff reset challenges and rebuilds the type without the value.
* Without `0012` a FEEDBACK reversal is refused by the database; without `0013` a staff
  password reset request fails. Reversal of TASK awards and the password change work
  either way.
`0014_profile_photo` (attendee profile editing and photos). Run `alembic upgrade head`
BEFORE starting the new code:

* It adds `users.profile_version integer NOT NULL DEFAULT 1` and `users.photo_key text NULL`
  (a format CHECK and a partial unique index). A constant default is a metadata-only change
  in PostgreSQL 11+: no table rewrite, every existing attendee reads as version 1 with no
  photo. No row is read or written.
* Code from before this revision never reads or writes the two columns, so an old backend
  keeps working on a migrated database (rolling back the *code* needs no downgrade).
* Downgrade drops the two columns (nothing else changes). Photo files already written stay
  on the volume as orphans until `purge-orphan-photos` (below) removes them.
* Without `0014` the profile and photo calls fail (a missing column); registration, login,
  points and everything else are unaffected.

* The password **reset by email** needs the email outbox (`RESEND_API_KEY` +
  `OTP_OUTBOX_ENCRYPTION_KEY`) and the running worker. Staff reset codes are never shown in
  the demo log or panel, so a deployment without an outbox cannot reset by email. If the
  only administrator forgot their password there, create another administrator with a
  different email using the `create-admin` command (it never touches an existing account);
  where the outbox is configured, another administrator can start the reset email for them.

`0011_audit_view` (the admin audit log view) only swaps indexes on `admin_audit` - it
adds four composite indexes that end in the list order and drops the two it fully covers
(`ix_admin_audit_created_at`, `ix_admin_audit_staff_account_id`). No row is read or
written. Run it with the other migrations, before the event; the audit endpoint works
without it, only slower on a long trail.

`0010_admin_lists` only adds indexes for the admin attendee / ledger lists
(a plain `CREATE INDEX`: a brief write lock, milliseconds at event scale - run it
before the event). The lists work without it, just without the index speed-up;
the new list endpoints use only existing columns.

The award contract also changed after `0009` (the request names the confirmed
`rule_revision`); that needs no further migration (the revision is derived, not
stored), but a staff page loaded before the deploy is refused with a "reload the
page" message until it is reloaded - no points are created in the meantime.

`0009_task_points` specifically: the old code ignores the new columns, so the
schema is compatible, but the **staff app and the backend must be rolled back
together** (the new scanner sends `task_id`, the old backend expects `points`).
Awards already recorded against activities stay valid for old code. Rolling the
schema itself back (`alembic downgrade 0008_otp_outbox`) drops the activity
point values and repeat settings (not the awards) - read the migration first.

Because rollback depends on old images being present, always bump
`FACEMARK_VERSION` per release, and do not `docker image prune` until a release
has proved itself. Take the backup **before** every `migrate`.

## Profile photos (media volume)

Attendees can upload a profile photo; it appears on the leaderboard and on their contact
card. Photos are small re-encoded JPEG files (no metadata) under `MEDIA_ROOT`
(`/data/media/avatars/<random>-<128|384>.jpg`), kept in the **named volume `media_data`**
(`facemark-prod_media_data`), so they survive `up --build`, image upgrades and container
recreation. No external storage service or credentials are involved.

* **Permissions.** `Dockerfile.prod` creates `/data/media` owned by uid `10001`; a new named
  volume takes that ownership, so the non-root backend can write while its root filesystem
  stays read-only. If the volume came from somewhere else:
  `docker run --rm -v facemark-prod_media_data:/m alpine chown -R 10001:10001 /m`.
* **Backup.** The photos are part of the data, and `deploy/backup/backup.sh` backs them
  up **together with** the database (dump first, photos second, with a manifest that
  records how many referenced photos the archive holds). How the two halves stay
  consistent, and how a restore proves it, is in
  [OPERATIONS.md](OPERATIONS.md#backup-and-restore). A database restored without its
  photos shows the robot where a photo is missing (a missing or failing image falls back
  to it; nothing breaks); photos restored without the database are orphans.
* **Orphan cleanup.** A crash between writing the files and committing the row, a deleted
  or reset account, or a failed removal can leave files no attendee references. They are
  harmless; remove them with the janitor, which touches only files older than the grace
  period (an upload writes its files *before* its row commits) and no name but its own:
  ```bash
  $DC --profile ops run --rm cli purge-orphan-photos --dry-run     # report only
  $DC --profile ops run --rm cli purge-orphan-photos               # default grace: 60 minutes
  ```
* **Proxy.** `deploy/Caddyfile` caps request bodies per route: 1MB everywhere except
  `PUT /api/attendees/me/photo` (9MB, a little above the application's own 8 MiB limit, so
  people normally meet the application's explanation). Caddy stacks `request_body` limits,
  so do not reintroduce a site-wide one.
* **Caching and CSP.** A photo's URL is random and new with every new photo, so it is served
  `Cache-Control: public, max-age=31536000, immutable` (every other `/api/` response stays
  `no-store`). The CSP's `img-src 'self' data: blob:` already allows it and the local
  preview - nothing was widened.
* **Not enough space / unwritable volume.** The upload answers `503 PHOTO_STORAGE_UNAVAILABLE`
  and the attendee keeps their current photo; nothing else is affected.

## Day-to-day

```bash
$DC ps                       # health of every service
$DC logs -f --tail 100 backend
$DC restart backend          # SSE streams drop; clients reconnect and refetch
$DC down                     # stop. NEVER add -v: it deletes the database and the TLS certificates
```

* `postgres_data` holds all attendee data; `media_data` holds their profile photos;
  `caddy_data` holds the certificates (re-issuing too often hits Let's Encrypt rate
  limits). All are named volumes - `down -v` would delete every one of them.
* Proxy logs mask card tokens (`/c/***`, `/api/cards/***`) and drop `Cookie`,
  `Authorization` and `Set-Cookie`. The backend masks its own access log the same way.

## How this was verified (locally, without a real server)

On a throw-away project (`-p facemark-prodsmoke`, ports 18080/18443, its own
volumes, `CADDY_GLOBAL_OPTIONS=local_certs`, fake credentials, all removed
afterwards): images built; migrate/bootstrap/`seed-demo` refusal ran; the stack
came up healthy with only the proxy publishing ports; HTTPS, headers, redirect,
`Secure; HttpOnly; SameSite=lax` cookie, CSRF rejection, SSE (`ready` at once,
heartbeat every 15 s, no buffering), forged forwarding headers, token masking in
proxy logs (including the 502 error path), startup refusal on unsafe
configuration, and data persisting across `down`/`up` were checked. You can
repeat the checks against the real URL at any time with `deploy/smoke.sh`.

## Not covered / do before relying on it

* **Real-device test (ARCHITECTURE.md M-CAM, §17.9):** open the site on an iPhone
  (Safari) and an Android phone (Chrome) over the real domain, scan a printed QR
  with the camera, award points from a second device and see the update within
  about a second; leave a stream open for several minutes. `localhost` does not count.
* **Content-Security-Policy** is enforced (no `unsafe-eval`) but still allows
  `'unsafe-inline'` scripts/styles, which the Next.js runtime needs; it was only
  checked on public pages. A nonce-based policy is a later hardening step.
* **Backups need scheduling and an off-server copy.** The scripts exist and were
  restore-tested on temporary resources (`deploy/backup/`, OPERATIONS.md); installing the
  cron schedule, the off-server hook and the encrypted remote is yours, and a drill on
  the real server (`restore-drill.sh`) is what proves it.
* **Single server, single backend worker, in-memory rate limiting** — no high
  availability by design (ARCHITECTURE.md invariant #17).
* Firewall, SSH hardening, OS updates, the **accounts and routing of your alerts**
  (`deploy/monitoring/monitor.sh` and `monitoring.example.yml` are ready; nobody is
  notified until you connect them) and Resend bounce/complaint handling are outside this
  repository.

### OTP email deliverability

Use `AI Summit <info@aiworks.az>` (or your verified sending address) as
`OTP_FROM_EMAIL`. Recreate the backend and worker after environment changes.
In Resend, confirm the sending domain is Verified and all required SPF and
DKIM records match the values shown in the dashboard. Disable open and click
tracking for OTP emails. Keep the existing plain-text and minimal HTML bodies.

Check a received message's original headers for SPF, DKIM and DMARC results
and alignment with the From domain. DNS record existence alone does not prove
successful authentication. The current DMARC monitoring policy (`p=none`) is
not an inbox guarantee. Review all legitimate domain senders before tightening
that policy; never add a second SPF record or replace unrelated mail records.
Provider acceptance does not guarantee inbox delivery. Monitor bounces and
complaints and avoid sudden sending-volume increases.
