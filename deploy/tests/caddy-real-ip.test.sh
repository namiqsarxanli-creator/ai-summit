#!/usr/bin/env bash
# The real Caddy proxy with deploy/Caddyfile, isolated: its own network with fixed
# addresses, a stand-in upstream that echoes the forwarding headers it received,
# and two clients. One client's address is added to the test copy's
# `trusted_proxies` and so plays a Cloudflare edge; the other connects directly.
# Checks that every upstream gets exactly one X-Forwarded-For: the
# CF-Connecting-IP value only from the "Cloudflare" peer, the connecting address
# otherwise - so a forged header never becomes the client address - and that
# CF-Connecting-IP / X-Real-IP / Forwarded never reach an upstream. Needs Docker and the local
# images caddy:2-alpine and python:3.12-slim. Cleans up only what it created.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TAG="caddy-real-ip-$(date +%s)-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
NET="$TAG-net" UP="$TAG-up" CADDY="$TAG-caddy"
SUBNET_PREFIX="172.31.251"
EDGE="$SUBNET_PREFIX.20"    # treated as a Cloudflare edge in the test copy
DIRECT="$SUBNET_PREFIX.30"  # an ordinary client that bypasses Cloudflare
cleanup() {
  docker rm -f "$UP" "$CADDY" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -f "${CONF:-}"
}
trap cleanup EXIT
docker network create --subnet "$SUBNET_PREFIX.0/24" "$NET" >/dev/null

# The upstream: answers with the forwarding headers it received, as JSON.
docker run -d --name "$UP" --network "$NET" --network-alias upstream python:3.12-slim python -c '
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def _any(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        seen = {k: self.headers.get(k) for k in ("X-Forwarded-For", "X-Real-IP", "Forwarded", "CF-Connecting-IP")}
        body = json.dumps(seen).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    do_GET = do_POST = do_PUT = _any
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(("0.0.0.0", 9000), H).serve_forever()
' >/dev/null

# The test copy differs from deploy/Caddyfile in ONE place: the edge address is
# prepended to the real Cloudflare ranges.
CONF="$(mktemp)"
grep -q "trusted_proxies static " "$ROOT/deploy/Caddyfile"
sed "s#trusted_proxies static #trusted_proxies static $EDGE/32 #" "$ROOT/deploy/Caddyfile" > "$CONF"
docker create --name "$CADDY" --network "$NET" --ip "$SUBNET_PREFIX.10" --network-alias proxy \
  -e DOMAIN=":8080" -e ACME_EMAIL="test@example.invalid" -e CADDY_GLOBAL_OPTIONS="auto_https off" \
  -e BACKEND_UPSTREAM="upstream:9000" -e FRONTEND_UPSTREAM="upstream:9000" \
  caddy:2-alpine caddy run --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
SRC="$CONF"
command -v cygpath >/dev/null 2>&1 && SRC="$(cygpath -w "$CONF")"  # Git Bash on Windows
docker cp "$SRC" "$CADDY:/etc/caddy/Caddyfile"
docker start "$CADDY" >/dev/null

client() {  # client <ip> <cases as python literal>
  docker run --rm --network "$NET" --ip "$1" python:3.12-slim python -c '
import http.client, json, sys, time
for _ in range(100):
    try:
        c = http.client.HTTPConnection("proxy", 8080, timeout=5); c.request("GET", "/"); c.getresponse().read(); break
    except OSError:
        time.sleep(0.1)
failed = 0
for name, method, path, headers, want in eval(sys.argv[1]):
    c = http.client.HTTPConnection("proxy", 8080, timeout=10)
    c.request(method, path, body=b"x" if method != "GET" else None, headers=headers)
    seen = json.loads(c.getresponse().read())
    ok = seen["X-Forwarded-For"] == want and all(seen[h] is None for h in ("X-Real-IP", "Forwarded", "CF-Connecting-IP"))
    failed += not ok
    print(("PASS" if ok else "FAIL"), name, "->", seen)
sys.exit(1 if failed else 0)
' "$2"
}

FORGED='{"X-Forwarded-For": "1.2.3.4, 6.6.6.6", "CF-Connecting-IP": "5.6.7.8", "X-Real-IP": "9.9.9.9", "Forwarded": "for=8.8.8.8"}'

echo "direct client (not Cloudflare): forged headers are ignored"
client "$DIRECT" "[
  ('api, forged CF-Connecting-IP and chain', 'GET', '/api/leaderboard', $FORGED, '$DIRECT'),
  ('frontend, forged headers', 'GET', '/', $FORGED, '$DIRECT'),
  ('photo upload route', 'PUT', '/api/attendees/me/photo', $FORGED, '$DIRECT'),
  ('realtime stream route', 'GET', '/api/realtime/stream', $FORGED, '$DIRECT'),
]"

echo "Cloudflare edge: the attendee address comes from CF-Connecting-IP only"
client "$EDGE" "[
  ('api', 'GET', '/api/leaderboard', {'CF-Connecting-IP': '203.0.113.7', 'X-Forwarded-For': '1.2.3.4, 6.6.6.6', 'X-Real-IP': '9.9.9.9'}, '203.0.113.7'),
  ('frontend', 'GET', '/', {'CF-Connecting-IP': '198.51.100.9', 'Forwarded': 'for=8.8.8.8'}, '198.51.100.9'),
  ('pair photo upload route', 'POST', '/api/attendees/me/pair-photos?partner_id=1', {'CF-Connecting-IP': '203.0.113.8'}, '203.0.113.8'),
  ('IPv6 attendee', 'GET', '/api/leaderboard', {'CF-Connecting-IP': '2001:db8::7'}, '2001:db8::7'),
  ('no CF-Connecting-IP: the edge itself', 'GET', '/api/leaderboard', {'X-Forwarded-For': '1.2.3.4'}, '$EDGE'),
  ('not an address: the edge itself', 'GET', '/api/leaderboard', {'CF-Connecting-IP': 'not-an-ip'}, '$EDGE'),
]"
echo "PASS: Caddy forwards exactly one client address; forged headers never become it"
