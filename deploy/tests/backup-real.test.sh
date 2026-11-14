#!/usr/bin/env bash
# Integration exercise: its own PostgreSQL container, media volume and network.
# Requires Docker, bash, flock and coreutils. Never uses Compose or existing DBs.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TAG="codex-backup-$(date +%s)-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
APP_IMAGE="${APP_IMAGE:-facemark-backend}"
DB="$TAG-db" MEDIA="$TAG-media" NET="$TAG-net" APP="$TAG-app"
TMP="$(mktemp -d)"
cleanup() {
  docker rm -f "$DB" >/dev/null 2>&1 || true
  docker rm -f "$APP" >/dev/null 2>&1 || true
  docker volume rm "$MEDIA" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT
docker network create "$NET" >/dev/null
docker volume create "$MEDIA" >/dev/null
docker run -d --name "$DB" --network "$NET" -e POSTGRES_USER=test \
  -e POSTGRES_PASSWORD=temporary-test-only -e POSTGRES_DB=scratch postgres:16-alpine >/dev/null
ready=0
for ((i=0; i<60; i++)); do
  # The image starts a temporary socket-only server during initialization.
  # TCP readiness avoids racing its shutdown before the final server starts.
  if docker exec "$DB" pg_isready -h 127.0.0.1 -U test -d scratch >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
[ "$ready" -eq 1 ] || { echo 'Temporary PostgreSQL failed to start'; exit 1; }
docker exec -i "$DB" psql -U test -d scratch -v ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE alembic_version(version_num text);
INSERT INTO alembic_version VALUES ('0014_profile_photo');
CREATE TABLE users(id int, photo_key text);
CREATE TABLE booths(id int);
CREATE TABLE points_log(id int);
CREATE TABLE feedback(id int);
CREATE TABLE contact_cards(id int);
CREATE TABLE staff_accounts(id int);
SQL
export DB_CONTAINER="$DB" MEDIA_VOLUME="$MEDIA" BACKUP_DIR="$TMP/backups"
export MIN_FREE_MB=0 MEDIA_LOCK_WAIT=5 MEDIA_LOCK_MAX_HOLD=60
# First deployment has neither attendees nor photos. An empty media archive
# must be a complete backup, not a grep/pipefail error.
bash "$ROOT/deploy/backup/backup.sh"
empty="$(find "$BACKUP_DIR" -name COMPLETE -print)"
[ -n "$empty" ]
grep -qx 'media_files=0' "${empty%/COMPLETE}/manifest.env"
grep -qx 'rows_users=0' "${empty%/COMPLETE}/manifest.env"
bash "$ROOT/deploy/backup/restore-drill.sh" --backup "${empty%/COMPLETE}"
echo 'PASS: first deployment with no attendees or photos backs up and restores'
docker exec "$DB" psql -U test -d scratch -c "INSERT INTO users VALUES (1, repeat('a',32)), (2, repeat('b',32))" >/dev/null
sleep 1
docker run --rm --network none -v "$MEDIA":/m postgres:16-alpine sh -c '
  mkdir /m/avatars
  for key in aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; do
    for size in 128 384; do echo fixture > "/m/avatars/$key-$size.jpg"; done
  done'
export DB_CONTAINER="$DB" MEDIA_VOLUME="$MEDIA" BACKUP_DIR="$TMP/backups"
export MIN_FREE_MB=0 MEDIA_LOCK_WAIT=5 MEDIA_LOCK_MAX_HOLD=60
# Replacement and removal exactly between dump and archive, through the real
# Python media guard against THIS temporary database and THIS media volume.
docker create --name "$APP" --network "$NET" -v "$MEDIA":/media \
  -e DATABASE_URL="postgresql+psycopg://test:temporary-test-only@$DB:5432/scratch" \
  -e ENVIRONMENT=development -e APP_ORIGIN=http://localhost:3000 \
  -e SCAN_REF_SECRET=temporary-backup-test-secret-0123456789 \
  --entrypoint python "$APP_IMAGE" -c 'from sqlalchemy import text; from app.db.session import SessionLocal; from app.core.media import MediaStore; from app.core.media_guard import delete_photo_files; from pathlib import Path; s=SessionLocal(); s.execute(text("UPDATE users SET photo_key=NULL")); s.commit(); s.close(); store=MediaStore(Path("/media")); assert not delete_photo_files(store, "a"*32); assert not delete_photo_files(store, "b"*32)' >/dev/null
# docker cp also works when this exercise runs inside a Linux runner container:
# paths visible in the runner are not necessarily host paths the daemon can bind.
docker cp "$ROOT/backend/app/." "$APP:/app/app"
export BACKUP_AFTER_DUMP_HOOK="docker start -a '$APP'"
bash "$ROOT/deploy/backup/backup.sh"
good="$(find "$BACKUP_DIR" -name COMPLETE -print | sort | tail -n1)"
[ -n "$good" ]
grep -qx 'photos_missing_files=0' "${good%/COMPLETE}/manifest.env"
unset BACKUP_AFTER_DUMP_HOOK
bash "$ROOT/deploy/backup/restore-drill.sh" --backup "${good%/COMPLETE}"
echo 'PASS: replacement/removal between dump and archive preserve snapshot files; restore verified'
# A pre-existing missing photo is degraded and cannot restore silently.
docker exec "$DB" psql -U test -d scratch -c "UPDATE users SET photo_key=repeat('c',32) WHERE id=1" >/dev/null
sleep 1
rc=0; bash "$ROOT/deploy/backup/backup.sh" || rc=$?
[ "$rc" -eq 4 ]
bad="$(find "$BACKUP_DIR" -name DEGRADED -print)"
[ -n "$bad" ]
rc=0; bash "$ROOT/deploy/backup/restore-drill.sh" --backup "${bad%/DEGRADED}" || rc=$?
[ "$rc" -eq 1 ]
rc=0; bash "$ROOT/deploy/backup/restore-drill.sh" --accept-degraded --backup "${bad%/DEGRADED}" || rc=$?
[ "$rc" -eq 3 ]
echo 'PASS: missing files produce DEGRADED; explicit restore returns warning'
docker exec "$DB" psql -U test -d scratch -c 'UPDATE users SET photo_key=NULL' >/dev/null
sleep 1
export MEDIA_LOCK_MAX_HOLD=1 BACKUP_AFTER_DUMP_HOOK='sleep 2'
rc=0; bash "$ROOT/deploy/backup/backup.sh" || rc=$?
[ "$rc" -eq 4 ]
lost="$(find "$BACKUP_DIR" -name DEGRADED -print | sort | tail -n1)"
grep -qx 'photos_missing_files=0' "${lost%/DEGRADED}/manifest.env"
grep -qx 'media_lock=lost' "${lost%/DEGRADED}/manifest.env"
rc=0; bash "$ROOT/deploy/backup/restore-drill.sh" --accept-degraded --backup "${lost%/DEGRADED}" || rc=$?
[ "$rc" -eq 3 ]
echo 'PASS: lost lock is degraded even with no missing files; restore still warns'

# --- pair photos (migration 0025): their files are referenced too ---------------------
unset BACKUP_AFTER_DUMP_HOOK
export MEDIA_LOCK_MAX_HOLD=60
docker exec -i "$DB" psql -U test -d scratch -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
CREATE TABLE pair_photos(id int, status text, photo_key text, uploader_screen_at timestamptz,
  partner_screen_at timestamptz, moderation text);
-- accepted, both consented, approved | withdrawn (no key) | pending, uploader consent only
INSERT INTO pair_photos VALUES
  (1, 'ACCEPTED', repeat('d', 32), now(), now(), 'APPROVED'),
  (2, 'WITHDRAWN', NULL, NULL, NULL, 'PENDING'),
  (3, 'PENDING', repeat('e', 32), now(), NULL, 'PENDING');
SQL
docker run --rm --network none -v "$MEDIA":/m postgres:16-alpine sh -c '
  for key in dddddddddddddddddddddddddddddddd eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee ffffffffffffffffffffffffffffffff; do
    for size in 128 384; do echo pair > "/m/avatars/$key-$size.jpg"; done
  done'
sleep 1
bash "$ROOT/deploy/backup/backup.sh"
pairs="$(find "$BACKUP_DIR" -name COMPLETE -print | sort | tail -n1)"
[ -n "$pairs" ]
m="${pairs%/COMPLETE}/manifest.env"
grep -qx 'pair_photo_rows=3' "$m"
grep -qx 'pair_photos_with_file=2' "$m"
grep -qx 'photos_missing_files=0' "$m"
grep -q '^pair_photos_state_sha256=[0-9a-f]\{64\}$' "$m"
# The unreferenced key (f...) is only COUNTED as an orphan; nothing deletes it.
docker run --rm --network none -v "$MEDIA":/m:ro postgres:16-alpine test -f /m/avatars/ffffffffffffffffffffffffffffffff-384.jpg
bash "$ROOT/deploy/backup/restore-drill.sh" --backup "${pairs%/COMPLETE}"
echo 'PASS: pair photos, their consents, withdrawal and files back up and restore verified'
# A referenced pair-photo file that is missing makes the backup DEGRADED.
docker run --rm --network none -v "$MEDIA":/m postgres:16-alpine rm /m/avatars/dddddddddddddddddddddddddddddddd-384.jpg
sleep 1
rc=0; bash "$ROOT/deploy/backup/backup.sh" || rc=$?
[ "$rc" -eq 4 ]
missing_pair="$(find "$BACKUP_DIR" -name DEGRADED -print | sort | tail -n1)"
grep -qx 'dddddddddddddddddddddddddddddddd' "${missing_pair%/DEGRADED}/missing-photos.txt"
echo 'PASS: a missing pair-photo file is detected (DEGRADED), not ignored'
