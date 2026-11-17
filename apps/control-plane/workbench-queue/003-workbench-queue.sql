-- Apply in the same database and as the same migration owner as 001-first-run.
-- The pg-boss 12.38.2 construction plan for harniqo_queue is a prerequisite.
CREATE TABLE harniqo.workbench_organization_budgets (
    organization_id uuid PRIMARY KEY,
    run_limit integer NOT NULL CHECK (run_limit > 0),
    concurrent_limit integer NOT NULL CHECK (concurrent_limit BETWEEN 1 AND run_limit),
    admitted_runs integer NOT NULL DEFAULT 0 CHECK (admitted_runs BETWEEN 0 AND run_limit)
);

CREATE TABLE harniqo.workbench_project_budgets (
    organization_id uuid NOT NULL REFERENCES harniqo.workbench_organization_budgets,
    project_id uuid NOT NULL,
    budget_policy_id uuid NOT NULL,
    run_limit integer NOT NULL CHECK (run_limit > 0),
    concurrent_limit integer NOT NULL CHECK (concurrent_limit BETWEEN 1 AND run_limit),
    admitted_runs integer NOT NULL DEFAULT 0 CHECK (admitted_runs BETWEEN 0 AND run_limit),
    PRIMARY KEY (organization_id, project_id),
    UNIQUE (organization_id, project_id, budget_policy_id)
);

CREATE TABLE harniqo.workbench_budget_reservations (
    organization_id uuid NOT NULL,
    project_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    run_id uuid PRIMARY KEY,
    budget_policy_id uuid NOT NULL,
    command_id uuid NOT NULL UNIQUE REFERENCES harniqo.commands (id),
    reserved_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    finalized_at timestamptz CHECK (finalized_at >= reserved_at),
    FOREIGN KEY (organization_id, project_id, principal_id, run_id)
        REFERENCES harniqo.runs (organization_id, project_id, principal_id, id),
    FOREIGN KEY (organization_id, project_id, budget_policy_id)
        REFERENCES harniqo.workbench_project_budgets (organization_id, project_id, budget_policy_id)
);

CREATE INDEX workbench_active_reservations
    ON harniqo.workbench_budget_reservations (organization_id, project_id) WHERE finalized_at IS NULL;

ALTER TABLE harniqo.workbench_organization_budgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE harniqo.workbench_organization_budgets FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON harniqo.workbench_organization_budgets TO CURRENT_USER USING (true);

ALTER TABLE harniqo.workbench_project_budgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE harniqo.workbench_project_budgets FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON harniqo.workbench_project_budgets TO CURRENT_USER USING (true);

ALTER TABLE harniqo.workbench_budget_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE harniqo.workbench_budget_reservations FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON harniqo.workbench_budget_reservations TO CURRENT_USER USING (true);

GRANT USAGE ON SCHEMA harniqo TO harniqo_worker;
GRANT SELECT, UPDATE (finalized_at) ON harniqo.workbench_budget_reservations TO harniqo_worker;
CREATE POLICY worker_read ON harniqo.workbench_budget_reservations FOR SELECT TO harniqo_worker USING (
    organization_id = nullif(current_setting('app.organization_id', true), '')::uuid
    AND project_id = nullif(current_setting('app.project_id', true), '')::uuid
    AND principal_id = nullif(current_setting('app.principal_id', true), '')::uuid
);
CREATE POLICY worker_finalize ON harniqo.workbench_budget_reservations FOR UPDATE TO harniqo_worker USING (
    organization_id = nullif(current_setting('app.organization_id', true), '')::uuid
    AND project_id = nullif(current_setting('app.project_id', true), '')::uuid
    AND principal_id = nullif(current_setting('app.principal_id', true), '')::uuid
    AND finalized_at IS NULL
) WITH CHECK (
    organization_id = nullif(current_setting('app.organization_id', true), '')::uuid
    AND project_id = nullif(current_setting('app.project_id', true), '')::uuid
    AND principal_id = nullif(current_setting('app.principal_id', true), '')::uuid
    AND finalized_at IS NOT NULL
    AND EXISTS (SELECT FROM harniqo.runs r WHERE
        (r.organization_id, r.project_id, r.principal_id, r.id) =
            (workbench_budget_reservations.organization_id, workbench_budget_reservations.project_id,
             workbench_budget_reservations.principal_id, workbench_budget_reservations.run_id)
        AND r.state IN ('succeeded', 'failed', 'cancelled') AND r.ended_at <= finalized_at)
);

-- Project authority is checked again against the pending Run and Command. The owner
-- can count across principals in one organization; the producer has no such table grant.
CREATE FUNCTION harniqo.reserve_workbench_run(p_run uuid, p_command uuid) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    bound_run harniqo.runs%ROWTYPE;
    org_budget harniqo.workbench_organization_budgets%ROWTYPE;
    project_budget harniqo.workbench_project_budgets%ROWTYPE;
    org_active bigint;
    project_active bigint;
