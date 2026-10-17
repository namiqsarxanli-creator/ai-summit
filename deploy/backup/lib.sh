#!/usr/bin/env bash
# Shared helpers for the backup / restore scripts. Sourced, never run directly.
# shellcheck shell=bash
# shellcheck disable=SC2034  # variables here are read by the scripts that source this file
#
# Everything the scripts need from the outside arrives as environment variables
# (never as arguments that end up in a process list) and is validated here.
# Nothing in this file prints a secret: the scripts never read .env.production
# themselves - they ask `docker compose` for the db container and run
# pg_dump / pg_restore INSIDE it over the local socket, which needs no password.

# Git Bash on Windows rewrites arguments that look like POSIX paths (/m, /var/...);
# harmless everywhere else.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

# Every file the scripts create is private to the user that runs them.
umask 077

FM_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FM_REPO_ROOT="$(cd "$FM_LIB_DIR/../.." && pwd)"

# Photo renditions the application writes (backend/app/core/images.py: SIZES).
# tests/test_backup_scripts.py fails if the two ever drift apart.
FM_PHOTO_SIZES="128 384"
# The advisory lock that serialises photo-file deletion against a backup: the same
# number as backend/app/core/media_guard.py (tests/test_media_guard.py compares them).
FM_MEDIA_LOCK_KEY=419078677
# Tables whose row counts go into the manifest and are compared after a restore.
# Fixed names (they are interpolated into SQL), no personal data in any of them
# that the manifest would repeat - only counts and digests of ids / photo keys.
FM_COUNT_TABLES="users booths points_log feedback contact_cards staff_accounts"

fm_log() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
fm_die() { fm_log "ERROR: $*"; exit 1; }
fm_warn() { fm_log "WARNING: $*"; }

fm_need() { command -v "$1" >/dev/null 2>&1 || fm_die "required command not found: $1"; }

# --- the docker context -------------------------------------------------------

# Sets: DB_CONTAINER DB_USER DB_NAME LIVE_DB TOOL_IMAGE
# DB_NAME is the database to back up (default: the container's POSTGRES_DB);
# LIVE_DB is always the container's own POSTGRES_DB - the one a restore may
# never be aimed at.
fm_resolve_db() {
  fm_need docker
  : "${COMPOSE_ENV_FILE:=$FM_REPO_ROOT/.env.production}"
  : "${COMPOSE_FILE:=$FM_REPO_ROOT/docker-compose.prod.yml}"
  if [ -z "${DB_CONTAINER:-}" ]; then
    DB_CONTAINER="$(docker compose --env-file "$COMPOSE_ENV_FILE" -f "$COMPOSE_FILE" ps -q db 2>/dev/null | head -n1)"
  fi
  [ -n "$DB_CONTAINER" ] || fm_die "no database container: start the stack, or set DB_CONTAINER=<container name>"
  [ "$(docker inspect -f '{{.State.Running}}' "$DB_CONTAINER" 2>/dev/null)" = "true" ] \
    || fm_die "the database container is not running"
  DB_USER="${DB_USER:-$(docker exec "$DB_CONTAINER" printenv POSTGRES_USER)}"
  LIVE_DB="$(docker exec "$DB_CONTAINER" printenv POSTGRES_DB)"
  DB_NAME="${DB_NAME:-$LIVE_DB}"
  [ -n "$DB_USER" ] && [ -n "$DB_NAME" ] || fm_die "could not determine the database user/name"
  TOOL_IMAGE="${TOOL_IMAGE:-$(docker inspect -f '{{.Config.Image}}' "$DB_CONTAINER")}"
  fm_ident "$DB_NAME" "DB_NAME"
}

