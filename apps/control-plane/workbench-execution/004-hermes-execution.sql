-- Run after the store and budget migrations, as the same migration owner.
-- The separate harniqo_worker login must be NOSUPERUSER NOBYPASSRLS and have no owner membership.
CREATE TABLE harniqo.hermes_execution_bindings (
    organization_id uuid NOT NULL,
    project_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    execution_target_id uuid NOT NULL,
    deployment_revision_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    runtime_instance_id uuid NOT NULL,
    harness_release_id uuid NOT NULL,
    platform_epoch uuid NOT NULL,
    instance_generation bigint NOT NULL CHECK (instance_generation BETWEEN 1 AND 9007199254740991),
    enabled boolean NOT NULL DEFAULT false,
    ready_until timestamptz,
    PRIMARY KEY (organization_id, project_id, principal_id, execution_target_id, deployment_revision_id, workspace_id),
    UNIQUE (runtime_instance_id)
);

CREATE SEQUENCE harniqo.hermes_lease_generation AS bigint MAXVALUE 9007199254740991 NO CYCLE;

CREATE TABLE harniqo.hermes_attempts (
    organization_id uuid NOT NULL,
    project_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    run_id uuid NOT NULL,
    attempt_id uuid NOT NULL UNIQUE,
    workspace_id uuid NOT NULL,
    execution_target_id uuid NOT NULL,
    deployment_revision_id uuid NOT NULL,
    runtime_instance_id uuid NOT NULL,
    harness_release_id uuid NOT NULL,
    platform_epoch uuid NOT NULL,
    instance_generation bigint NOT NULL CHECK (instance_generation BETWEEN 1 AND 9007199254740991),
    lease_generation bigint NOT NULL UNIQUE DEFAULT nextval('harniqo.hermes_lease_generation'),
    native_session_id text NOT NULL,
    native_run_id text CHECK (native_run_id ~ '^run_[0-9a-f]{32}$'),
    served_model text CHECK (char_length(served_model) BETWEEN 1 AND 200),
    start_authorized_at timestamptz,
    released_at timestamptz,
    PRIMARY KEY (organization_id, project_id, principal_id, run_id),
    FOREIGN KEY (organization_id, project_id, principal_id, run_id)
        REFERENCES harniqo.runs (organization_id, project_id, principal_id, id),
    CHECK (native_run_id IS NULL OR start_authorized_at IS NOT NULL)
);

-- A missing worker, lost start receipt or expired lease keeps this claim.
CREATE UNIQUE INDEX hermes_active_writer_per_workspace
    ON harniqo.hermes_attempts (workspace_id) WHERE released_at IS NULL;

-- These privileges belong to the trusted coordinator. Runner processes use their own
-- authenticated assignment transport and do not receive a PostgreSQL connection.
GRANT USAGE ON SCHEMA harniqo TO harniqo_worker;
GRANT USAGE ON SEQUENCE harniqo.hermes_lease_generation TO harniqo_worker;
GRANT SELECT ON harniqo.runs, harniqo.commands, harniqo.run_targets,
    harniqo.hermes_execution_bindings, harniqo.hermes_attempts TO harniqo_worker;
GRANT SELECT (organization_id, project_id, principal_id, can_submit)
    ON harniqo.project_memberships TO harniqo_worker;
GRANT UPDATE (state, revision, last_seq, active_attempt_id, native_session, started_at, ended_at)
    ON harniqo.runs TO harniqo_worker;
GRANT INSERT ON harniqo.run_events, harniqo.hermes_attempts TO harniqo_worker;
GRANT UPDATE (native_run_id, served_model, start_authorized_at, released_at)
    ON harniqo.hermes_attempts TO harniqo_worker;

ALTER TABLE harniqo.hermes_execution_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE harniqo.hermes_execution_bindings FORCE ROW LEVEL SECURITY;
ALTER TABLE harniqo.hermes_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE harniqo.hermes_attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON harniqo.hermes_execution_bindings TO CURRENT_USER USING (true);
CREATE POLICY migration_owner ON harniqo.hermes_attempts TO CURRENT_USER USING (true);
GRANT SELECT (organization_id, project_id, principal_id, execution_target_id,
    deployment_revision_id, workspace_id, platform_epoch, enabled, ready_until)
    ON harniqo.hermes_execution_bindings TO harniqo_control;
CREATE POLICY control_readiness ON harniqo.hermes_execution_bindings FOR SELECT TO harniqo_control USING (
    EXISTS (SELECT 1 FROM harniqo.project_memberships m
        WHERE (m.organization_id, m.project_id, m.principal_id) =
          (hermes_execution_bindings.organization_id, hermes_execution_bindings.project_id,
            hermes_execution_bindings.principal_id)
        AND m.can_submit));

CREATE FUNCTION harniqo.hermes_execution_ready(org_id uuid, proj_id uuid, user_id uuid,
    target_id uuid, revision_id uuid, work_id uuid, epoch_id uuid)
RETURNS boolean LANGUAGE sql STABLE SET search_path = pg_catalog
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM harniqo.hermes_execution_bindings b
        WHERE (b.organization_id, b.project_id, b.principal_id, b.execution_target_id,
          b.deployment_revision_id, b.workspace_id) = (org_id, proj_id, user_id, target_id, revision_id, work_id)
          AND b.platform_epoch = epoch_id AND b.enabled AND b.ready_until > statement_timestamp()
    )
$function$;
REVOKE ALL ON FUNCTION harniqo.hermes_execution_ready(uuid, uuid, uuid, uuid, uuid, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION harniqo.hermes_execution_ready(uuid, uuid, uuid, uuid, uuid, uuid, uuid) TO harniqo_control;

DO $migration$
DECLARE
    relation text;
BEGIN
    FOREACH relation IN ARRAY ARRAY['project_memberships', 'runs', 'commands', 'run_targets', 'run_events',
        'hermes_execution_bindings', 'hermes_attempts'] LOOP
        EXECUTE format('CREATE POLICY worker_scoped ON harniqo.%I TO harniqo_worker USING (
            organization_id = nullif(current_setting(''app.organization_id'', true), '''')::uuid
            AND project_id = nullif(current_setting(''app.project_id'', true), '''')::uuid
            AND principal_id = nullif(current_setting(''app.principal_id'', true), '''')::uuid
        )', relation);
    END LOOP;
END
$migration$;
