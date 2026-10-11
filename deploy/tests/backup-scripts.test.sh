#!/usr/bin/env bash
# Offline tests of the backup / restore scripts. They need bash and coreutils
# only - no Docker, no database: a stand-in `docker` answers the handful of
# read-only questions the scripts ask before they would change anything, and
# logs every call, so each test can prove that a refused restore never reached
# a command that creates, drops or writes.
#
#   bash deploy/tests/backup-scripts.test.sh
#
# The real thing (pg_dump, pg_restore, volumes) is exercised by
# deploy/backup/restore-drill.sh against a running database; see DEPLOYMENT.md.
# shellcheck disable=SC2319  # `check ... $?` deliberately reads the status of the test just before it
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0 fail=0
ok() { pass=$((pass + 1)); echo "  ok    $1"; }
bad() { fail=$((fail + 1)); echo "  FAIL  $1"; }
check() { if [ "$2" -eq 0 ]; then ok "$1"; else bad "$1"; fi; }

echo "static checks"
for f in "$ROOT"/deploy/*.sh "$ROOT"/deploy/backup/*.sh "$ROOT"/deploy/tests/*.sh; do
  bash -n "$f" 2>/dev/null; check "syntax: ${f#"$ROOT"/}" $?
done

sizes_in_app="$(grep -E '^(SMALL|LARGE) = [0-9]+' "$ROOT/backend/app/core/images.py" | sed -E 's/.* = ([0-9]+).*/\1/' | tr '\n' ' ' | sed 's/ $//')"
sizes_in_scripts="$(grep -E '^FM_PHOTO_SIZES=' "$ROOT/deploy/backup/lib.sh" | sed -E 's/.*"([0-9 ]+)".*/\1/')"
[ -n "$sizes_in_app" ] && [ "$sizes_in_app" = "$sizes_in_scripts" ]
check "photo sizes in the scripts ($sizes_in_scripts) match app/core/images.py ($sizes_in_app)" $?

missing=""
for t in $(grep -E '^FM_COUNT_TABLES=' "$ROOT/deploy/backup/lib.sh" | sed -E 's/.*"([a-z_ ]+)".*/\1/'); do
  grep -rqE "__tablename__ = \"$t\"" "$ROOT/backend/app" || missing="$missing $t"
done
[ -z "$missing" ]; check "every table the manifest counts exists in the models${missing:+ (missing:$missing)}" $?

