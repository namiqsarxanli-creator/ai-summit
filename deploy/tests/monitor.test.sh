#!/usr/bin/env bash
# Offline test of deploy/monitoring/monitor.sh: how it turns container state,
# the public endpoints and `ops-check` output into one exit code. A stand-in
# `docker` and `curl` answer from environment variables; nothing is contacted.
#
#   bash deploy/tests/monitor.test.sh
# shellcheck disable=SC2319  # `check ... $?` deliberately reads the status of the test just before it
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0 fail=0
ok() { pass=$((pass + 1)); echo "  ok    $1"; }
bad() { fail=$((fail + 1)); echo "  FAIL  $1"; }

mkdir -p "$TMP/bin"
cat > "$TMP/bin/docker" <<'SHIM'
#!/usr/bin/env bash
args="$*"
case "$1" in
  compose)
    shift
    # find the subcommand after the (optional) --env-file/-f options
    while [ $# -gt 0 ]; do case "$1" in ps | exec) break ;; *) shift ;; esac; done
    sub="$1"; shift
    if [ "$sub" = ps ]; then
      service="${*: -1}"
      case " ${SHIM_DOWN:-} " in *" $service "*) exit 0 ;; esac
      echo "id-$service"; exit 0
    fi
    if [ "$sub" = exec ]; then
      case "$args" in
        *" backend "*) printf '%s\n' "${SHIM_BACKEND_OUT:-OK    database: reachable}"; exit "${SHIM_BACKEND_RC:-0}" ;;
        *" worker "*) printf '%s\n' "${SHIM_WORKER_OUT:-OK    worker: loop 1s ago}"; exit "${SHIM_WORKER_RC:-0}" ;;
      esac
    fi ;;
  inspect)
    id="${*: -1}"; service="${id#id-}"
    case "$args" in
      *State.Status*) echo running ;;
      *State.Health*) var="SHIM_HEALTH_$service"; echo "${!var:-healthy}" ;;
      *RestartCount*) var="SHIM_RESTARTS_$service"; echo "${!var:-0}" ;;
    esac
    exit 0 ;;
esac
echo "UNEXPECTED docker call: $args" >&2
exit 99
SHIM
cat > "$TMP/bin/curl" <<'SHIM'
#!/usr/bin/env bash
echo "curl $*" >> "$SHIM_CURL_LOG"
case " $* " in *" -K - "*) cat >> "$SHIM_CURL_LOG.stdin" ;; esac
case "$*" in
  *"/api/health/live"*) printf '%s' "${SHIM_LIVE_CODE:-200}" ;;
  *"/api/health/ready"*) printf '%s' "${SHIM_READY_CODE:-200}" ;;
esac
exit 0
SHIM
chmod +x "$TMP/bin/docker" "$TMP/bin/curl"
export SHIM_CURL_LOG="$TMP/curl.log"

# scenario <description> <expected exit> <expected text> [ENV=value ...] [-- monitor args]
scenario() {
  local description="$1" want="$2" text="$3"
  shift 3
  local envs=() args=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do envs+=("$1"); shift; done
  [ "${1:-}" = "--" ] && shift
  args=("$@")
  : > "$SHIM_CURL_LOG"
  OUT="$(env "${envs[@]}" PATH="$TMP/bin:$PATH" DC="docker compose" bash "$ROOT/deploy/monitoring/monitor.sh" "${args[@]}" 2>&1)"
  RC=$?
  if [ "$RC" -eq "$want" ] && [[ "$OUT" == *"$text"* ]]; then ok "$description"
  else bad "$description (exit $RC, wanted $want and \"$text\")"; echo "$OUT" | sed 's/^/          /' | head -12; fi
}

