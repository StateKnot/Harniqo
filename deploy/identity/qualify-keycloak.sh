#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -eq 0 ]]; then
  printf 'Usage: %s <integration command> [args ...]\n' "$0" >&2
  exit 2
fi

image=quay.io/keycloak/keycloak:26.8.2@sha256:723b1d6e254c0fc96d5cffec860c7e0744d6a3277822b865bb538cf891575e41
base_url=https://127.0.0.1:10055
issuer="$base_url/realms/harniqo"
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/harniqo-keycloak.XXXXXXXX")"
work_dir="$(cd "$work_dir" && pwd -P)"
container_id=''

cleanup() {
  if [[ -n "$container_id" ]]; then
    docker rm -f "$container_id" >/dev/null 2>&1 || true
  fi
  rm -rf -- "$work_dir"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 1 \
  -subj '/CN=Harniqo OIDC Integration CA' \
  -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -keyout "$work_dir/ca.key" -out "$work_dir/ca.pem" >/dev/null 2>&1
openssl req -new -newkey rsa:2048 -nodes -subj '/CN=127.0.0.1' \
  -keyout "$work_dir/server.key" -out "$work_dir/server.csr" >/dev/null 2>&1
openssl x509 -req -sha256 -days 1 -CAcreateserial -CA "$work_dir/ca.pem" \
  -CAkey "$work_dir/ca.key" -in "$work_dir/server.csr" -out "$work_dir/server.pem" \
  -extfile <(printf '%s\n' 'basicConstraints=critical,CA:FALSE' \
    'keyUsage=critical,digitalSignature,keyEncipherment' \
    'extendedKeyUsage=serverAuth' 'subjectAltName=IP:127.0.0.1') >/dev/null 2>&1

openssl rand -hex 24 > "$work_dir/password"
jq -n --rawfile password "$work_dir/password" '{
  realm: "harniqo",
  enabled: true,
  sslRequired: "all",
  clients: [{
    clientId: "harniqo-desktop",
    protocol: "openid-connect",
    enabled: true,
    publicClient: true,
    standardFlowEnabled: true,
    implicitFlowEnabled: false,
    directAccessGrantsEnabled: false,
    serviceAccountsEnabled: false,
    redirectUris: ["http://127.0.0.1/oauth/callback"],
    attributes: {"pkce.code.challenge.method": "S256"}
  }],
  users: [{
    username: "harniqo-integration",
    enabled: true,
    firstName: "Harniqo",
    lastName: "Integration",
    email: "integration@example.invalid",
    emailVerified: true,
    credentials: [{type: "password", value: ($password | rtrimstr("\n")), temporary: false}]
  }]
}' > "$work_dir/harniqo-realm.json"
jq -n --arg issuer "$issuer" '{
  issuer: $issuer,
  client_id: "harniqo-desktop",
  required_scopes: ["openid", "profile"],
  authorization_code_pkce_required: true
}' > "$work_dir/discovery.json"

# Keycloak runs as UID 1000 inside the container. Bind only these files from the
# mode-0700 work directory so the container can read them without a UID mapping.
chmod 0644 "$work_dir/server.key" "$work_dir/server.pem" "$work_dir/harniqo-realm.json"
container_id="$(docker run --detach --rm --memory 2g \
  --publish 127.0.0.1:10055:8443 \
  --mount "type=bind,src=$work_dir/server.key,dst=/fixture.key,readonly" \
  --mount "type=bind,src=$work_dir/server.pem,dst=/fixture.pem,readonly" \
  --mount "type=bind,src=$work_dir/harniqo-realm.json,dst=/opt/keycloak/data/import/harniqo-realm.json,readonly" \
  "$image" start-dev --import-realm --hostname="$base_url" --http-enabled=false \
  --https-certificate-file=/fixture.pem --https-certificate-key-file=/fixture.key)"

deadline=$((SECONDS + 90))
until curl --noproxy '*' --fail --silent --max-time 2 --cacert "$work_dir/ca.pem" \
  "$issuer/.well-known/openid-configuration" > "$work_dir/provider.json"; do
  if (( SECONDS >= deadline )) || ! docker container inspect "$container_id" >/dev/null 2>&1; then
    printf 'Keycloak did not become ready on %s\n' "$base_url" >&2
    exit 1
  fi
  sleep 1
done
jq -e --arg issuer "$issuer" '
  .issuer == $issuer and
  (.code_challenge_methods_supported | index("S256") != null)
' "$work_dir/provider.json" >/dev/null

export HARNIQO_TEST_ISSUER="$issuer"
export HARNIQO_TEST_CA="$work_dir/ca.pem"
export HARNIQO_TEST_CERT="$work_dir/server.pem"
export HARNIQO_TEST_KEY_FILE="$work_dir/server.key"
export HARNIQO_TEST_DISCOVERY_FILE="$work_dir/discovery.json"
export HARNIQO_TEST_USERNAME=harniqo-integration
export HARNIQO_TEST_PASSWORD_FILE="$work_dir/password"
"$@"
