# Workbench access tokens

The control plane checks a signed access token from its configured HTTPS issuer against its own audience, the desktop OAuth client, a pinned signature algorithm, and the configured token profile. The JWKS URL is trusted startup configuration. Fetches use normal TLS trust, bounded responses and a rotation cache. For a private CA, provision its trust on the control-plane host.

- Use the `keycloak` profile for Keycloak access tokens containing `typ: Bearer` and the desktop client in `azp`. Configure an audience mapper on the desktop client so the access token includes the platform audience. The audience is a separate identifier from the desktop client ID. See [Keycloak audience mapper](https://www.keycloak.org/admin-api/protocol-mappers#oidc-audience-mapper).
- Use the `rfc9068` profile for external issuers that send signed JWT access tokens with the `at+jwt` media type and the desktop client in `client_id`. See [RFC 9068](https://www.rfc-editor.org/rfc/rfc9068.html).

The migration owner applies `002-external-identities.sql` after the first-run storage migration and provisions the exact verified issuer and subject against an internal principal and first-workbench organization. Disable that mapping to revoke access for subsequent API requests. Runtime transactions resolve the mapping afresh. Project membership and permissions remain enforced by the workbench store. Identity and project membership are provisioned separately.

Run the tests from the repository root with the pinned Node release.

```sh
node --test apps/control-plane/workbench-access/index.test.ts
```

Set `HARNIQO_ACCESS_POSTGRES_URL` to the private administrative connection URL of a disposable PostgreSQL 17 instance on loopback with database `harniqo_access_test` to include the migration, role isolation and revocation test. The test creates the migration owner and runtime roles in that database cluster.
