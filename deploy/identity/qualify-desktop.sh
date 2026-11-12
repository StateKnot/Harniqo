#!/usr/bin/env bash
set -euo pipefail
umask 077

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$repo"
: "${HARNIQO_TEST_DISCOVERY_FILE:?Run this script through qualify-keycloak.sh}"
: "${HARNIQO_TEST_CERT:?Missing the Keycloak test certificate}"
: "${HARNIQO_TEST_KEY_FILE:?Missing the Keycloak test key}"
: "${HARNIQO_TEST_CA:?Missing the Keycloak test CA}"
: "${HARNIQO_TEST_ISSUER:?Missing the Keycloak test issuer}"
test "$(node --version)" = v24.22.1

work="$(mktemp -d)"
http_pid=
https_pid=
cleanup() {
  for pid in "$http_pid" "$https_pid"; do
    if [[ -n "$pid" ]]; then
      kill -TERM "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  rm -r -- "$work"
}
trap cleanup EXIT
export HARNIQO_DISCOVERY_FILE="$HARNIQO_TEST_DISCOVERY_FILE"
export HARNIQO_CONTROL_HOST=127.0.0.1
export HARNIQO_TLS_CERT_FILE="$HARNIQO_TEST_CERT"
export HARNIQO_TLS_KEY_FILE="$HARNIQO_TEST_KEY_FILE"
HARNIQO_CONTROL_PROTOCOL=http HARNIQO_CONTROL_PORT=10056 node apps/control-plane/main.ts > "$work/http.log" 2>&1 &
http_pid=$!
HARNIQO_CONTROL_PROTOCOL=https HARNIQO_CONTROL_PORT=10057 node apps/control-plane/main.ts > "$work/https.log" 2>&1 &
https_pid=$!

for _ in {1..40}; do
  if grep -Fqx 'Harniqo HTTP discovery ready' "$work/http.log" && grep -Fqx 'Harniqo HTTPS discovery ready' "$work/https.log"; then
    break
  fi
  kill -0 "$http_pid" "$https_pid"
  sleep 0.2
done
grep -Fqx 'Harniqo HTTP discovery ready' "$work/http.log"
grep -Fqx 'Harniqo HTTPS discovery ready' "$work/https.log"
export NODE_EXTRA_CA_CERTS="$HARNIQO_TEST_CA"
export HARNIQO_SMOKE_BROWSER_EXECUTABLE="$repo/deploy/identity/test-browser.mjs"
export HARNIQO_SMOKE_LOGIN_ISSUER="$HARNIQO_TEST_ISSUER"
export HARNIQO_SMOKE_LOGIN_CLIENT_ID=harniqo-desktop
receipt_dir="${HARNIQO_TEST_RECEIPT_DIRECTORY:-$work}"
app_args=(--packaged)
if [[ "$#" -eq 1 ]]; then
  app_args=(--app "$1")
elif [[ "$#" -gt 1 ]]; then
  printf 'Usage: %s [installed desktop executable]\n' "$0" >&2
  exit 2
fi
for scheme in http https; do
  case "$scheme" in
    http) origin=http://127.0.0.1:10056 ;;
    https) origin=https://127.0.0.1:10057 ;;
  esac
  HARNIQO_SMOKE_LOGIN_ORIGIN="$origin" node apps/desktop/scripts/smoke.mjs "${app_args[@]}" \
    --receipt "$receipt_dir/$scheme.json"
done
