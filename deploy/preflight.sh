#!/usr/bin/env bash
# Pre-deployment check of the production env file. Prints variable NAMES and
# what is wrong - never a value. Exit 0 = nothing blocking found.
#
#   deploy/preflight.sh [.env.production]
set -u

ENV_FILE="${1:-.env.production}"
COMPOSE_FILE="$(dirname "$0")/../docker-compose.prod.yml"
fail=0

problem() { echo "  FAIL  $1"; fail=1; }
ok() { echo "  ok    $1"; }

if [ ! -f "$ENV_FILE" ]; then
  echo "env file not found: $ENV_FILE"
  exit 1
fi

# Read KEY=VALUE lines without executing the file.
declare -A V
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  case "$line" in ''|'#'*) continue ;; esac
  key="${line%%=*}"
  value="${line#*=}"
  [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && V["$key"]="$value"
done < "$ENV_FILE"

echo "Checking $ENV_FILE"

for name in DOMAIN APP_ORIGIN ACME_EMAIL POSTGRES_PASSWORD SCAN_REF_SECRET RESEND_API_KEY OTP_FROM_EMAIL OTP_OUTBOX_ENCRYPTION_KEY; do
  value="${V[$name]:-}"
  if [ -z "$value" ]; then
    problem "$name is not set"
  elif [[ "$value" == *REPLACE_ME* ]]; then
    problem "$name still contains the REPLACE_ME placeholder"
  else
    ok "$name is set"
  fi
done

origin="${V[APP_ORIGIN]:-}"
domain="${V[DOMAIN]:-}"
if [ -n "$origin" ] && [ -n "$domain" ]; then
  [ "$origin" = "https://$domain" ] && ok "APP_ORIGIN matches DOMAIN" \
    || problem "APP_ORIGIN must be exactly https://<DOMAIN> (no port, path or slash)"
fi
case "$domain" in
  localhost|*.localhost|*.local|*.test|*.invalid|*.example|example.*|*.example.*|'')
    problem "DOMAIN is not a real public host name" ;;
esac

pw="${V[POSTGRES_PASSWORD]:-}"
if [ -n "$pw" ] && [[ "$pw" != *REPLACE_ME* ]]; then
  [ "${#pw}" -ge 24 ] || problem "POSTGRES_PASSWORD should be at least 24 characters"
  [[ "$pw" =~ ^[A-Za-z0-9._~-]+$ ]] || problem "POSTGRES_PASSWORD may only use letters, digits and . _ ~ - (it goes inside a URL)"
fi
sec="${V[SCAN_REF_SECRET]:-}"
if [ -n "$sec" ] && [[ "$sec" != *REPLACE_ME* ]]; then
  [ "${#sec}" -ge 32 ] || problem "SCAN_REF_SECRET must be at least 32 characters"
  [[ "$sec" == *dev_only* ]] && problem "SCAN_REF_SECRET looks like the development value"
fi

key="${V[OTP_OUTBOX_ENCRYPTION_KEY]:-}"
if [ -n "$key" ] && [[ "$key" != *REPLACE_ME* ]]; then
  # URL-safe base64 of exactly 32 bytes is 43 characters plus one "=".
  [[ "$key" =~ ^[A-Za-z0-9_-]{43}=?$ ]] || problem "OTP_OUTBOX_ENCRYPTION_KEY is not a generated key (expected 32 bytes, URL-safe base64)"
  [ "$key" != "${V[SCAN_REF_SECRET]:-}" ] || problem "OTP_OUTBOX_ENCRYPTION_KEY must not equal SCAN_REF_SECRET"
fi

for forbidden in ENVIRONMENT DEMO_MODE; do
  [ -z "${V[$forbidden]:-}" ] || problem "$forbidden must not be set here (fixed by docker-compose.prod.yml)"
done

# Permissions (only meaningful on Linux/macOS).
if perm="$(stat -c '%a' "$ENV_FILE" 2>/dev/null)"; then
  case "$perm" in
    600|400) ok "file permissions are $perm" ;;
    *) problem "file permissions are $perm - run: chmod 600 $ENV_FILE" ;;
  esac
fi

# Compose itself must accept the file (this catches missing required vars).
if command -v docker >/dev/null 2>&1; then
  if docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --quiet 2>/dev/null; then
    ok "docker compose config is valid"
  else
    problem "docker compose config failed (run it without --quiet to see which variable)"
  fi
fi

if [ "$fail" -eq 0 ]; then echo "Preflight passed."; else echo "Preflight FAILED."; fi
exit "$fail"