# The scripts must never read the environment file or print secrets.
! grep -nE '(^|[^A-Za-z_])(source|\.) +[^ ]*\.env|cat +[^ ]*\.env|printenv' "$ROOT"/deploy/backup/*.sh | grep -vE 'printenv POSTGRES_(USER|DB)\)?"?$|printenv POSTGRES_(USER|DB)\)' >/dev/null
check "no script sources an env file or prints environment values" $?
! grep -nE 'set -x|bash -x' "$ROOT"/deploy/backup/*.sh >/dev/null
check "no script traces its commands (set -x would print every value)" $?

echo "restore.sh refuses before it changes anything"
# --- the stand-in docker --------------------------------------------------------------------
mkdir -p "$TMP/bin"
cat > "$TMP/bin/docker" <<'SHIM'
#!/usr/bin/env bash
echo "docker $*" >> "$SHIM_LOG"
args="$*"
case "$1" in
  compose) echo "fakecontainer"; exit 0 ;;
  inspect)
    case "$args" in *State.Running*) echo true ;; *Config.Image*) echo postgres:16-alpine ;; esac
    exit 0 ;;
  volume)
    if [ "$2" = "inspect" ]; then
      for v in ${SHIM_VOLUMES:-}; do [ "$v" = "$3" ] && exit 0; done
      exit 1
    fi
    echo "UNEXPECTED volume command" >&2; exit 99 ;;
  exec)
    case "$args" in
      *"printenv POSTGRES_USER"*) echo facemark; exit 0 ;;
      *"printenv POSTGRES_DB"*) echo "${SHIM_LIVE_DB:-facemark}"; exit 0 ;;
      *"FROM pg_database WHERE datname"*)
        for d in ${SHIM_DBS:-}; do case "$args" in *"'$d'"*) echo 1 ;; esac; done
        exit 0 ;;
    esac ;;
esac
echo "UNEXPECTED docker call: $1" >&2
exit 99
SHIM
chmod +x "$TMP/bin/docker"
export SHIM_LOG="$TMP/docker.log"

# --- a structurally valid backup ----------------------------------------------------------
BK="$TMP/backups/facemark-20261004T031500Z"
mkdir -p "$BK"
printf 'not a real dump' > "$BK/db.dump"
printf 'x' > "$BK/media.tar.gz"
: > "$BK/media.sha256"
cat > "$BK/manifest.env" <<'M'
format=1
created_utc=20261004T031500Z
source_db=facemark
alembic_revision=0014_profile_photo
rows_users=1
users_ids_sha256=00
users_with_photo=0
photo_keys_sha256=00
photos_missing_files=0
media_files=0
M
: > "$BK/COMPLETE"
(cd "$BK" && sha256sum db.dump media.tar.gz media.sha256 manifest.env > SHA256SUMS)

# Runs restore.sh with the stand-in docker; sets OUT and RC; clears the call log first.
run_restore() {
  : > "$SHIM_LOG"
  OUT="$(PATH="$TMP/bin:$PATH" bash "$ROOT/deploy/backup/restore.sh" "$@" 2>&1)"
  RC=$?
}
# refused <description> <expected message part> <restore args...>
refused() {
  local description="$1" part="$2"
  shift 2
  run_restore "$@"
  local changed=""
  grep -qE 'CREATE DATABASE|DROP DATABASE|volume (create|rm)|pg_restore|docker run' "$SHIM_LOG" && changed=" - BUT IT REACHED A CHANGING COMMAND"
  if [ "$RC" -eq 1 ] && [[ "$OUT" == *"$part"* ]] && [ -z "$changed" ]; then
    ok "$description"
  else
    bad "$description (exit $RC, wanted 1 and \"$part\"$changed)"
    echo "$OUT" | tail -3 | sed 's/^/          /'
  fi
}

VOLARG=(--target-media-volume facemark-restore-new)
refused "the live database name is refused" "must look like facemark_restore_" --backup "$BK" --target-db facemark "${VOLARG[@]}"
refused "postgres is refused" "must look like facemark_restore_" --backup "$BK" --target-db postgres "${VOLARG[@]}"
refused "a name outside the restore pattern is refused" "must look like facemark_restore_" --backup "$BK" --target-db prod_copy "${VOLARG[@]}"
refused "SQL metacharacters in the name are refused" "must look like facemark_restore_" --backup "$BK" --target-db 'facemark_restore_x"; DROP DATABASE facemark; --' "${VOLARG[@]}"
refused "uppercase / punctuation is refused" "must look like facemark_restore_" --backup "$BK" --target-db 'facemark_restore_X-1' "${VOLARG[@]}"

SHIM_LIVE_DB=facemark_restore_oops refused "even a restore-pattern name is refused when it IS the live database" "that is the live database" \
  --backup "$BK" --target-db facemark_restore_oops "${VOLARG[@]}"
export SHIM_LIVE_DB=facemark

SHIM_DBS="facemark_restore_taken" refused "a database that already exists is never overwritten" "already exists" \
  --backup "$BK" --target-db facemark_restore_taken "${VOLARG[@]}"

refused "the source database of the backup is refused" "the database the backup came from" \
  --backup "$(
    cp -r "$BK" "$TMP/src-same" 2>/dev/null
    sed -i 's/^source_db=.*/source_db=facemark_restore_same/' "$TMP/src-same/manifest.env"
    (cd "$TMP/src-same" && sha256sum db.dump media.tar.gz media.sha256 manifest.env > SHA256SUMS)
    mkdir -p "$TMP/backups2" && rm -rf "$TMP/backups2/facemark-20261004T031600Z" && mv "$TMP/src-same" "$TMP/backups2/facemark-20261004T031600Z"
    echo "$TMP/backups2/facemark-20261004T031600Z"
  )" --target-db facemark_restore_same "${VOLARG[@]}"

SHIM_VOLUMES="facemark-restore-new" refused "a media volume that already exists is never reused" "already exists" \
  --backup "$BK" --target-db facemark_restore_fresh "${VOLARG[@]}"
refused "the live media volume name is refused" "must look like facemark-restore-" \
  --backup "$BK" --target-db facemark_restore_fresh --target-media-volume facemark-prod_media_data
refused "a media directory outside facemark-restore-* is refused" "directory named facemark-restore-" \
  --backup "$BK" --target-db facemark_restore_fresh --target-media-dir /srv/facemark/media
refused "a media directory with .. is refused" "must not contain .." \
  --backup "$BK" --target-db facemark_restore_fresh --target-media-dir "$TMP/facemark-restore-a/../b"
