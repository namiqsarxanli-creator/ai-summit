#!/usr/bin/env bash
# EXAMPLE off-server hook. Copy it, edit it, point OFFSITE_HOOK at the copy.
# It is called by backup.sh as: offsite.sh /srv/facemark-backups/facemark-<timestamp> complete|degraded
# and must exit non-zero if the copy failed (backup.sh then exits 3).
#
# Nothing here runs by itself and no credentials are stored in the repository.
# Whatever you use, the copy must be ENCRYPTED before or while it leaves the
# server (the dump holds attendee e-mail addresses and phone numbers), and the
# key that decrypts it must NOT be stored next to the backups it protects.
# shellcheck disable=SC2034  # `name` is used by the options you uncomment
set -euo pipefail
dir="${1:?backup directory}"
name="$(basename "$dir")"

# --- Option A: rclone with a "crypt" remote (client-side encryption) ---------------
#   rclone config        # once: create a remote, then a crypt remote on top of it
#   rclone copy "$dir" "facemark-crypt:backups/$name" --immutable
#
# --- Option B: restic (encrypted, deduplicated, has its own retention) -------------
#   export RESTIC_REPOSITORY=... RESTIC_PASSWORD_FILE=/root/.facemark-restic-pass   # chmod 600
#   restic backup "$dir" --tag facemark
#   restic forget --tag facemark --keep-daily 14 --keep-monthly 6 --prune
#
# --- Option C: rsync over SSH to a host you control (encrypt the disk there) -------
#   rsync -a --partial -e "ssh -i /root/.ssh/facemark_backup" "$dir" backup@host:/srv/facemark/
echo "offsite.example.sh is an example; edit it before use" >&2
exit 1
