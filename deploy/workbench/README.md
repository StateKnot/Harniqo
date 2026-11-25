# Persistent Hermes workbench

The dedicated deployment uses a persistent PostgreSQL 17 volume and separate user services for the HTTPS API, trusted control worker and dedicated Hermes connector. The existing HTTP discovery and identity service can serve the same desktop client. Use the approved server port range and an immutable source checkout for all three services.

Run these commands on the deployment host from the pinned source checkout. Substitute its absolute paths. The private directory must be new. The image reference must come from the verified PostgreSQL archive or registry metadata.

```sh
python3 deploy/workbench/prepare.py \
  --directory /absolute/private/workbench \
  --database-image postgres@sha256:REPLACE_WITH_VERIFIED_DIGEST \
  --database-port 10052
docker compose -p harniqo-workbench \
  --env-file /absolute/private/workbench/database.env \
  -f deploy/workbench/compose.yaml up -d --wait
```

The prepare command writes private role password and connection-URL files, a stable `profile.json`, a native API token and empty workspace/state directories. It refuses an existing private directory. The database only publishes a loopback port. Preserve the Compose project name and its `workbench-data` volume across upgrades.

## Explicit identity and project seed

Read the actual user subject from the configured identity provider. Write a private `seed.json` with the following fields.

- `profile` is the complete object from the generated `profile.json`.
- `identities` contains exact `issuer`, `subject`, `principalId`, `canRead` and `canSubmit` values. The primary principal in the profile requires submit access. A second denied identity can explicitly have both permissions set to false.
- `budget` contains positive integer `organizationRunLimit`, `projectRunLimit` and `concurrentLimit` values. These limits apply to admitted Runs and outstanding reservations. Monetary accounting retains its own integration requirements.

```sh
node deploy/workbench/bootstrap.mjs \
  /absolute/private/workbench \
  /absolute/private/workbench/seed.json
```

Bootstrap creates separate migration, API and worker roles with no superuser or RLS-bypass privileges. It checks PostgreSQL 17, installs the locked pg-boss schema and applies all numbered store/access/queue/execution migrations in one transaction. A checksum ledger and advisory lock preserve the applied migration identity. The explicit seed inserts frozen bindings and project access. A rerun accepts the same records and rejects drift, preserving existing grant and budget state.

The initial execution binding has no readiness deadline. A trusted worker must obtain a current authenticated connector readiness response before renewing that deadline and advertising the target to desktop users.

## Service configuration

Prepare private `api.env`, `worker.env` and `connector.env` files using the corresponding runtime settings.

- API configuration uses `HARNIQO_DISCOVERY_FILE`, `HARNIQO_WORKBENCH_FILE`, `HARNIQO_WORKBENCH_DATABASE_URL_FILE`, `HARNIQO_CONTROL_HOST`, `HARNIQO_CONTROL_PORT`, `HARNIQO_TLS_CERT_FILE` and `HARNIQO_TLS_KEY_FILE`. Set `HARNIQO_CONTROL_PROTOCOL=https`. A private identity CA can be supplied through `NODE_EXTRA_CA_CERTS`.
- Worker configuration uses `HARNIQO_WORKER_FILE` and `HARNIQO_WORKER_DATABASE_URL_FILE`. Its connection file is the generated `worker.database-url`. Use the exact fixed deployment profile and mTLS controller credentials.
- Connector configuration uses `HARNIQO_CONNECTOR_FILE`, a dedicated immutable Hermes runtime, persistent connector state and the native API token. Its independent deadline guard must survive the connector main process. The connector user unit therefore uses `KillMode=process`.

The API uses `control.database-url`. Keep `admin.database-url` and `migrator.database-url` out of runtime service configuration. TLS private keys and database connection files require private owner-only permissions. [Control-plane settings](../../apps/control-plane/README.md) · [Dedicated connector](../../apps/runner/hermes-connector/README.md)

Render units using the exact deployed source and Node paths, then review and verify them before installing.

```sh
python3 deploy/workbench/render-units.py \
  --repository /absolute/pinned/source \
  --node /absolute/node-v24.22.1-linux-x64/bin/node \
  --directory /absolute/private/workbench
systemd-analyze --user verify /absolute/private/workbench/systemd/*.service
install -m 0600 /absolute/private/workbench/systemd/*.service "$XDG_CONFIG_HOME/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now harniqo-workbench-connector harniqo-workbench-worker harniqo-workbench-api
```

When `XDG_CONFIG_HOME` is unset, use the current user's `.config` directory. Keep the private configuration, workspace, connector journal and database volume through restarts. Validate two sequential native instructions and an installed desktop login/submission/reply before reporting the client ready for use.
