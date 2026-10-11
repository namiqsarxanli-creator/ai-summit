#!/usr/bin/env bash
# Restore a backup into a NEW, separately named database and media store, then
# verify it. It can never overwrite anything that already exists.
#
#   deploy/backup/restore.sh --backup /srv/facemark-backups/facemark-20261004T031500Z \
#       --target-db facemark_restore_20261004 \
#       --target-media-volume facemark-restore-20261004
#   (or --target-media-dir /srv/facemark-restore-20261004)
#
# Refusals (each is a hard stop, no flag overrides them):
#   * the target database must be named  facemark_restore_<letters/digits/_>,
#     must not exist, and must not be the live database (POSTGRES_DB of the db
#     container) or the database the backup came from;
#   * the target media must be a docker volume named  facemark-restore-<...>
#     that does not exist, or an absolute directory whose last name starts with
#     facemark-restore- and that is missing or empty;
#   * the backup must be COMPLETE and every file must match SHA256SUMS. A DEGRADED
#     backup (a referenced photo missing from its archive, or the media lock not
#     confirmed) is restored only with an explicit --accept-degraded, finishes with
#     exit 3, and the missing photos stay missing (the application shows the robot).
# The dump is restored and checked as a non-superuser role named like the target
# database, so a tampered copy cannot reach the live database or the server; the
# photo archive may hold only directories and regular files.
# Restoring INTO the live database or volume is a manual, stack-stopped step
# that this script only describes (OPERATIONS.md, "Promoting a restore").
#
# Exit codes: 0 restored and verified | 1 refused or failed (what this run
# created is removed again) | 3 restored, verified, but with warnings.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/backup/lib.sh
. "$here/lib.sh"

BACKUP="" TARGET_DB="" TARGET_VOLUME="" TARGET_DIR="" KEEP_ON_FAILURE=0 ACCEPT_DEGRADED=0
while [ $# -gt 0 ]; do
  case "$1" in
    --accept-degraded) ACCEPT_DEGRADED=1; shift ;;
    --backup) BACKUP="${2:-}"; shift 2 ;;
    --target-db) TARGET_DB="${2:-}"; shift 2 ;;
    --target-media-volume) TARGET_VOLUME="${2:-}"; shift 2 ;;
    --target-media-dir) TARGET_DIR="${2:-}"; shift 2 ;;
    --keep-on-failure) KEEP_ON_FAILURE=1; shift ;;
    *) fm_die "unknown argument: $1 (see the header of this script)" ;;
  esac
done
[ -n "$BACKUP" ] && [ -n "$TARGET_DB" ] || fm_die "--backup and --target-db are required"
if [ -n "$TARGET_VOLUME" ] && [ -n "$TARGET_DIR" ]; then fm_die "give --target-media-volume OR --target-media-dir"; fi
[ -n "$TARGET_VOLUME" ] || [ -n "$TARGET_DIR" ] || fm_die "a media target is required (--target-media-volume or --target-media-dir)"

fm_resolve_db

# --- validate the backup -------------------------------------------------------------------
[ -d "$BACKUP" ] && [ ! -L "$BACKUP" ] || fm_die "--backup is not a backup directory"
BACKUP="$(cd "$BACKUP" && pwd)"
[[ "$(basename "$BACKUP")" =~ $FM_BACKUP_NAME_RE ]] || fm_die "--backup does not look like facemark-<timestamp>"
DEGRADED_BACKUP=0
if [ -f "$BACKUP/COMPLETE" ] && [ -f "$BACKUP/DEGRADED" ]; then
  fm_die "the backup carries both markers (COMPLETE and DEGRADED); it was altered"
elif [ -f "$BACKUP/COMPLETE" ]; then
  :
elif [ -f "$BACKUP/DEGRADED" ]; then
  [ "$ACCEPT_DEGRADED" -eq 1 ] \
    || fm_die "this backup is DEGRADED (see missing-photos.txt and manifest.env in it) and is not a known-good backup; to restore it anyway add --accept-degraded"
  DEGRADED_BACKUP=1