refused "a relative media directory is refused" "absolute path" \
  --backup "$BK" --target-db facemark_restore_fresh --target-media-dir facemark-restore-rel
mkdir -p "$TMP/facemark-restore-full" && echo data > "$TMP/facemark-restore-full/file"
refused "a non-empty media directory is never written into" "not empty" \
  --backup "$BK" --target-db facemark_restore_fresh --target-media-dir "$TMP/facemark-restore-full"
refused "both a volume and a directory is refused" "OR" \
  --backup "$BK" --target-db facemark_restore_fresh "${VOLARG[@]}" --target-media-dir "$TMP/facemark-restore-x"
refused "no media target is refused" "media target is required" --backup "$BK" --target-db facemark_restore_fresh

cp -r "$BK" "$TMP/backups/facemark-20261004T031700Z"
printf 'tampered' >> "$TMP/backups/facemark-20261004T031700Z/media.tar.gz"
refused "a backup whose files do not match SHA256SUMS is refused" "SHA256SUMS" \
  --backup "$TMP/backups/facemark-20261004T031700Z" --target-db facemark_restore_fresh "${VOLARG[@]}"
cp -r "$BK" "$TMP/backups/facemark-20261004T031800Z" && rm "$TMP/backups/facemark-20261004T031800Z/COMPLETE"
refused "a backup without the COMPLETE marker is refused" "COMPLETE" \
  --backup "$TMP/backups/facemark-20261004T031800Z" --target-db facemark_restore_fresh "${VOLARG[@]}"
refused "a directory that is not named like a backup is refused" "facemark-<timestamp>" \
  --backup "$TMP" --target-db facemark_restore_fresh "${VOLARG[@]}"
ln -s "$BK" "$TMP/facemark-20261004T031900Z" 2>/dev/null && [ -L "$TMP/facemark-20261004T031900Z" ] \
  && refused "a symlink is not accepted as a backup" "not a backup directory" \
    --backup "$TMP/facemark-20261004T031900Z" --target-db facemark_restore_fresh "${VOLARG[@]}"
refused "an unknown option is refused" "unknown argument" --backup "$BK" --target-db facemark_restore_fresh "${VOLARG[@]}" --force
refused "restoring into the live database cannot be forced" "unknown argument" --backup "$BK" --target-db facemark --overwrite

echo "backup.sh refuses unsafe input before it touches anything"
run_backup() {
  : > "$SHIM_LOG"
  OUT="$(PATH="$TMP/bin:$PATH" bash "$ROOT/deploy/backup/backup.sh" 2>&1)"
  RC=$?
}
BACKUP_DIR="" run_backup; [ "$RC" -eq 1 ] && [[ "$OUT" == *"BACKUP_DIR is required"* ]]; check "BACKUP_DIR is required" $?
BACKUP_DIR="relative/path" run_backup; [ "$RC" -eq 1 ] && [[ "$OUT" == *"absolute path"* ]]; check "a relative BACKUP_DIR is refused" $?
BACKUP_DIR="$TMP/b" KEEP_MIN=0 run_backup; [ "$RC" -eq 1 ] && [[ "$OUT" == *"KEEP_MIN"* ]]; check "KEEP_MIN=0 (would allow deleting every backup) is refused" $?
BACKUP_DIR="$TMP/b" RETENTION_DAYS=abc run_backup; [ "$RC" -eq 1 ] && [[ "$OUT" == *"RETENTION_DAYS"* ]]; check "a non-numeric RETENTION_DAYS is refused" $?
BACKUP_DIR="$TMP/b" DB_NAME='x; drop database facemark' run_backup; [ "$RC" -eq 1 ] && [[ "$OUT" == *"DB_NAME"* ]]; check "an unsafe DB_NAME is refused" $?
BACKUP_DIR="$TMP/b" MEDIA_DIR="$TMP" MEDIA_VOLUME=v run_backup; [ "$RC" -eq 1 ] && [[ "$OUT" == *"not both"* ]]; check "MEDIA_DIR together with MEDIA_VOLUME is refused" $?
BACKUP_DIR="$TMP/b" MEDIA_VOLUME=facemark-nonexistent run_backup; [ "$RC" -eq 1 ] && [[ "$OUT" == *"media volume not found"* ]]; check "a missing media volume stops the backup before the dump" $?
! grep -q 'pg_dump' "$SHIM_LOG"; check "...and no pg_dump was started" $?