BEGIN
    SELECT r.* INTO bound_run FROM harniqo.runs r
    JOIN harniqo.commands c ON
        (c.organization_id, c.project_id, c.principal_id, c.resource_id) =
            (r.organization_id, r.project_id, r.principal_id, r.id)
    JOIN harniqo.project_memberships m ON
        (m.organization_id, m.project_id, m.principal_id) =
            (r.organization_id, r.project_id, r.principal_id)
    WHERE r.id = p_run AND c.id = p_command AND r.state = 'accepted'
        AND c.operation = 'submit_run' AND c.state = 'accepted' AND m.can_submit
        AND r.organization_id = nullif(current_setting('app.organization_id', true), '')::uuid
        AND r.principal_id = nullif(current_setting('app.principal_id', true), '')::uuid;
    IF NOT FOUND THEN RETURN 'FORBIDDEN'; END IF;

    -- Organization before project serializes admissions across sessions and principals.
    SELECT * INTO org_budget FROM harniqo.workbench_organization_budgets b
    WHERE b.organization_id = bound_run.organization_id FOR UPDATE;
    IF NOT FOUND THEN RETURN 'BUDGET_EXCEEDED'; END IF;

    SELECT * INTO project_budget FROM harniqo.workbench_project_budgets b
    WHERE (b.organization_id, b.project_id, b.budget_policy_id) =
        (bound_run.organization_id, bound_run.project_id, bound_run.budget_policy_id) FOR UPDATE;
    IF NOT FOUND OR org_budget.admitted_runs >= org_budget.run_limit
        OR project_budget.admitted_runs >= project_budget.run_limit THEN
        RETURN 'BUDGET_EXCEEDED';
    END IF;

    SELECT count(*), count(*) FILTER (WHERE r.project_id = bound_run.project_id)
        INTO org_active, project_active
    FROM harniqo.workbench_budget_reservations r
    WHERE r.organization_id = bound_run.organization_id AND r.finalized_at IS NULL;
    IF org_active >= org_budget.concurrent_limit OR project_active >= project_budget.concurrent_limit THEN
        RETURN 'QUOTA_EXCEEDED';
    END IF;

    INSERT INTO harniqo.workbench_budget_reservations
        (organization_id, project_id, principal_id, run_id, budget_policy_id, command_id)
    VALUES (bound_run.organization_id, bound_run.project_id, bound_run.principal_id,
        p_run, bound_run.budget_policy_id, p_command);
    UPDATE harniqo.workbench_organization_budgets SET admitted_runs = admitted_runs + 1
        WHERE organization_id = bound_run.organization_id;
    UPDATE harniqo.workbench_project_budgets SET admitted_runs = admitted_runs + 1
        WHERE (organization_id, project_id) = (bound_run.organization_id, bound_run.project_id);
    RETURN 'reserved';
END
$function$;

REVOKE ALL ON FUNCTION harniqo.reserve_workbench_run(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION harniqo.reserve_workbench_run(uuid, uuid) TO harniqo_control;

-- This dedicated schema contains only Harniqo's workbench queue. Queue DDL runs
-- as the migration owner, with role-specific access restricted to its partition.
DO $queue$
DECLARE
    job_table text;
BEGIN
    IF (SELECT version FROM harniqo_queue.version) IS DISTINCT FROM 46 THEN
        RAISE EXCEPTION 'Harniqo requires pg-boss schema version 46';
    END IF;
    IF EXISTS (SELECT FROM harniqo_queue.queue) THEN
        RAISE EXCEPTION 'Harniqo requires an empty dedicated pg-boss schema at installation';
    END IF;
    PERFORM harniqo_queue.create_queue('harniqo-workbench-run',
        '{"policy":"standard","partition":true,"retryLimit":4,"retryDelay":10,"retryBackoff":true,"expireInSeconds":300,"retentionSeconds":1209600}'::jsonb);
    SELECT table_name INTO STRICT job_table FROM harniqo_queue.queue WHERE name = 'harniqo-workbench-run';
    EXECUTE format('GRANT INSERT, SELECT (id) ON TABLE harniqo_queue.%I TO harniqo_control', job_table);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE harniqo_queue.%I TO harniqo_worker', job_table);
END
$queue$;

REVOKE ALL ON SCHEMA harniqo_queue FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA harniqo_queue FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA harniqo_queue FROM PUBLIC;
GRANT USAGE ON SCHEMA harniqo_queue TO harniqo_control, harniqo_worker;
GRANT SELECT ON harniqo_queue.version, harniqo_queue.queue TO harniqo_control, harniqo_worker;
GRANT EXECUTE ON FUNCTION harniqo_queue.job_now() TO harniqo_control, harniqo_worker;