else
  fm_die "the backup is not marked COMPLETE or DEGRADED (an interrupted backup is never restored)"
fi
for f in db.dump media.tar.gz media.sha256 manifest.env SHA256SUMS; do
  [ -f "$BACKUP/$f" ] || fm_die "the backup is missing $f"
done
(cd "$BACKUP" && sha256sum -c SHA256SUMS >/dev/null 2>&1) || fm_die "the backup does not match its SHA256SUMS (damaged or altered)"
fm_read_manifest "$BACKUP/manifest.env"
case "${M[format]:-}" in
  1) # made before the media lock existed: no status, no missing-photos.txt, no guarantee
    [ "$DEGRADED_BACKUP" -eq 0 ] || fm_die "a format-1 backup cannot be degraded; it was altered"
    DEGRADED_BACKUP=1
    fm_warn "legacy format-1 backup has no consistency guarantee; verified restore will return warnings" ;;
  2)
    if [ "$DEGRADED_BACKUP" -eq 1 ]; then
      [ "${M[status]:-}" = "degraded" ] || fm_die "the DEGRADED marker disagrees with the manifest; it was altered"
    else
      [ "${M[status]:-}" = "complete" ] || fm_die "the COMPLETE marker disagrees with the manifest; it was altered"
      [ "${M[media_lock]:-}" = "held" ] && [ "${M[photos_missing_files]:-}" = "0" ] \
        || fm_die "a COMPLETE backup must confirm the media lock and contain every referenced photo"
    fi ;;
  *) fm_die "unknown backup format" ;;
esac

# --- validate the targets (before anything is created) --------------------------------------------
[[ "$TARGET_DB" =~ ^facemark_restore_[a-z0-9_]{1,40}$ ]] \
  || fm_die "--target-db must look like facemark_restore_<name> (lowercase letters, digits, _)"
[ "$TARGET_DB" != "$LIVE_DB" ] || fm_die "refusing: that is the live database"
[ "$TARGET_DB" != "${M[source_db]:-}" ] || fm_die "refusing: that is the database the backup came from"
[ "$TARGET_DB" != "$DB_NAME" ] || fm_die "refusing: that is the configured database"
exists="$(psql_q postgres "SELECT 1 FROM pg_database WHERE datname = '$TARGET_DB'")"
[ -z "$exists" ] || fm_die "refusing: database $TARGET_DB already exists (a restore never overwrites)"

if [ -n "$TARGET_VOLUME" ]; then
  [[ "$TARGET_VOLUME" =~ ^facemark-restore-[a-z0-9][a-z0-9_.-]{0,60}$ ]] \
    || fm_die "--target-media-volume must look like facemark-restore-<name>"
  if docker volume inspect "$TARGET_VOLUME" >/dev/null 2>&1; then
    fm_die "refusing: volume $TARGET_VOLUME already exists (a restore never overwrites)"
  fi
  MEDIA_TARGET_LABEL="volume $TARGET_VOLUME"