echo "retention only ever removes complete, well-named backups"
mkdir -p "$TMP/ret/facemark-20260101T000000Z" "$TMP/ret/facemark-20260102T000000Z" "$TMP/ret/not-a-backup" "$TMP/ret/facemark-20260103T000000Z"
: > "$TMP/ret/facemark-20260101T000000Z/COMPLETE"
: > "$TMP/ret/facemark-20260103T000000Z/COMPLETE"
for d in "$TMP/ret/facemark-20260101T000000Z" "$TMP/ret/facemark-20260103T000000Z"; do
  printf 'format=2\nstatus=complete\nmedia_lock=held\nphotos_missing_files=0\n' > "$d/manifest.env"
done
listed="$(bash -c ". '$ROOT/deploy/backup/lib.sh'; fm_list_backups '$TMP/ret'" | xargs -n1 basename | tr '\n' ' ')"
[ "$listed" = "facemark-20260101T000000Z facemark-20260103T000000Z " ]
check "only complete, correctly named directories are listed (the unfinished one and the stranger are not)" $?

echo "degraded backups and retention"
mkdir -p "$TMP/ret/facemark-20260104T000000Z"
: > "$TMP/ret/facemark-20260104T000000Z/DEGRADED"
touch -d '40 days ago' "$TMP/ret"/facemark-*
bash -c '. "$1/deploy/backup/lib.sh"; fm_prune "$2" 14 2 7' _ "$ROOT" "$TMP/ret"
[ -d "$TMP/ret/facemark-20260101T000000Z" ] && [ -d "$TMP/ret/facemark-20260103T000000Z" ]
check "degraded backups never displace KEEP_MIN known-good backups" $?
[ ! -d "$TMP/ret/facemark-20260104T000000Z" ]; check "old degraded backup is pruned when good backups exist" $?
mkdir -p "$TMP/bad"/facemark-2026010{1,2,3}T000000Z
for d in "$TMP/bad"/facemark-*; do : > "$d/DEGRADED"; touch -d '40 days ago' "$d"; done
bash -c '. "$1/deploy/backup/lib.sh"; fm_prune "$2" 14 2 7' _ "$ROOT" "$TMP/bad"
[ -d "$TMP/bad/facemark-20260102T000000Z" ] && [ -d "$TMP/bad/facemark-20260103T000000Z" ]
check "without known-good backups newest degraded copies are preserved" $?
BACKUP_DIR="$TMP/bad" bash "$ROOT/deploy/backup/check-backup.sh" > "$TMP/freshness" 2>&1
[ "$?" -eq 2 ]; check "degraded-only storage is critical" $?
mkdir -p "$TMP/ret/facemark-20260105T000000Z"
: > "$TMP/ret/facemark-20260105T000000Z/DEGRADED"
BACKUP_DIR="$TMP/ret" bash "$ROOT/deploy/backup/check-backup.sh" > "$TMP/freshness" 2>&1
[ "$?" -eq 1 ]; check "new degraded attempt cannot be reported healthy" $?

# A real competing process owns the kernel lock; no fake flock or PID file.
echo "concurrent backup exclusion"
mkdir -p "$TMP/locked"
mkfifo "$TMP/locked/release"
flock --exclusive "$TMP/locked/.lock" bash -c 'touch "$1/ready"; read -r _ < "$1/release"' _ "$TMP/locked" &
holder=$!
for ((i=0; i<100; i++)); do [ ! -f "$TMP/locked/ready" ] || break; sleep .02; done
BACKUP_DIR="$TMP/locked" run_backup
[ "$RC" -eq 1 ] && [[ "$OUT" == *"already running"* ]] && [ ! -s "$SHIM_LOG" ]
check "second backup refuses before any Docker call" $?
echo 'done' > "$TMP/locked/release"
wait "$holder"
flock --nonblock "$TMP/locked/.lock" true
check "kernel lock is released after owner exits" $?
rm "$TMP/locked/ready"
bash -c 'exec 9>"$1/.lock"; flock -n 9; touch "$1/ready"; exec sleep 300' _ "$TMP/locked" &
holder=$!
for ((i=0; i<100; i++)); do [ ! -f "$TMP/locked/ready" ] || break; sleep .02; done
flock --nonblock "$TMP/locked/.lock" true
[ "$?" -ne 0 ]; check "separate process really owns the exclusive lock" $?
kill -KILL "$holder"
wait "$holder" 2>/dev/null || true
flock --nonblock "$TMP/locked/.lock" true
check "kernel releases the lock even after SIGKILL" $?

echo
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
