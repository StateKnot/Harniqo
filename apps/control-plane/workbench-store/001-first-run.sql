-- Apply in a migration transaction as the schema owner. Provision harniqo_control
-- separately as the non-owner, NOBYPASSRLS role used by the control plane.
CREATE SCHEMA harniqo;

CREATE TABLE harniqo.project_memberships (
    organization_id uuid NOT NULL,
    project_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    can_read boolean NOT NULL,
    can_submit boolean NOT NULL,
    PRIMARY KEY (organization_id, project_id, principal_id),
    CHECK (NOT can_submit OR can_read)
);

-- Provisions an authorized principal's session against one immutable target.
-- Retire targets via enabled and provision new rows when any binding changes.
CREATE TABLE harniqo.run_targets (
    organization_id uuid NOT NULL,
    project_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    session_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    deployment_id uuid NOT NULL,
    deployment_name text NOT NULL CHECK (char_length(deployment_name) BETWEEN 1 AND 128),
    deployment_revision_id uuid NOT NULL,
    configuration_revision_id uuid NOT NULL,
    input_snapshot_id uuid NOT NULL,
    budget_policy_id uuid NOT NULL,
    execution_target_id uuid NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    PRIMARY KEY (organization_id, project_id, principal_id, session_id),
    UNIQUE (session_id),
    UNIQUE (organization_id, project_id, principal_id, session_id, workspace_id,
        deployment_revision_id, configuration_revision_id, input_snapshot_id,
        budget_policy_id, execution_target_id),
    FOREIGN KEY (organization_id, project_id, principal_id)
        REFERENCES harniqo.project_memberships (organization_id, project_id, principal_id)
);

CREATE UNIQUE INDEX run_targets_one_enabled_session_per_deployment
    ON harniqo.run_targets (organization_id, principal_id, deployment_id) WHERE enabled;

CREATE TABLE harniqo.runs (
    organization_id uuid NOT NULL,
    project_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    id uuid NOT NULL,
    session_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    deployment_revision_id uuid NOT NULL,
    configuration_revision_id uuid NOT NULL,
    input_snapshot_id uuid NOT NULL,
    budget_policy_id uuid NOT NULL,
    execution_target_id uuid NOT NULL,
    required_capabilities text[] NOT NULL CHECK (required_capabilities <@ ARRAY['new_session']::text[]),
    prompt text NOT NULL CHECK (char_length(prompt) BETWEEN 1 AND 65536),
    state text NOT NULL CHECK (state IN (
        'accepted', 'queued', 'provisioning', 'running', 'waiting_approval',
        'waiting_input', 'stopping', 'reconciling', 'succeeded', 'failed', 'cancelled'
    )),
    revision bigint NOT NULL CHECK (revision BETWEEN 1 AND 9007199254740991),
    last_seq bigint NOT NULL CHECK (last_seq BETWEEN 1 AND 9007199254740991),
    native_session jsonb CHECK (jsonb_typeof(native_session) = 'object'),
    active_attempt_id uuid,
    accepted_at timestamptz NOT NULL,
    started_at timestamptz,
    ended_at timestamptz,
    PRIMARY KEY (organization_id, project_id, principal_id, id),
    UNIQUE (id),
    FOREIGN KEY (organization_id, project_id, principal_id, session_id, workspace_id,
        deployment_revision_id, configuration_revision_id, input_snapshot_id,
        budget_policy_id, execution_target_id)
        REFERENCES harniqo.run_targets (organization_id, project_id, principal_id, session_id, workspace_id,
            deployment_revision_id, configuration_revision_id, input_snapshot_id,
            budget_policy_id, execution_target_id),
    CHECK ((state IN ('succeeded', 'failed', 'cancelled')) = (ended_at IS NOT NULL)),
    CHECK (state NOT IN ('running', 'waiting_approval', 'waiting_input') OR
        (native_session IS NOT NULL AND active_attempt_id IS NOT NULL AND started_at IS NOT NULL))
);

CREATE UNIQUE INDEX runs_one_active_per_session
    ON harniqo.runs (session_id) WHERE ended_at IS NULL;

