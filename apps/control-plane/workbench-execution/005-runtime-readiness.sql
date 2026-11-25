-- The trusted worker renews only the short health lease of its fixed runtime.
GRANT UPDATE (ready_until) ON harniqo.hermes_execution_bindings TO harniqo_worker;
CREATE POLICY worker_readiness_lease ON harniqo.hermes_execution_bindings
    AS RESTRICTIVE FOR UPDATE TO harniqo_worker
    USING (
        organization_id = nullif(current_setting('app.organization_id', true), '')::uuid
        AND project_id = nullif(current_setting('app.project_id', true), '')::uuid
        AND principal_id = nullif(current_setting('app.principal_id', true), '')::uuid
    )
    WITH CHECK (
        ready_until IS NULL OR (
            ready_until > statement_timestamp()
            AND ready_until <= statement_timestamp() + interval '15 seconds'
        )
    );
