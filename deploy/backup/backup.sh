#!/usr/bin/env bash
# One consistent backup of the PostgreSQL database AND the profile photos.
#
#   BACKUP_DIR=/srv/facemark-backups deploy/backup/backup.sh
#
# Exit codes
#   0  COMPLETE: fully recoverable, and (if configured) the off-server copy worked
#   1  failed: NOTHING was published, every earlier backup is untouched
#   3  COMPLETE, but the off-server hook failed (the backup exists only here)
#   4  DEGRADED: everything was captured, but a photo the database references is
#      missing from the archive (or the consistency lock could not be confirmed).
#      Kept for diagnosis; it is NOT a known-good backup and is never counted
#      toward KEEP_MIN. Someone must look.
#   75 (internal, never returned) another backup holds the lock
#
# Environment (all optional except BACKUP_DIR):
#   BACKUP_DIR         absolute directory the backups live in (created 0700)
#   DB_CONTAINER       database container; default: `db` of docker-compose.prod.yml
#   COMPOSE_ENV_FILE   default ./.env.production      (only used to find the container)
#   COMPOSE_FILE       default ./docker-compose.prod.yml
#   DB_NAME / DB_USER  default: the container's POSTGRES_DB / POSTGRES_USER
#   MEDIA_VOLUME       docker volume with the photos (default facemark-prod_media_data)
#   MEDIA_DIR          ... or a host directory instead
#   RETENTION_DAYS     known-good backups older than this may be deleted (default 14)...
#   KEEP_MIN           ...but the newest N KNOWN-GOOD backups are always kept (default 3)
#   DEGRADED_RETENTION_DAYS  degraded backups are deleted after this, but only while a
#                      known-good backup exists (default 7)
#   MIN_FREE_MB        refuse to start with less free space than this (default 1024)
#   MEDIA_LOCK_WAIT    seconds to wait for in-flight photo deletions to finish before
#                      giving up (default 120)
#   MEDIA_LOCK_MAX_HOLD  the database ends the lock after this many idle seconds, so a
#                      lock leaked by a hung run cannot outlive it (default 1800)
#   OFFSITE_HOOK       executable run as `$OFFSITE_HOOK <backup dir> complete|degraded`
#   ALLOW_LOOSE_PERMS  =1 only on a filesystem that cannot hold 0700 (drills on Windows)
#   BACKUP_AFTER_DUMP_HOOK  a shell command run between the dump and the archive.
#                      For tests and diagnosis ONLY (it is how the concurrency tests
#                      replace and delete photos in exactly the window that matters).
#
# Two different locks, do not confuse them
#   * `flock` on $BACKUP_DIR/.lock: only ONE backup runs at a time. The kernel
#     releases it when the process dies, however it dies - there is no stale lock
#     file to clean up and nothing to guess about.
#   * the MEDIA lock (a PostgreSQL advisory lock): while it is held no photo file
#     is deleted by the application or the orphan janitor. That is what makes the
#     dump and the photo archive agree. See backend/app/core/media_guard.py for the
#     guarantees and OPERATIONS.md for the whole story.
#
# Order of work: take the media lock -> pg_dump -> archive the photos -> confirm the
# lock was held all along and release it. A photo replaced or removed meanwhile keeps
# its old files until the janitor collects them, so every photo the dump references is
# in the archive. The archive is then checked against the dump anyway: a backup whose
# archive lacks a referenced photo is DEGRADED, never silently "fine".
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/backup/lib.sh
. "$here/lib.sh"

