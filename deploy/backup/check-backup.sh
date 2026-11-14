#!/usr/bin/env bash
# Is there a recent, complete, intact backup? For cron / an uptime monitor.
#
#   BACKUP_DIR=/srv/facemark-backups deploy/backup/check-backup.sh
#
# Exit 0 ok | 1 warning (newest backup older than MAX_AGE_HOURS, default 26)
# | 2 critical (none, older than twice that, or fails its SHA256SUMS).
# VERIFY=1 also checks every file against SHA256SUMS (reads the whole backup).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/backup/lib.sh
. "$here/lib.sh"

[ -n "${BACKUP_DIR:-}" ] || { echo "CRIT backup: BACKUP_DIR is not set"; exit 2; }
MAX_AGE_HOURS="${MAX_AGE_HOURS:-26}"
[[ "$MAX_AGE_HOURS" =~ ^[1-9][0-9]*$ ]] || { echo "CRIT backup: MAX_AGE_HOURS must be positive"; exit 2; }
newest="$(fm_list_backups "$BACKUP_DIR" | tail -n1)"
[ -n "$newest" ] || { echo "CRIT backup: no complete backup in BACKUP_DIR"; exit 2; }

age_minutes="$(( ($(date +%s) - $(stat -c %Y "$newest/COMPLETE")) / 60 ))"
if [ "${VERIFY:-0}" = "1" ] && ! (cd "$newest" && sha256sum -c SHA256SUMS >/dev/null 2>&1); then
  echo "CRIT backup: $(basename "$newest") does not match its SHA256SUMS"
  exit 2
fi
limit=$((MAX_AGE_HOURS * 60))
if [ "$age_minutes" -gt $((limit * 2)) ]; then
  echo "CRIT backup: newest is $((age_minutes / 60)) h old ($(basename "$newest"))"; exit 2
elif [ "$age_minutes" -gt "$limit" ]; then
  echo "WARN backup: newest is $((age_minutes / 60)) h old ($(basename "$newest"))"; exit 1
fi
degraded="$(fm_list_degraded "$BACKUP_DIR" | tail -n1)"
if [ -n "$degraded" ] && [[ "$degraded" > "$newest" ]]; then
  echo "WARN backup: newest attempt is degraded ($(basename "$degraded")); last known-good is $(basename "$newest")"
  exit 1
fi
echo "OK backup: newest is $((age_minutes / 60)) h old ($(basename "$newest"))"