else
  case "$TARGET_DIR" in /*) ;; *) fm_die "--target-media-dir must be an absolute path" ;; esac
  case "$TARGET_DIR" in *..*) fm_die "--target-media-dir must not contain .." ;; esac
  [[ "$(basename "$TARGET_DIR")" == facemark-restore-* ]] \
    || fm_die "--target-media-dir must end in a directory named facemark-restore-<name>"
  if [ -e "$TARGET_DIR" ]; then
    [ -d "$TARGET_DIR" ] && [ ! -L "$TARGET_DIR" ] || fm_die "refusing: $TARGET_DIR exists and is not a plain directory"
    [ -z "$(ls -A "$TARGET_DIR")" ] || fm_die "refusing: $TARGET_DIR is not empty (a restore never overwrites)"
  fi
  MEDIA_TARGET_LABEL="directory $TARGET_DIR"
fi

# --- create (and remember exactly what this run created) -----------------------------------------------
# The dump is restored - and every check of its contents below is run - as a
# role of its own (named like the target database) that is NOT a superuser and
# owns nothing but the target. A backup copy is only as trustworthy as whoever
# could write to where it was kept; SHA256SUMS travels with the copy, so a
# tampered dump would pass it. As that role, the dump's SQL cannot reach the
# live database or run programs on the server. It stays the target's owner;
# drop both together once the restore is no longer needed (OPERATIONS.md).
SUPER_USER="$DB_USER"
RESTORE_ROLE="$TARGET_DB"
role_exists="$(psql_q postgres "SELECT 1 FROM pg_roles WHERE rolname = '$RESTORE_ROLE'")"
[ -z "$role_exists" ] || fm_die "refusing: role $RESTORE_ROLE already exists (a restore never overwrites)"

CREATED_DB=0 CREATED_ROLE=0 CREATED_VOLUME=0 CREATED_DIR=0 OK=0
cleanup() {
  local status=$?
  DB_USER="$SUPER_USER"
  if [ "$OK" -eq 0 ] && [ "$KEEP_ON_FAILURE" -eq 0 ]; then
    # Only what THIS run made, by the names validated above.
    [ "$CREATED_DB" -eq 1 ] && psql_q postgres "DROP DATABASE IF EXISTS \"$TARGET_DB\" WITH (FORCE)" >/dev/null 2>&1 || true
    [ "$CREATED_ROLE" -eq 1 ] && psql_q postgres "DROP ROLE IF EXISTS \"$RESTORE_ROLE\"" >/dev/null 2>&1 || true
    [ "$CREATED_VOLUME" -eq 1 ] && docker volume rm "$TARGET_VOLUME" >/dev/null 2>&1 || true
    [ "$CREATED_DIR" -eq 1 ] && rm -rf "$TARGET_DIR" || true
    [ "$status" -ne 0 ] && fm_log "restore FAILED - the targets this run created were removed again"
  fi
}
trap cleanup EXIT

fm_log "creating role and database $TARGET_DB (not a superuser)"
psql_q postgres "CREATE ROLE \"$RESTORE_ROLE\" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS" >/dev/null
CREATED_ROLE=1
psql_q postgres "CREATE DATABASE \"$TARGET_DB\" OWNER \"$RESTORE_ROLE\"" >/dev/null
CREATED_DB=1
# From here on every query against the restored data runs as the restore role.
DB_USER="$RESTORE_ROLE"
fm_log "restoring the database (one transaction: all or nothing)"
pg pg_restore -U "$RESTORE_ROLE" -d "$TARGET_DB" --no-owner --no-acl --exit-on-error --single-transaction < "$BACKUP/db.dump" \
  || fm_die "pg_restore failed"

fm_log "restoring photos into $MEDIA_TARGET_LABEL"
if [ -n "$TARGET_VOLUME" ]; then
  docker volume create "$TARGET_VOLUME" >/dev/null
  CREATED_VOLUME=1
  MOUNT="$TARGET_VOLUME"
else
  mkdir -p "$TARGET_DIR"
  CREATED_DIR=1
  MOUNT="$TARGET_DIR"
fi
# The application's user (uid 10001, backend/Dockerfile.prod) owns the files; nobody else can read them.
docker run --rm -i --network none --entrypoint sh -v "$MOUNT":/m "$TOOL_IMAGE" -c '
  set -e; tar -xzf - -C /m --no-same-owner
  # A backup only ever holds directories and regular files; anything else
  # (a symlink to a server file, a device, a FIFO) means it was altered.
  if [ -n "$(find /m ! -type f ! -type d | head -n 1)" ]; then
    echo "the photo archive holds something that is not a plain file or directory" >&2
    exit 3
  fi
  chown -R 10001:10001 /m; chmod -R u=rwX,g=rX,o= /m
' < "$BACKUP/media.tar.gz" || fm_die "extracting the photos failed"

# --- verify ----------------------------------------------------------------------------------------------
FAILED=0 WARN=$DEGRADED_BACKUP
# assert <description> <command...>: runs the test command; a failing test is
# recorded, never an abort, so one run reports every problem at once.
assert() {
  local description="$1"
  shift
  if "$@"; then fm_log "  ok    $description"; else fm_log "  FAIL  $description"; FAILED=1; fi
}
fm_log "verifying"

rev="$(psql_q "$TARGET_DB" "SELECT version_num FROM alembic_version")"
assert "migration revision is ${M[alembic_revision]} (restored: ${rev:-none})"   test "$rev" = "${M[alembic_revision]}"

for t in $FM_COUNT_TABLES; do
  got="$(psql_q "$TARGET_DB" "SELECT count(*) FROM $t")"
  assert "table $t has ${M[rows_$t]:-?} rows (restored: $got)" test "$got" = "${M[rows_$t]:-x}"
done

assert "the set of attendee ids matches the backup"   test "$(fm_sql_ids_digest "$TARGET_DB")" = "${M[users_ids_sha256]}"
assert "the set of referenced photo keys matches the backup"   test "$(fm_sql_photo_digest "$TARGET_DB")" = "${M[photo_keys_sha256]}"

# Every file of the archive, byte for byte.
media_files_match() {
  # sha256sum -c rejects an empty list. With no archived files there are
  # no per-file hashes to check; the exact file-count assertion below still
  # rejects any unexpected restored file.
  [ -s "$BACKUP/media.sha256" ] || return 0
  docker run --rm -i --network none --entrypoint sh -v "$MOUNT":/m:ro "$TOOL_IMAGE"     -c 'cd /m && sha256sum -c - >/dev/null 2>&1' < "$BACKUP/media.sha256"
}
assert "every restored photo file matches its recorded SHA-256" media_files_match
files="$(docker run --rm --network none --entrypoint sh -v "$MOUNT":/m:ro "$TOOL_IMAGE" -c 'find /m -type f | wc -l' | tr -d ' \r')"
expected_files="$(wc -l < "$BACKUP/media.sha256" | tr -d ' ')"
assert "the media store holds exactly the $expected_files archived files (found $files)"   test "$files" = "$expected_files"

# Do the restored rows and the restored files belong together?
refs="$(psql_q "$TARGET_DB" "SELECT photo_key FROM users WHERE photo_key IS NOT NULL ORDER BY photo_key COLLATE \"C\"" | tr -d '\r')"
have="$(docker run --rm --network none --entrypoint sh -v "$MOUNT":/m:ro "$TOOL_IMAGE" -c 'ls /m/avatars 2>/dev/null || true' | tr -d '\r')"
total=0 dangling=0
for key in $refs; do
  total=$((total + 1))
  for size in $FM_PHOTO_SIZES; do
    if ! printf '%s\n' "$have" | grep -qx "$key-$size.jpg"; then
      dangling=$((dangling + 1))
      break
    fi
  done
done
assert "photo rows with their files: $((total - dangling)) of $total (the backup itself recorded ${M[photos_missing_files]} without)" \
  test "$dangling" -le "${M[photos_missing_files]}"
[ "$dangling" -eq 0 ] || WARN=1

if [ "$FAILED" -ne 0 ]; then fm_die "verification FAILED - the restore is not trustworthy"; fi
OK=1
cat >&2 <<EOF

Restore verified.
  database : $TARGET_DB (revision ${M[alembic_revision]})
  photos   : $MEDIA_TARGET_LABEL (${M[media_files]:-?} files)
Nothing live was touched. To use this copy instead of the live data, follow
OPERATIONS.md, "Promoting a restore" (stack stopped, deliberate, by hand).
It is owned by the role "$RESTORE_ROLE" (not a superuser) it was restored as.
To discard it: DROP DATABASE "$TARGET_DB", DROP ROLE "$RESTORE_ROLE" and remove
the media target (restore-drill.sh does all three for you).
EOF
[ "$WARN" -eq 0 ] || { fm_warn "restored a degraded or legacy backup; consult its manifest for consistency and missing-photo warnings"; exit 3; }
exit 0