[ -n "${BACKUP_DIR:-}" ] || fm_die "BACKUP_DIR is required"
case "$BACKUP_DIR" in /*) ;; *) fm_die "BACKUP_DIR must be an absolute path" ;; esac
RETENTION_DAYS="${RETENTION_DAYS:-14}"
KEEP_MIN="${KEEP_MIN:-3}"
DEGRADED_RETENTION_DAYS="${DEGRADED_RETENTION_DAYS:-7}"
MIN_FREE_MB="${MIN_FREE_MB:-1024}"
MEDIA_LOCK_WAIT="${MEDIA_LOCK_WAIT:-120}"
MEDIA_LOCK_MAX_HOLD="${MEDIA_LOCK_MAX_HOLD:-1800}"
for n in RETENTION_DAYS KEEP_MIN DEGRADED_RETENTION_DAYS MIN_FREE_MB MEDIA_LOCK_WAIT MEDIA_LOCK_MAX_HOLD; do
  [[ "${!n}" =~ ^[0-9]+$ ]] || fm_die "$n must be a whole number"
done
[ "$KEEP_MIN" -ge 1 ] || fm_die "KEEP_MIN must be at least 1"
[ "$MEDIA_LOCK_WAIT" -ge 1 ] || fm_die "MEDIA_LOCK_WAIT must be at least 1"
[ "$MEDIA_LOCK_MAX_HOLD" -ge 1 ] || fm_die "MEDIA_LOCK_MAX_HOLD must be at least 1"

# --- one backup at a time: flock ------------------------------------------------------------
# The script re-runs itself under `flock`. The lock is owned by the flock process
# (--close: the backup and its children never inherit the descriptor, so a hook that
# daemonises cannot keep it alive). If this run is killed, flock exits and the kernel
# releases the lock at once; a second run then simply starts.
if [ -z "${FM_BACKUP_LOCK_HELD:-}" ]; then
  fm_need flock
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR" 2>/dev/null || true
  status=0
  FM_BACKUP_LOCK_HELD=1 flock --nonblock --conflict-exit-code 75 --close \
    "$BACKUP_DIR/.lock" bash "${BASH_SOURCE[0]}" "$@" || status=$?
  if [ "$status" -eq 75 ]; then
    holder="$(cat "$BACKUP_DIR/.lock.info" 2>/dev/null || true)"
    fm_die "another backup is already running${holder:+ ($holder)}; this run did nothing"
  fi
  exit "$status"
fi
# From here on this process holds the lock. The note is for humans only - nothing
# ever decides anything from it, so there is no window in which it can mislead.
printf 'pid %s, started %s, host %s\n' "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(hostname)" > "$BACKUP_DIR/.lock.info"

fm_resolve_db
fm_resolve_media_source
fm_need tar
fm_need sha256sum

perm="$(stat -c '%a' "$BACKUP_DIR" 2>/dev/null || echo '???')"
if [ "${perm: -2}" != "00" ] && [ "${ALLOW_LOOSE_PERMS:-0}" != "1" ]; then
  fm_die "BACKUP_DIR is accessible to other users (mode $perm); the dump holds personal data - chmod 700 it"
fi
free_kb="$(df -Pk "$BACKUP_DIR" | awk 'NR==2 {print $4}')"
[ "$free_kb" -ge $((MIN_FREE_MB * 1024)) ] || fm_die "less than ${MIN_FREE_MB} MiB free in BACKUP_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FINAL="$BACKUP_DIR/facemark-$STAMP"
TMP="$BACKUP_DIR/.tmp-$STAMP-$$"
PUBLISHED=0
LOCK_OPEN=0
LOCK_SESSION_PID=""
LOCK_IN=""
LOCK_OUT=""

# Writes lines to the lock session. If that session is gone the pipe is closed; the write
# must then be an error we can handle, not a death by SIGPIPE - so SIGPIPE is ignored
# only for the duration of the write (an ignored signal would also be inherited by every
# child started meanwhile).
lock_write() {
  local rc
  trap '' PIPE
  printf '%s\n' "$@" 1>&"$LOCK_IN" 2>/dev/null
  rc=$?
  trap - PIPE
  return "$rc"
}

# On ANY exit: end the media-lock session if it is still open, and remove only this run's
# own scratch directory. Earlier backups are never touched here.
cleanup() {
  local status=$?
  if [ "$LOCK_OPEN" -eq 1 ]; then
    lock_write '\q' || true
    [ -n "$LOCK_SESSION_PID" ] && kill "$LOCK_SESSION_PID" 2>/dev/null || true
  fi
  rm -rf "$TMP"
  if [ "$PUBLISHED" -eq 0 ] && [ "$status" -ne 0 ]; then
    fm_log "backup FAILED - previous backups are untouched"
  fi
}
trap cleanup EXIT
[ ! -e "$FINAL" ] || fm_die "a backup with this timestamp already exists"
mkdir -m 700 "$TMP"

# --- 1. the media lock (before anything is read) ---------------------------------------------------
# A long-lived psql session holds the advisory lock; its stdin/stdout are this script's
# two ends of a pipe. When this script dies for any reason the pipe closes, psql exits,
# the connection ends and PostgreSQL releases the lock.
fm_log "taking the media lock (photo deletions pause until the archive is done)"
coproc LOCKER {
  docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -X -A -t -q -v ON_ERROR_STOP=1 2> "$TMP/media-lock.err"
}
LOCK_SESSION_PID=$!
LOCK_IN="${LOCKER[1]}"
LOCK_OUT="${LOCKER[0]}"
LOCK_OPEN=1
lock_write \
  "SET lock_timeout = '${MEDIA_LOCK_WAIT}s';" \
  "SET idle_session_timeout = '${MEDIA_LOCK_MAX_HOLD}s';" \
  "SELECT pg_advisory_lock(${FM_MEDIA_LOCK_KEY}) AS media_lock \\gset" \
  '\echo LOCKED' || fm_die "could not talk to the database to take the media lock"
answer=""
read -r -t "$((MEDIA_LOCK_WAIT + 20))" answer <&"$LOCK_OUT" || true
if [ "$answer" != "LOCKED" ]; then
  reason="$(tr -d '\r' < "$TMP/media-lock.err" 2>/dev/null | head -n 3 | tr '\n' ' ')"
  fm_die "could not take the media lock within ${MEDIA_LOCK_WAIT}s (a photo cleanup or deletion is running, or the database is unreachable): ${reason:-no detail}"
fi

# --- 2. the database ---------------------------------------------------------------------------------
fm_log "dumping database"
pg_version="$(docker exec "$DB_CONTAINER" pg_dump --version | tr -d '\r')"
docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc --no-owner --no-acl > "$TMP/db.dump" \
  || fm_die "pg_dump failed"
[ -s "$TMP/db.dump" ] || fm_die "the dump is empty"
pg pg_restore --list < "$TMP/db.dump" > /dev/null || fm_die "the dump cannot be read back by pg_restore"

if [ -n "${BACKUP_AFTER_DUMP_HOOK:-}" ]; then
  fm_warn "running BACKUP_AFTER_DUMP_HOOK (tests/diagnosis only)"
  bash -c "$BACKUP_AFTER_DUMP_HOOK" || fm_die "BACKUP_AFTER_DUMP_HOOK failed"
fi

# --- 3. the photos ----------------------------------------------------------------------------------------
fm_log "archiving photos from $MEDIA_LABEL"
fm_media_ro "tar -C /m --exclude='.tmp-*' -czf - ." > "$TMP/media.tar.gz" || fm_die "tar of the media failed"
tar -tzf "$TMP/media.tar.gz" > "$TMP/media.list" || fm_die "the media archive is unreadable"

# --- 4. confirm the lock was held all the way, and release it ----------------------------------------------
# pg_advisory_unlock answers `t` only if THIS session still held the lock; a session that
# was cut (idle timeout, server restart, network) answers nothing at all.
lock_state=lost
if lock_write "SELECT pg_advisory_unlock(${FM_MEDIA_LOCK_KEY});" '\q'; then
  answer=""
  read -r -t 30 answer <&"$LOCK_OUT" || true
  [ "$answer" = "t" ] && lock_state=held
fi
LOCK_OPEN=0
wait "$LOCK_SESSION_PID" 2>/dev/null || true
if [ "$lock_state" = held ]; then
  fm_log "media lock confirmed held throughout, released"
else
  fm_warn "the media lock could NOT be confirmed to have been held throughout"
fi

# Per-file digests of what the archive really holds (extracted into a throw-away
# container, never onto the host), so a restore can prove each photo file intact.
docker run --rm -i --network none --entrypoint sh "$TOOL_IMAGE" -c '
  set -e; mkdir /x; tar -xzf - -C /x; cd /x
  find . -type f | LC_ALL=C sort | while IFS= read -r f; do sha256sum "$f"; done
' < "$TMP/media.tar.gz" > "$TMP/media.sha256" || fm_die "could not hash the archived photos"

# --- 5. what the pair contains (derived from the artifacts themselves) --------------------------------------
fm_log "reading the manifest back out of the dump"
revision="$(fm_copy_column "$TMP/db.dump" alembic_version version_num | head -n1)"
[ -n "$revision" ] || fm_die "the dump has no alembic revision (is this the FACEMARK database?)"

ids="$(fm_copy_column "$TMP/db.dump" users id | LC_ALL=C sort -n | paste -sd, -)"
keys_sorted="$(fm_copy_column "$TMP/db.dump" users photo_key | LC_ALL=C sort)"
keys_joined="$(printf '%s' "$keys_sorted" | paste -sd, -)"
photos_referenced="$(printf '%s' "$keys_sorted" | grep -c . || true)"

# Which referenced photos does the archive really hold (every rendition)?
present_keys="$(
  { grep -oE 'avatars/[0-9a-f]{32}-[0-9]+\.jpg$' "$TMP/media.list" || [ "$?" -eq 1 ]; } | sed -E 's#avatars/([0-9a-f]{32})-([0-9]+)\.jpg#\1 \2#' \
    | awk -v sizes="$FM_PHOTO_SIZES" 'BEGIN { n = split(sizes, s, " ") } { have[$1 " " $2] = 1; keys[$1] = 1 }
        END { for (k in keys) { ok = 1; for (i = 1; i <= n; i++) if (!((k " " s[i]) in have)) ok = 0; if (ok) print k } }' \
    | LC_ALL=C sort
)"
printf '%s\n' "$keys_sorted" | sed '/^$/d' > "$TMP/.referenced"
printf '%s\n' "$present_keys" | sed '/^$/d' > "$TMP/.present"
comm -23 "$TMP/.referenced" "$TMP/.present" > "$TMP/missing-photos.txt"
missing="$(grep -c . "$TMP/missing-photos.txt" || true)"
referenced_present="$(comm -12 "$TMP/.referenced" "$TMP/.present" | grep -c . || true)"
orphans="$(comm -13 "$TMP/.referenced" "$TMP/.present" | grep -c . || true)"
media_files="$(grep -cE 'avatars/[0-9a-f]{32}-[0-9]+\.jpg$' "$TMP/media.list" || true)"
rm -f "$TMP/media.list" "$TMP/.referenced" "$TMP/.present"

# --- 6. classify: known-good or degraded ----------------------------------------------------------------------
reasons=()
[ "$missing" -eq 0 ] || reasons+=("missing_photos")
[ "$lock_state" = held ] || reasons+=("media_lock_unconfirmed")
if [ "${#reasons[@]}" -eq 0 ]; then
  backup_status=complete
  degraded_reason=none
else
  backup_status=degraded
  degraded_reason="$(IFS=+; echo "${reasons[*]}")"
fi

{
  echo "format=2"
  echo "created_utc=$STAMP"
  echo "source_db=$DB_NAME"
  echo "alembic_revision=$revision"
  echo "pg_dump_version=$(printf '%s' "$pg_version" | tr -c 'A-Za-z0-9._-' '_')"
  echo "status=$backup_status"
  echo "degraded_reason=$degraded_reason"
  echo "media_lock=$lock_state"
  for t in $FM_COUNT_TABLES; do
    echo "rows_$t=$(fm_copy_count "$TMP/db.dump" "$t")"
  done
  echo "users_ids_sha256=$(fm_sha256_of_text "$ids")"
  echo "users_with_photo=$photos_referenced"
  echo "photo_keys_sha256=$(fm_sha256_of_text "$keys_joined")"
  echo "photos_missing_files=$missing"
  echo "photos_present=$referenced_present"
  echo "media_orphan_keys=$orphans"
  echo "media_files=$media_files"
  echo "dump_bytes=$(wc -c < "$TMP/db.dump" | tr -d ' ')"
  echo "media_bytes=$(wc -c < "$TMP/media.tar.gz" | tr -d ' ')"
} > "$TMP/manifest.env"

# --- 7. seal and publish -----------------------------------------------------------------------------------------
(cd "$TMP" && sha256sum db.dump media.tar.gz media.sha256 missing-photos.txt manifest.env > SHA256SUMS)
if [ "$backup_status" = complete ]; then marker=COMPLETE; else marker=DEGRADED; fi
: > "$TMP/$marker"
chmod 600 "$TMP"/*
# One rename: a backup either exists complete or does not exist.
mv "$TMP" "$FINAL"
PUBLISHED=1
status=0
fm_log "backup $backup_status: $FINAL ($(du -sk "$FINAL" | cut -f1) KiB)"
if [ "$backup_status" = degraded ]; then
  fm_warn "DEGRADED backup (${degraded_reason}): it is kept for diagnosis but is NOT a known-good backup"
  [ "$missing" -eq 0 ] || fm_warn "$missing referenced photo(s) are missing from the archive - their keys are in $FINAL/missing-photos.txt"
  status=4
fi

# --- 8. retention (only after success; degraded backups never count as good) --------------------------------------
fm_prune "$BACKUP_DIR" "$RETENTION_DAYS" "$KEEP_MIN" "$DEGRADED_RETENTION_DAYS"
# Scratch directories of runs that were killed (never this run's: it is gone already).
find "$BACKUP_DIR" -maxdepth 1 -type d -name '.tmp-*' -mtime +1 -exec rm -rf {} + 2>/dev/null || true
if [ "$backup_status" = degraded ] && [ -z "$(fm_list_backups "$BACKUP_DIR")" ]; then
  fm_warn "there is NO known-good backup in $BACKUP_DIR - fix the cause and run the backup again"
fi

# --- 9. off-server copy -----------------------------------------------------------------------------------------------
if [ -n "${OFFSITE_HOOK:-}" ]; then
  fm_log "running the off-server hook"
  if ! "$OFFSITE_HOOK" "$FINAL" "$backup_status"; then
    fm_warn "the off-server hook FAILED - this backup exists only on this server until it is copied"
    [ "$status" -ne 0 ] || status=3
  fi
else
  fm_warn "no OFFSITE_HOOK set: this backup lives on the same server as the data (see OPERATIONS.md)"
fi
exit "$status"