CREATE TABLE harniqo.commands (
    organization_id uuid NOT NULL,
    project_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    id uuid NOT NULL,
    operation text NOT NULL CHECK (operation = 'submit_run'),
    idempotency_key text NOT NULL CHECK (idempotency_key ~ '^[A-Za-z0-9._:-]{16,128}$'),
    canonical_request_sha256 text NOT NULL CHECK (canonical_request_sha256 ~ '^[a-f0-9]{64}$'),
    state text NOT NULL CHECK (state IN ('accepted', 'applied', 'rejected')),
    resource_id uuid NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (organization_id, project_id, principal_id, operation, idempotency_key),
    UNIQUE (id),
    FOREIGN KEY (organization_id, project_id, principal_id, resource_id)
        REFERENCES harniqo.runs (organization_id, project_id, principal_id, id)
);

CREATE TABLE harniqo.run_events (
    organization_id uuid NOT NULL,
    project_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    event_id uuid NOT NULL UNIQUE,
    run_id uuid NOT NULL,
    seq bigint NOT NULL CHECK (seq BETWEEN 1 AND 9007199254740991),
    persisted_at timestamptz NOT NULL,
    platform jsonb CHECK (jsonb_typeof(platform) = 'object'),
    source jsonb CHECK (jsonb_typeof(source) = 'object'),
    PRIMARY KEY (organization_id, project_id, principal_id, run_id, seq),
    FOREIGN KEY (organization_id, project_id, principal_id, run_id)
        REFERENCES harniqo.runs (organization_id, project_id, principal_id, id),
    CHECK ((platform IS NULL) <> (source IS NULL)),
    CHECK (seq <> 1 OR (platform IS NOT NULL AND
        (platform @> '{"type":"run.status_changed","payload":{"state":"accepted","run_revision":1}}')))
);

ALTER TABLE harniqo.project_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE harniqo.project_memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON harniqo.project_memberships TO CURRENT_USER USING (true);
CREATE POLICY current_principal ON harniqo.project_memberships FOR SELECT TO harniqo_control
    USING (
        organization_id = nullif(current_setting('app.organization_id', true), '')::uuid
        AND principal_id = nullif(current_setting('app.principal_id', true), '')::uuid
    );

-- Apply the same scoped membership policy to each project-owned relation. The
-- subquery also uses the principal/organization policy of the membership table.
DO $migration$
DECLARE
    relation text;
BEGIN
    FOREACH relation IN ARRAY ARRAY['run_targets', 'runs', 'commands', 'run_events'] LOOP
        EXECUTE format('ALTER TABLE harniqo.%I ENABLE ROW LEVEL SECURITY', relation);
        EXECUTE format('ALTER TABLE harniqo.%I FORCE ROW LEVEL SECURITY', relation);
        EXECUTE format('CREATE POLICY migration_owner ON harniqo.%I TO CURRENT_USER USING (true)', relation);
        EXECUTE format(
            'CREATE POLICY project_read ON harniqo.%1$I FOR SELECT TO harniqo_control USING (
                EXISTS (SELECT FROM harniqo.project_memberships m
                    WHERE (m.organization_id, m.project_id, m.principal_id) =
                      (%1$I.organization_id, %1$I.project_id, %1$I.principal_id) AND m.can_read)
            )', relation
        );
    END LOOP;

    FOREACH relation IN ARRAY ARRAY['runs', 'commands', 'run_events'] LOOP
        EXECUTE format(
            'CREATE POLICY project_submit ON harniqo.%1$I FOR INSERT TO harniqo_control WITH CHECK (
                EXISTS (SELECT FROM harniqo.project_memberships m
                    WHERE (m.organization_id, m.project_id, m.principal_id) =
                      (%1$I.organization_id, %1$I.project_id, %1$I.principal_id) AND m.can_submit)
            )', relation
        );
    END LOOP;
END
$migration$;

GRANT USAGE ON SCHEMA harniqo TO harniqo_control;
GRANT SELECT ON harniqo.project_memberships, harniqo.run_targets,
    harniqo.runs, harniqo.commands, harniqo.run_events TO harniqo_control;
GRANT INSERT ON harniqo.runs, harniqo.commands, harniqo.run_events TO harniqo_control;
