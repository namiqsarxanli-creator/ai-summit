#!/usr/bin/env bash
# Post-deployment smoke test against the PUBLIC URL (through the real proxy
# chain - ARCHITECTURE.md 17.9 says localhost does not count).
#
#   deploy/smoke.sh https://your-domain
#
# Optional, to also prove the realtime stream is not buffered (needs a
# logged-in ATTENDEE session cookie value, taken from a test account's
# browser; it is read from the environment and never printed):
#
#   SMOKE_ATTENDEE_SESSION=<cookie value> deploy/smoke.sh https://your-domain
#
# Extra curl options (e.g. --resolve for a test host, -k for a local CA)
# can be passed through SMOKE_CURL_OPTS.
set -u

BASE="${1:-}"
if [ -z "$BASE" ]; then echo "usage: $0 https://your-domain"; exit 2; fi
BASE="${BASE%/}"
# shellcheck disable=SC2206
CURL=(curl -s --max-time 20 ${SMOKE_CURL_OPTS:-})
fail=0
ok() { echo "  ok    $1"; }
bad() { echo "  FAIL  $1"; fail=1; }
check() { # description, condition (0 = pass)
  if [ "$2" -eq 0 ]; then ok "$1"; else bad "$1"; fi
}

hdr() { "${CURL[@]}" -D - -o /dev/null "$@" | tr -d '\r'; }

echo "Smoke test: $BASE"

body="$("${CURL[@]}" -w ' %{http_code}' "$BASE/api/health")"
[ "$body" = '{"status":"ok"} 200' ]; check "GET /api/health through the proxy" $?

h="$(hdr "$BASE/")"
echo "$h" | head -1 | grep -q ' 200'; check "GET / is served" $?
echo "$h" | grep -qi '^strict-transport-security:'; check "HSTS header present" $?
echo "$h" | grep -qi '^x-content-type-options: nosniff'; check "nosniff header present" $?
echo "$h" | grep -qi '^content-security-policy:'; check "Content-Security-Policy present" $?
echo "$h" | grep -qi "unsafe-eval" && bad "CSP must not allow unsafe-eval" || ok "CSP has no unsafe-eval"
echo "$h" | grep -qi '^server: *caddy' && bad "Server header should be hidden" || ok "Server header hidden"

h="$(hdr "$BASE/c/smoke-test-token")"
echo "$h" | grep -qi '^referrer-policy: no-referrer'; check "card page keeps Referrer-Policy: no-referrer" $?

code="$("${CURL[@]}" -o /dev/null -w '%{http_code}' -X POST -H 'Origin: https://evil.invalid' \
  -H 'Content-Type: application/json' --data '{}' "$BASE/api/staff/auth/login")"
[ "$code" = "403" ]; check "foreign Origin is rejected (CSRF), got $code" $?

code="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$BASE/api/realtime/stream")"
[ "$code" = "401" ]; check "SSE stream refuses an anonymous client, got $code" $?

# The plain-HTTP address is derived from BASE only when BASE has no explicit
# port (a custom HTTPS port says nothing about the HTTP one); otherwise pass
# SMOKE_HTTP_URL=http://host:port explicitly.
http_url="${SMOKE_HTTP_URL:-}"
if [ -z "$http_url" ]; then
  hostpart="${BASE#https://}"
  case "$hostpart" in *:*) ;; *) http_url="http://$hostpart" ;; esac
fi
if [ -n "$http_url" ]; then
  loc="$(hdr "$http_url/" | grep -i '^location:')"
  echo "$loc" | grep -qi '^location: https://'; check "http:// redirects to https://" $?
else
  echo "  skip  http->https redirect (custom port; set SMOKE_HTTP_URL to include it)"
fi

if [ -n "${SMOKE_ATTENDEE_SESSION:-}" ]; then
  echo "  ...   reading the realtime stream for 35 s (expects 'ready' at once, a heartbeat every ~15 s)"
  out="$(mktemp)"
  "${CURL[@]}" -N --max-time 35 -H "Cookie: session=${SMOKE_ATTENDEE_SESSION}" \
    "$BASE/api/realtime/stream" > "$out" 2>/dev/null
  grep -q '^event: ready' "$out"; check "stream sent 'ready'" $?
  beats="$(grep -c '^event: heartbeat' "$out")"
  [ "$beats" -ge 2 ]; check "stream delivered $beats heartbeats (>=2 means no buffering)" $?
  rm -f "$out"
else
  echo "  skip  authenticated SSE check (set SMOKE_ATTENDEE_SESSION to include it)"
fi

if [ "$fail" -eq 0 ]; then echo "Smoke test passed."; else echo "Smoke test FAILED."; fi
exit "$fail"
