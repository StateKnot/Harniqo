-- Apply as the migration owner after workbench-store/001-first-run.sql.
CREATE TABLE harniqo.external_identities (
    issuer text COLLATE "C" NOT NULL CHECK (octet_length(issuer) BETWEEN 1 AND 2048),
    subject text COLLATE "C" NOT NULL CHECK (char_length(subject) BETWEEN 1 AND 255),
    organization_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    PRIMARY KEY (issuer, subject)
);

ALTER TABLE harniqo.external_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE harniqo.external_identities FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON harniqo.external_identities TO CURRENT_USER USING (true);
CREATE POLICY verified_identity ON harniqo.external_identities FOR SELECT TO harniqo_control
    USING (
        enabled
        AND organization_id = nullif(current_setting('app.organization_id', true), '')::uuid
        AND issuer = current_setting('app.oidc_issuer', true)
        AND subject = current_setting('app.oidc_subject', true)
    );

GRANT SELECT (issuer, subject, organization_id, principal_id, enabled)
    ON harniqo.external_identities TO harniqo_control;
