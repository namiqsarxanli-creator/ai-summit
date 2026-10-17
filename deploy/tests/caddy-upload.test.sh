#!/usr/bin/env bash
# The real Caddy proxy with deploy/Caddyfile, isolated: its own network, a stand-in
# upstream that reports how many body bytes reached it, and a client. Checks the
# per-route request-body limits: the two raw-image uploads get 9MB, every other
# route keeps 1MB, matched by exact method AND path. Needs Docker and the local
# images caddy:2-alpine and python:3.12-slim. Cleans up only what it created.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TAG="caddy-upload-$(date +%s)-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
NET="$TAG-net" UP="$TAG-up" CADDY="$TAG-caddy"
cleanup() {
  docker rm -f "$UP" "$CADDY" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker network create "$NET" >/dev/null

# The upstream: answers 200 with the number of body bytes it received.
docker run -d --name "$UP" --network "$NET" --network-alias upstream python:3.12-slim python -c '
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def _any(self):
        n = int(self.headers.get("Content-Length") or 0)
        got = len(self.rfile.read(n)) if n else 0
        body = str(got).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    do_POST = do_PUT = do_GET = _any
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(("0.0.0.0", 9000), H).serve_forever()
' >/dev/null

docker create --name "$CADDY" --network "$NET" --network-alias proxy \
  -e DOMAIN=":8080" -e ACME_EMAIL="test@example.invalid" -e CADDY_GLOBAL_OPTIONS="auto_https off" \
  -e BACKEND_UPSTREAM="upstream:9000" -e FRONTEND_UPSTREAM="upstream:9000" \
  caddy:2-alpine caddy run --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
docker cp "$ROOT/deploy/Caddyfile" "$CADDY:/etc/caddy/Caddyfile"
docker start "$CADDY" >/dev/null

docker run --rm --network "$NET" python:3.12-slim python -c '
import http.client, sys, time
for _ in range(100):
    try:
        c = http.client.HTTPConnection("proxy", 8080, timeout=5); c.request("GET", "/"); c.getresponse().read(); break
    except OSError:
        time.sleep(0.1)
MB = 1024 * 1024
cases = [
    ("POST", "/api/attendees/me/pair-photos?partner_id=1", 5 * MB, 200),
    ("POST", "/api/attendees/me/pair-photos?partner_id=1", 10 * MB, 413),
    ("PUT", "/api/attendees/me/photo", 5 * MB, 200),
    ("PUT", "/api/attendees/me/photo", 10 * MB, 413),
    ("PUT", "/api/attendees/me/pair-photos", 2 * MB, 413),   # method is part of the match
    ("POST", "/api/attendees/me/pair-photos/5/answer", 2 * MB, 413),  # exact path
    ("POST", "/api/attendees/me/profile", 2 * MB, 413),     # generic API: 1MB
    ("POST", "/api/attendees/me/profile", 512 * 1024, 200),
    ("POST", "/some/page", 2 * MB, 413),                   # frontend: 1MB
]
failed = 0
for method, path, size, want in cases:
    c = http.client.HTTPConnection("proxy", 8080, timeout=30)
    try:
        c.request(method, path, body=b"\0" * size, headers={"Content-Type": "application/octet-stream"})
        r = c.getresponse(); body = r.read(); status = r.status
    except (BrokenPipeError, ConnectionResetError):
        status = 413  # the proxy stopped reading a body over its limit
        body = b""
    ok = status == want and (want != 200 or int(body or 0) == size)
    failed += not ok
    print(("PASS" if ok else "FAIL"), method, path, size, "->", status, body[:20])
sys.exit(1 if failed else 0)
'
echo "PASS: Caddy body limits (9MB for both photo uploads, 1MB elsewhere)"