echo "monitor.sh"
scenario "everything healthy -> OK" 0 "OVERALL: OK" SHIM_X=1
scenario "a stopped worker container is critical" 2 "CRIT  container:worker: not running" SHIM_DOWN=worker
scenario "an unhealthy container is critical" 2 "container:backend: unhealthy" SHIM_HEALTH_backend=unhealthy
scenario "a container that keeps restarting is a warning" 1 "restarted 5 times" SHIM_RESTARTS_proxy=5
scenario "a starting container is a warning" 1 "still starting" SHIM_HEALTH_db=starting
scenario "the worker reporting a pause is a warning" 1 "WARN  ops:worker: paused" "SHIM_WORKER_OUT=WARN  worker: paused by provider rate limit for 30s" SHIM_WORKER_RC=1
scenario "a stalled worker loop is critical" 2 "CRIT  ops:worker: loop stalled" "SHIM_WORKER_OUT=CRIT  worker: loop stalled (300s since last turn)" SHIM_WORKER_RC=2
scenario "an outbox backlog is critical" 2 "CRIT  ops:outbox-backlog: oldest due=240s" "SHIM_BACKEND_OUT=CRIT  outbox-backlog: oldest due=240s" SHIM_BACKEND_RC=2
scenario "an ops-check that cannot run is critical" 2 "could not run ops-check" SHIM_BACKEND_RC=137
scenario "public live+ready 200 -> OK" 0 "public:ready: HTTP 200" -- --public https://summit.example.test/
scenario "live but not ready names the database" 2 "database is not answering" SHIM_READY_CODE=503 -- --public https://summit.example.test
scenario "not even live is critical" 2 "CRIT  public:live" SHIM_LIVE_CODE=502 SHIM_READY_CODE=502 -- --public https://summit.example.test
scenario "an unknown option is refused" 2 "unknown argument" -- --nope

# the dead-man's switch is requested only when everything is OK
scenario "PING_OK_URL is requested when all is OK" 0 "OVERALL: OK" PING_OK_URL=https://monitor.invalid/ping/abc
grep -q 'monitor.invalid/ping/abc' "$SHIM_CURL_LOG" && ok "...and the ping was sent" || bad "...the ping was NOT sent"
scenario "PING_OK_URL is NOT requested on a warning" 1 "OVERALL: WARNING" PING_OK_URL=https://monitor.invalid/ping/abc SHIM_HEALTH_db=starting
grep -q 'monitor.invalid' "$SHIM_CURL_LOG" && bad "...but it was pinged" || ok "...and no ping was sent"
scenario "PING_OK_URL is NOT requested on a critical" 2 "OVERALL: CRITICAL" PING_OK_URL=https://monitor.invalid/ping/abc SHIM_DOWN=worker
grep -q 'monitor.invalid' "$SHIM_CURL_LOG" && bad "...but it was pinged" || ok "...and no ping was sent"

# the operator's Telegram alert: on a change of result, never the token on argv
export ALERT_ENV_FILE="$TMP/none.env"
TG="ALERT_TELEGRAM_BOT_TOKEN=123:secret-token ALERT_TELEGRAM_CHAT_ID=42 ALERT_STATE_FILE=$TMP/alert.state"
sent() { cat "$SHIM_CURL_LOG.stdin" 2>/dev/null | grep -c 'api.telegram.org'; }
rm -f "$TMP/alert.state" "$SHIM_CURL_LOG.stdin"
# shellcheck disable=SC2086
scenario "first run OK: no alert" 0 "OVERALL: OK" $TG
[ "$(sent)" = 0 ] && ok "...nothing sent" || bad "...an alert was sent"
# shellcheck disable=SC2086
scenario "a stopped worker alerts" 2 "OVERALL: CRITICAL" $TG SHIM_DOWN=worker
[ "$(sent)" = 1 ] && ok "...one alert sent" || bad "...alerts sent: $(sent)"
grep -q 'secret-token' "$SHIM_CURL_LOG" && bad "...the token was on curl's command line" || ok "...the token never on argv"
grep -q 'container:worker' "$SHIM_CURL_LOG" && ok "...the message names the failing part" || bad "...the message does not name it"
# shellcheck disable=SC2086
scenario "the same problem again: no repeat within the hour" 2 "OVERALL: CRITICAL" $TG SHIM_DOWN=worker
[ "$(sent)" = 1 ] && ok "...still one alert" || bad "...alerts sent: $(sent)"
# shellcheck disable=SC2086
scenario "the same problem after the repeat interval: reminded" 2 "OVERALL: CRITICAL" $TG SHIM_DOWN=worker ALERT_REPEAT_SECONDS=0
[ "$(sent)" = 2 ] && ok "...reminded once" || bad "...alerts sent: $(sent)"
# shellcheck disable=SC2086
scenario "recovery is announced" 0 "OVERALL: OK" $TG
[ "$(sent)" = 3 ] && ok "...recovery sent" || bad "...alerts sent: $(sent)"
grep -q 'working again' "$SHIM_CURL_LOG" && ok "...as a recovery" || bad "...not as a recovery"
scenario "no token: nothing is sent" 2 "OVERALL: CRITICAL" SHIM_DOWN=worker "ALERT_STATE_FILE=$TMP/alert2.state"
[ "$(sent)" = 3 ] && ok "...nothing more sent" || bad "...alerts sent: $(sent)"

echo
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