# A PostgreSQL identifier the scripts are willing to interpolate into a command.
fm_ident() {
  [[ "$1" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || fm_die "${2:-name} must be lowercase letters, digits and _ (got an unusable value)"
}

pg() { docker exec -i "$DB_CONTAINER" "$@"; }
# Quiet, unaligned, tuples-only, stops at the first error. $1 database, $2 SQL.
psql_q() { pg psql -U "$DB_USER" -d "$1" -X -A -t -q -v ON_ERROR_STOP=1 -c "$2"; }

# The media source for BACKUPS: a named volume (default) or a host directory.
# Sets MEDIA_MOUNT (the -v argument's left side) and MEDIA_LABEL (for messages).
fm_resolve_media_source() {
  if [ -n "${MEDIA_DIR:-}" ] && [ -n "${MEDIA_VOLUME:-}" ]; then
    fm_die "set MEDIA_VOLUME or MEDIA_DIR, not both"
  fi
  if [ -n "${MEDIA_DIR:-}" ]; then
    case "$MEDIA_DIR" in /*) ;; *) fm_die "MEDIA_DIR must be an absolute path" ;; esac
    [ -d "$MEDIA_DIR" ] || fm_die "MEDIA_DIR does not exist"
    MEDIA_MOUNT="$MEDIA_DIR"
    MEDIA_LABEL="directory $MEDIA_DIR"
  else
    MEDIA_VOLUME="${MEDIA_VOLUME:-facemark-prod_media_data}"
    docker volume inspect "$MEDIA_VOLUME" >/dev/null 2>&1 \
      || fm_die "media volume not found: $MEDIA_VOLUME (set MEDIA_VOLUME or MEDIA_DIR)"
    MEDIA_MOUNT="$MEDIA_VOLUME"
    MEDIA_LABEL="volume $MEDIA_VOLUME"
  fi
}

# Runs a command in a throw-away container of the db image (busybox tar/sha256sum),
# no network, with the media source mounted read-only at /m. stdin is passed through.
fm_media_ro() {
  docker run --rm -i --network none --entrypoint sh -v "$MEDIA_MOUNT":/m:ro "$TOOL_IMAGE" -c "$@"
}

# --- reading a dump without a database ------------------------------------------------
# A custom-format dump is itself the best witness of what was backed up, so the
# manifest is derived from it rather than from the live database a moment
# before or after (which keeps changing while the backup runs).

# copy_rows <dump> <table>: the table's data rows as `COPY` text (header + rows).
fm_copy_rows() { pg pg_restore -a -t "$2" -f - < "$1"; }

# copy_column <dump> <table> <column>: that column's values, one per line,
# NULLs skipped. Reads the column order from the COPY header, so it keeps
# working when columns are added.
fm_copy_column() {
  fm_copy_rows "$1" "$2" | awk -v want="$3" '
    /^COPY / && !inrows {
      head = $0; sub(/^[^(]*\(/, "", head); sub(/\).*$/, "", head)
      n = split(head, cols, /, /); idx = 0
      for (i = 1; i <= n; i++) { c = cols[i]; gsub(/"/, "", c); if (c == want) idx = i }
      inrows = 1; next
    }
    inrows && $0 == "\\." { inrows = 0; next }
    inrows && idx > 0 { split($0, f, "\t"); if (f[idx] != "\\N" && f[idx] != "") print f[idx] }
  '
}

# Pair photos (migration 0025): their files share the media volume with profile
# photos. A row's verified state is: id, status, photo key (or \N), whether each
# of the two people gave screen consent (t/f), moderation. Timestamps are left out
# on purpose: their text depends on the session time zone, the flags do not.
# Prints one tab-separated line per row, by id. Nothing when the table is absent
# (a backup of a database older than 0025).
fm_pair_state_from_dump() {
  fm_copy_rows "$1" pair_photos | awk '
    /^COPY / && !inrows {
      head = $0; sub(/^[^(]*\(/, "", head); sub(/\).*$/, "", head)
      n = split(head, cols, /, /)
      for (i = 1; i <= n; i++) { c = cols[i]; gsub(/"/, "", c); at[c] = i }
      inrows = 1; next
    }
    inrows && $0 == "\\." { inrows = 0; next }
    inrows {
      split($0, f, "\t")
      u = (f[at["uploader_screen_at"]] == "\\N") ? "f" : "t"
      p = (f[at["partner_screen_at"]] == "\\N") ? "f" : "t"
      printf "%s\t%s\t%s\t%s\t%s\t%s\n", f[at["id"]], f[at["status"]], f[at["photo_key"]], u, p, f[at["moderation"]]
    }
  ' | LC_ALL=C sort -t "$(printf '\t')" -k1,1n
}

# The same from a restored database ($1); nothing when the table does not exist.
fm_sql_has_pair_photos() { [ "$(psql_q "$1" "SELECT to_regclass('public.pair_photos') IS NOT NULL")" = t ]; }
fm_sql_pair_state() {
  fm_sql_has_pair_photos "$1" || return 0
  psql_q "$1" "SELECT concat_ws(E'\t', id, status, coalesce(photo_key, E'\\\\N'), CASE WHEN uploader_screen_at IS NULL THEN 'f' ELSE 't' END, CASE WHEN partner_screen_at IS NULL THEN 'f' ELSE 't' END, moderation) FROM pair_photos ORDER BY id"
}
fm_sql_pair_keys() {
  fm_sql_has_pair_photos "$1" || return 0
  psql_q "$1" "SELECT photo_key FROM pair_photos WHERE photo_key IS NOT NULL ORDER BY photo_key COLLATE \"C\""
}

fm_copy_count() { fm_copy_rows "$1" "$2" | awk '/^COPY / && !inrows {inrows=1; next} inrows && $0=="\\." {inrows=0; next} inrows {n++} END {print n+0}'; }

fm_sha256_of_text() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }

# The same digests the manifest holds, recomputed from a RESTORED database - so
# they are compared like with like. $1 database.
fm_sql_ids_digest() {
  psql_q "$1" "SELECT encode(sha256(convert_to(coalesce(string_agg(id::text, ',' ORDER BY id), ''), 'UTF8')), 'hex') FROM users"
}
fm_sql_photo_digest() {
  psql_q "$1" "SELECT encode(sha256(convert_to(coalesce(string_agg(photo_key, ',' ORDER BY photo_key COLLATE \"C\"), ''), 'UTF8')), 'hex') FROM users WHERE photo_key IS NOT NULL"
}

# --- manifest ------------------------------------------------------------------------------

# Reads KEY=VALUE lines WITHOUT sourcing the file, into the associative array M.
fm_read_manifest() {
  declare -gA M=()
  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in '' | '#'*) continue ;; esac
    key="${line%%=*}"
    value="${line#*=}"
    [[ "$key" =~ ^[a-z][a-z0-9_]*$ ]] || fm_die "manifest has a malformed key"
    [[ "$value" =~ ^[A-Za-z0-9._:@+/-]*$ ]] || fm_die "manifest value for $key has unexpected characters"
    M["$key"]="$value"
  done < "$1"
}

# Backup directory names are the only thing retention and restore will act on.
FM_BACKUP_NAME_RE='^facemark-[0-9]{8}T[0-9]{6}Z$'

# A backup directory carries exactly one marker, written last:
#   COMPLETE  fully recoverable: every photo the database references is in the
#             archive, and the media lock was held from before the dump until
#             the archive was finished. Only these count as "known good".
#   DEGRADED  everything was captured and checksummed, but a referenced photo is
#             missing or the lock could not be confirmed. Kept for diagnosis and
#             for a deliberate restore (`restore.sh --accept-degraded`); it is
#             NEVER counted toward KEEP_MIN and never lets retention touch a
#             known-good backup.
# A directory with neither marker is an interrupted run and is ignored.
fm_list_with_marker() { # directory marker
  local d
  for d in "$1"/facemark-*; do
    [ -d "$d" ] && [ ! -L "$d" ] || continue
    [[ "$(basename "$d")" =~ $FM_BACKUP_NAME_RE ]] || continue
    [ -f "$d/$2" ] || continue
    if [ "$2" = COMPLETE ]; then
      [ ! -e "$d/DEGRADED" ] || continue
      # Legacy backups did not guarantee consistency and may contain missing photos.
      grep -qx 'format=2' "$d/manifest.env" 2>/dev/null || continue
      grep -qx 'status=complete' "$d/manifest.env" || continue
      grep -qx 'media_lock=held' "$d/manifest.env" || continue
      grep -qx 'photos_missing_files=0' "$d/manifest.env" || continue
    fi
    printf '%s\n' "$d"
  done | sort
}
# Complete (known-good) backups under $1, oldest first.
fm_list_backups() { fm_list_with_marker "$1" COMPLETE; }
# Degraded backups under $1, oldest first.
fm_list_degraded() { fm_list_with_marker "$1" DEGRADED; }

# Removes one backup directory, after re-checking that it really is one.
fm_remove_backup() { # root directory
  local root="$1" d="$2"
  [[ "$d" == "$root"/facemark-* ]] || return 1
  [[ "$(basename "$d")" =~ $FM_BACKUP_NAME_RE ]] || return 1
  [ -d "$d" ] && [ ! -L "$d" ] || return 1
  [ -f "$d/COMPLETE" ] || [ -f "$d/DEGRADED" ] || return 1
  rm -rf "$d"
}

# Retention, run only after a backup succeeded. $1 root, $2 RETENTION_DAYS,
# $3 KEEP_MIN, $4 DEGRADED_RETENTION_DAYS.
#
#  * Known-good backups: the newest KEEP_MIN are always kept; older ones go once
#    they are more than RETENTION_DAYS old. Degraded backups are not in this
#    list at all, so no number of them can push a good backup out.
#  * Degraded backups are kept for diagnosis. While at least one known-good
#    backup exists they go after DEGRADED_RETENTION_DAYS. While NONE exists they
#    are the only data there is: the newest KEEP_MIN stay whatever their age, and
#    older ones go only after RETENTION_DAYS.
fm_prune() {
  local root="$1" days="$2" keep="$3" ddays="$4" i n protected threshold
  local -a good bad
  mapfile -t good < <(fm_list_backups "$root")
  mapfile -t bad < <(fm_list_degraded "$root")
  [ -n "${good[0]:-}" ] || good=()
  [ -n "${bad[0]:-}" ] || bad=()

  n=$((${#good[@]} - keep))
  for ((i = 0; i < n; i++)); do
    if [ -n "$(find "${good[$i]}" -maxdepth 0 -mtime +"$days" 2>/dev/null)" ]; then
      fm_log "retention: removing $(basename "${good[$i]}") (known good, older than $days days)"
      fm_remove_backup "$root" "${good[$i]}"
    fi
  done

  if [ "${#good[@]}" -gt 0 ]; then
    protected=0
    threshold="$ddays"
  else
    protected="$keep"
    threshold="$days"
    [ "${#bad[@]}" -eq 0 ] || fm_warn "no known-good backup exists - keeping the newest $keep degraded one(s)"
  fi
  n=$((${#bad[@]} - protected))
  for ((i = 0; i < n; i++)); do
    if [ -n "$(find "${bad[$i]}" -maxdepth 0 -mtime +"$threshold" 2>/dev/null)" ]; then
      fm_log "retention: removing $(basename "${bad[$i]}") (degraded, older than $threshold days)"
      fm_remove_backup "$root" "${bad[$i]}"
    fi
  done
}
