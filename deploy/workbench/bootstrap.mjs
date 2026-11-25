import { createHash } from 'node:crypto';
import { constants } from 'node:fs';
import { open, readFile, readdir, realpath } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const repository = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const require = createRequire(join(repository, 'apps/control-plane/package.json'));
const { Pool } = require('pg');
const { getConstructionPlans } = await import(require.resolve('pg-boss'));
let stage = 'configuration';

async function privateText(path, maximum = 32768) {
  if (!isAbsolute(path)) throw new Error('INVALID_PRIVATE_PATH');
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.nlink !== 1 || stat.size < 1 || stat.size > maximum
      || stat.uid !== process.getuid?.() || (stat.mode & 0o077) !== 0) throw new Error('UNSAFE_PRIVATE_FILE');
    return await file.readFile('utf8');
  } finally { await file.close(); }
}

function fields(value, required) {
  return value && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === required.length && required.every(key => Object.hasOwn(value, key));
}

function validateSeed(seed) {
  const profileFields = [
    'organizationId', 'projectId', 'principalId', 'sessionId', 'workspaceId', 'deploymentId',
    'executionTargetId', 'runtimeInstanceId', 'harnessReleaseId', 'platformEpoch',
    'deploymentRevisionId', 'configurationRevisionId', 'inputSnapshotId', 'budgetPolicyId', 'name',
  ];
  const uuid = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/u;
  if (!fields(seed, ['profile', 'identities', 'budget']) || !fields(seed.profile, profileFields)
    || !profileFields.filter(key => key !== 'name').every(key => uuid.test(seed.profile[key]))
    || typeof seed.profile.name !== 'string' || !seed.profile.name.trim() || seed.profile.name.length > 128
    || !Array.isArray(seed.identities) || seed.identities.length < 1 || seed.identities.length > 32
    || !fields(seed.budget, ['organizationRunLimit', 'projectRunLimit', 'concurrentLimit'])
    || !Object.values(seed.budget).every(value => Number.isSafeInteger(value) && value > 0)
    || seed.budget.concurrentLimit > Math.min(seed.budget.organizationRunLimit, seed.budget.projectRunLimit)) {
    throw new Error('INVALID_SEED');
  }
  for (const identity of seed.identities) {
    if (!fields(identity, ['issuer', 'subject', 'principalId', 'canRead', 'canSubmit'])
      || typeof identity.issuer !== 'string' || !/^https:\/\/[^?#@\\\s]+$/u.test(identity.issuer)
      || typeof identity.subject !== 'string' || !identity.subject || identity.subject.length > 255
      || !uuid.test(identity.principalId) || typeof identity.canRead !== 'boolean'
      || typeof identity.canSubmit !== 'boolean' || identity.canSubmit && !identity.canRead) {
      throw new Error('INVALID_SEED_IDENTITY');
    }
  }
  if (!seed.identities.some(identity => identity.principalId === seed.profile.principalId && identity.canSubmit)) {
    throw new Error('PRIMARY_PRINCIPAL_NOT_GRANTED');
  }
}

async function insertExact(client, table, row, keys) {
  stage = 'seed-' + table;
  const columns = Object.keys(row);
  await client.query(`INSERT INTO harniqo.${table} (${columns.join(',')})
    VALUES (${columns.map((_, index) => '$' + (index + 1)).join(',')}) ON CONFLICT DO NOTHING`, Object.values(row));
  const result = await client.query(`SELECT ${columns.join(',')} FROM harniqo.${table}
    WHERE ${keys.map((key, index) => key + '=$' + (index + 1)).join(' AND ')}`, keys.map(key => row[key]));
  if (result.rows.length !== 1 || columns.some(key => {
    const actual = result.rows[0][key];
    return (typeof row[key] === 'number' && typeof actual === 'string' ? Number(actual) : actual) !== row[key];
  })) {
    throw new Error('EXISTING_SEED_DIFFERS');
  }
}

async function seedDatabase(client, seed) {
  const p = seed.profile;
  for (const identity of seed.identities) {
    await insertExact(client, 'external_identities', {
      issuer: identity.issuer, subject: identity.subject, organization_id: p.organizationId,
      principal_id: identity.principalId, enabled: true,
    }, ['issuer', 'subject']);
    await insertExact(client, 'project_memberships', {
      organization_id: p.organizationId, project_id: p.projectId, principal_id: identity.principalId,
      can_read: identity.canRead, can_submit: identity.canSubmit,
    }, ['organization_id', 'project_id', 'principal_id']);
  }
  await insertExact(client, 'workbench_organization_budgets', {
    organization_id: p.organizationId, run_limit: seed.budget.organizationRunLimit,
    concurrent_limit: seed.budget.concurrentLimit,
  }, ['organization_id']);
  await insertExact(client, 'workbench_project_budgets', {
    organization_id: p.organizationId, project_id: p.projectId, budget_policy_id: p.budgetPolicyId,
    run_limit: seed.budget.projectRunLimit, concurrent_limit: seed.budget.concurrentLimit,
  }, ['organization_id', 'project_id']);
  await insertExact(client, 'run_targets', {
    organization_id: p.organizationId, project_id: p.projectId, principal_id: p.principalId,
    session_id: p.sessionId, workspace_id: p.workspaceId, deployment_id: p.deploymentId,
    deployment_name: p.name, deployment_revision_id: p.deploymentRevisionId,
    configuration_revision_id: p.configurationRevisionId, input_snapshot_id: p.inputSnapshotId,
    budget_policy_id: p.budgetPolicyId, execution_target_id: p.executionTargetId, enabled: true,
  }, ['organization_id', 'project_id', 'principal_id', 'session_id']);
  await insertExact(client, 'hermes_execution_bindings', {
    organization_id: p.organizationId, project_id: p.projectId, principal_id: p.principalId,
    execution_target_id: p.executionTargetId, deployment_revision_id: p.deploymentRevisionId,
    workspace_id: p.workspaceId, runtime_instance_id: p.runtimeInstanceId,
    harness_release_id: p.harnessReleaseId, platform_epoch: p.platformEpoch,
    instance_generation: 1, enabled: true,
  }, ['organization_id', 'project_id', 'principal_id', 'execution_target_id', 'deployment_revision_id', 'workspace_id']);
}

async function main() {
  const [directory, seedPath] = process.argv.slice(2);
  if (!directory || !seedPath || await realpath(directory) !== directory) throw new Error('PRIVATE_DIRECTORY_REQUIRED');
  const seed = JSON.parse(await privateText(seedPath));
  validateSeed(seed);
  const passwords = Object.fromEntries(await Promise.all(['migrator', 'control', 'worker'].map(async name => {
    const password = (await privateText(join(directory, name + '.password'), 128)).trim();
    if (!/^[0-9a-f]{64}$/u.test(password)) throw new Error('INVALID_ROLE_PASSWORD');
    return [name, password];
  })));
  const url = new URL((await privateText(join(directory, 'admin.database-url'), 8192)).trim());
  if (url.protocol !== 'postgresql:' || url.hostname !== '127.0.0.1' || url.pathname !== '/harniqo'
    || url.username !== 'harniqo_admin' || url.search || url.hash) throw new Error('INVALID_BOOTSTRAP_DATABASE');
  const pool = new Pool({ connectionString: url.href, max: 1, connectionTimeoutMillis: 5000, statement_timeout: 30000 });
  const client = await pool.connect();
  let committed = false;
  try {
    await client.query('BEGIN');
    await client.query("SELECT pg_advisory_xact_lock(hashtextextended('harniqo-workbench-bootstrap',0))");
    stage = 'roles';
    const version = await client.query("SELECT current_setting('server_version_num')::integer AS version");
    if (version.rows[0].version < 170000 || version.rows[0].version >= 180000) throw new Error('POSTGRES_17_REQUIRED');
    for (const [name, password] of Object.entries(passwords)) {
      const role = 'harniqo_' + name;
      const result = await client.query('SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=$1', [role]);
      if (result.rows.length) {
        const row = result.rows[0];
        if (!row.rolcanlogin || row.rolsuper || row.rolbypassrls) throw new Error('UNSAFE_EXISTING_ROLE');
      } else {
        await client.query(`CREATE ROLE ${role} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '${password}'`);
      }
    }
    for (const role of ['harniqo_control', 'harniqo_worker']) {
      const member = await client.query("SELECT pg_has_role($1, 'harniqo_migrator', 'MEMBER') AS member", [role]);
      if (member.rows[0].member) throw new Error('RUNTIME_INHERITS_MIGRATION_ROLE');
    }
    await client.query('GRANT CREATE ON DATABASE harniqo TO harniqo_migrator');
    await client.query('SET LOCAL ROLE harniqo_migrator');
    const existing = await client.query("SELECT to_regnamespace('harniqo') IS NOT NULL AS present, to_regnamespace('harniqo_deploy') IS NOT NULL AS ledger");
    if (existing.rows[0].present && !existing.rows[0].ledger) throw new Error('EXISTING_SCHEMA_HAS_NO_DEPLOY_LEDGER');
    await client.query(`CREATE SCHEMA IF NOT EXISTS harniqo_deploy;
      CREATE TABLE IF NOT EXISTS harniqo_deploy.schema_migrations (
        name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now()
      )`);
    // The upstream export owns a transaction. Retain its DDL and lock inside
    // this bootstrap transaction, which also owns SET LOCAL ROLE and seeding.
    const construction = getConstructionPlans('harniqo_queue');
    if (!/^\s*BEGIN;/u.test(construction) || !/COMMIT;\s*$/u.test(construction)) {
      throw new Error('PG_BOSS_PLAN_WRAPPER_CHANGED');
    }
    const queueSql = construction.replace(/^\s*BEGIN;\s*/u, '').replace(/\s*COMMIT;\s*$/u, '');
    const migrations = [{ name: '000-pg-boss-12.38.2-schema46.sql', sql: queueSql }];
    for (const folder of ['workbench-store', 'workbench-access', 'workbench-queue', 'workbench-execution']) {
      const path = join(repository, 'apps/control-plane', folder);
      for (const file of await readdir(path)) {
        if (/^\d{3}-[a-z0-9-]+\.sql$/u.test(file)) migrations.push({ name: file, sql: await readFile(join(path, file), 'utf8') });
      }
    }
    migrations.sort((a, b) => a.name.localeCompare(b.name, 'en'));
    for (const migration of migrations) {
      stage = 'migration-' + migration.name;
      const hash = createHash('sha256').update(migration.sql).digest('hex');
      const applied = await client.query('SELECT sha256 FROM harniqo_deploy.schema_migrations WHERE name=$1', [migration.name]);
      if (applied.rows.length) {
        if (applied.rows[0].sha256 !== hash) throw new Error('APPLIED_MIGRATION_CHANGED');
        continue;
      }
      await client.query(migration.sql);
      await client.query('INSERT INTO harniqo_deploy.schema_migrations (name,sha256) VALUES ($1,$2)', [migration.name, hash]);
    }
    stage = 'seed';
    const identity = await client.query('SELECT current_user AS role');
    if (identity.rows[0].role !== 'harniqo_migrator') throw new Error('MIGRATION_ROLE_CHANGED');
    await seedDatabase(client, seed);
    await client.query('COMMIT');
    committed = true;
    console.log(JSON.stringify({ ok: true, migrations: migrations.map(item => item.name), profile: 'profile.json', nativeReadiness: 'requires_current_heartbeat' }));
  } finally {
    if (!committed) await client.query('ROLLBACK').catch(() => {});
    client.release();
    await pool.end();
  }
}

try { await main(); }
catch (error) {
  const code = typeof error?.code === 'string' && /^[A-Z0-9_]{1,64}$/u.test(error.code) ? error.code
    : typeof error?.message === 'string' && /^[A-Z0-9_]+$/u.test(error.message) ? error.message : 'BOOTSTRAP_FAILED';
  console.error(JSON.stringify({ ok: false, stage, code }));
  process.exitCode = 1;
}
