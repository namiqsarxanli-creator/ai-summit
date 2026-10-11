#!/usr/bin/env bash
# One-shot health report for the production stack - run it by hand, from cron,
# or from any uptime tool that can run a command and read an exit code.
#
#   deploy/monitoring/monitor.sh [--public https://your-domain]
#
# Exit code = the worst result: 0 ok | 1 warning | 2 critical. One line per
# check on stdout; nothing sensitive is printed (counts, ages, reason codes).
#
# Environment (all optional):
#   DC            how to call Compose; default
#                 "docker compose --env-file .env.production -f docker-compose.prod.yml"
#   BACKUP_DIR    also run deploy/backup/check-backup.sh against it
#   PING_OK_URL   a dead-man's-switch URL of YOUR monitoring service, requested
#                 ONLY when the overall result is OK (so silence raises the alert).
#                 Nothing is created or contacted unless you set it.
#
# What each check proves, and what to do about a failure: OPERATIONS.md.
set -u
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
cd "$root" || exit 2
DC="${DC:-docker compose --env-file .env.production -f docker-compose.prod.yml}"

PUBLIC=""
while [ $# -gt 0 ]; do
  case "$1" in
    --public) PUBLIC="${2:-}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

worst=0
note() { # level(0|1|2) name detail
  local label=OK
  [ "$1" -eq 1 ] && label=WARN
  [ "$1" -ge 2 ] && label=CRIT
  printf '%-5s %s: %s\n' "$label" "$2" "$3"
  [ "$1" -gt "$worst" ] && worst="$1"
}

# 1. containers: running, and healthy where the service defines a health check.
for service in db backend frontend worker proxy; do
  # shellcheck disable=SC2086
  id="$($DC ps -q "$service" 2>/dev/null | head -n1)"
  if [ -z "$id" ]; then note 2 "container:$service" "not running"; continue; fi
  state="$(docker inspect -f '{{.State.Status}}' "$id" 2>/dev/null)"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id" 2>/dev/null)"
  restarts="$(docker inspect -f '{{.RestartCount}}' "$id" 2>/dev/null)"
  if [ "$state" != "running" ]; then note 2 "container:$service" "state is $state"
  elif [ "$health" = "unhealthy" ]; then note 2 "container:$service" "unhealthy"
  elif [ "$health" = "starting" ]; then note 1 "container:$service" "still starting"
  elif [ "${restarts:-0}" -ge 3 ]; then note 1 "container:$service" "healthy but restarted $restarts times"
  else note 0 "container:$service" "$state${health:+, health $health}"; fi
done

# 2. the public endpoints, through the real proxy (liveness and readiness differ).
if [ -n "$PUBLIC" ]; then
  PUBLIC="${PUBLIC%/}"
  live="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$PUBLIC/api/health/live" 2>/dev/null)"
  ready="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$PUBLIC/api/health/ready" 2>/dev/null)"
  if [ "$live" != "200" ]; then note 2 "public:live" "HTTP ${live:-no answer} - nothing is serving"
  else note 0 "public:live" "HTTP 200"; fi
  if [ "$ready" = "200" ]; then note 0 "public:ready" "HTTP 200"
  elif [ "$live" = "200" ]; then note 2 "public:ready" "HTTP ${ready:-no answer} - the process is up but the database is not answering"
  else note 2 "public:ready" "HTTP ${ready:-no answer}"; fi
fi

# 3-4. database / migrations / OTP outbox / media (backend) and the worker's own loop.
run_ops() { # name, then the command; ops-check's exit code is 0/1/2
  local name="$1" out rc
  shift
  out="$("$@" 2>&1)"
  rc=$?
  case "$rc" in 0 | 1 | 2) ;; *) note 2 "$name" "could not run ops-check (exit $rc)"; return ;; esac
  local line level rest
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    level="${line%% *}"
    rest="${line#"$level"}"
    rest="${rest#"${rest%%[![:space:]]*}"}" # drop the column padding
    case "$level" in
      CRIT) note 2 "ops:${rest%%:*}" "${rest#*: }" ;;
      WARN) note 1 "ops:${rest%%:*}" "${rest#*: }" ;;
      *) note 0 "ops:${rest%%:*}" "${rest#*: }" ;;
    esac
  done <<< "$out"
}
# shellcheck disable=SC2086
run_ops "backend" $DC exec -T backend python -m app.cli ops-check --media
# shellcheck disable=SC2086
run_ops "worker" $DC exec -T worker python -m app.cli ops-check --no-db --worker

# 5. backups.
if [ -n "${BACKUP_DIR:-}" ]; then
  out="$(bash "$root/deploy/backup/check-backup.sh" 2>&1)"
  rc=$?
  case "$rc" in 0) note 0 "backup" "${out#OK backup: }" ;; 1) note 1 "backup" "${out#WARN backup: }" ;; *) note 2 "backup" "${out#CRIT backup: }" ;; esac
fi

case "$worst" in 0) echo "OVERALL: OK" ;; 1) echo "OVERALL: WARNING" ;; *) echo "OVERALL: CRITICAL" ;; esac
if [ "$worst" -eq 0 ] && [ -n "${PING_OK_URL:-}" ]; then
  curl -fsS -m 10 "$PING_OK_URL" >/dev/null 2>&1 || echo "WARN  dead-man's switch: could not reach PING_OK_URL" >&2
fi
exit "$worst"
