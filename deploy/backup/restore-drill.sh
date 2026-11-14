#!/usr/bin/env bash
# Restore drill: restore a backup into throw-away targets, run every check of
# restore.sh, then remove exactly what the drill created.
#
#   BACKUP_DIR=/srv/facemark-backups deploy/backup/restore-drill.sh
#   deploy/backup/restore-drill.sh --backup /srv/facemark-backups/facemark-20261004T031500Z
#
# A backup nobody has restored is a hope, not a backup: run this after setting
# backups up and then regularly (monthly, and before every release).
# It never touches the live database or the live media volume; the names it
# uses are generated here (facemark_restore_drill_<time>_<random>,
# facemark-restore-drill-<time>-<random>) and only those are removed.
#
# KEEP=1 leaves the restored copy in place for you to inspect (and prints how
# to remove it). Exit codes are restore.sh's: 0 verified, 1 failed, 3 warnings.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/backup/lib.sh
. "$here/lib.sh"

BACKUP=""
DEGRADED_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --accept-degraded) DEGRADED_ARGS=(--accept-degraded); shift ;;
    --backup) BACKUP="${2:-}"; shift 2 ;;
    *) fm_die "unknown argument: $1" ;;
  esac
done
if [ -z "$BACKUP" ]; then
  [ -n "${BACKUP_DIR:-}" ] || fm_die "give --backup <dir> or set BACKUP_DIR"
  BACKUP="$(fm_list_backups "$BACKUP_DIR" | tail -n1)"
  [ -n "$BACKUP" ] || fm_die "no complete backup found in BACKUP_DIR"
  fm_log "using the newest complete backup: $(basename "$BACKUP")"
fi

fm_resolve_db
random="$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
stamp="$(date -u +%Y%m%d%H%M%S)"
DRILL_DB="facemark_restore_drill_${stamp}_${random}"
DRILL_VOLUME="facemark-restore-drill-${stamp}-${random}"

remove_drill_targets() {
  # The names were built above from fixed parts; re-check before any DROP.
  [[ "$DRILL_DB" =~ ^facemark_restore_drill_[0-9]{14}_[0-9a-f]{8}$ ]] || return 0
  [[ "$DRILL_VOLUME" =~ ^facemark-restore-drill-[0-9]{14}-[0-9a-f]{8}$ ]] || return 0
  [ "$DRILL_DB" != "$LIVE_DB" ] || return 0
  psql_q postgres "DROP DATABASE IF EXISTS \"$DRILL_DB\" WITH (FORCE)" >/dev/null 2>&1 || true
  # restore.sh restores as a role named like the database (never a superuser).
  psql_q postgres "DROP ROLE IF EXISTS \"$DRILL_DB\"" >/dev/null 2>&1 || true
  docker volume rm "$DRILL_VOLUME" >/dev/null 2>&1 || true
}
if [ "${KEEP:-0}" != "1" ]; then trap remove_drill_targets EXIT; fi

status=0
bash "$here/restore.sh" "${DEGRADED_ARGS[@]}" --backup "$BACKUP" --target-db "$DRILL_DB" --target-media-volume "$DRILL_VOLUME" || status=$?

if [ "${KEEP:-0}" = "1" ]; then
  fm_log "KEEP=1: left database $DRILL_DB and volume $DRILL_VOLUME in place. Remove them with:"
  fm_log "  docker exec $DB_CONTAINER psql -U $DB_USER -d postgres -c 'DROP DATABASE \"$DRILL_DB\"'"
  fm_log "  docker exec $DB_CONTAINER psql -U $DB_USER -d postgres -c 'DROP ROLE \"$DRILL_DB\"'"
  fm_log "  docker volume rm $DRILL_VOLUME"
fi
case "$status" in
  0) fm_log "RESTORE DRILL PASSED" ;;
  3) fm_log "RESTORE DRILL PASSED WITH WARNINGS (see above)" ;;
  *) fm_log "RESTORE DRILL FAILED" ;;
esac
exit "$status"
