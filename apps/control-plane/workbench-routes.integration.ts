import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { randomBytes, randomUUID } from 'node:crypto';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:https';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { getCACertificates, setDefaultCACertificates } from 'node:tls';
import { exportJWK, generateKeyPair, SignJWT } from 'jose';
import { Kysely, PostgresDialect, sql } from 'kysely';
import { Pool } from 'pg';
import { getConstructionPlans, PgBoss } from 'pg-boss';
import { createControlPlane } from './discovery.ts';
import { assignHermesJob, executeHermesAssignment } from './workbench-execution/index.ts';
import type { OpenAuthorizedExecution } from './workbench-execution/index.ts';
import { WORKBENCH_QUEUE_SCHEMA, WORKBENCH_RUN_QUEUE } from './workbench-queue/index.ts';
import type { WorkbenchRunJob } from './workbench-queue/index.ts';
import { loadWorkbench } from './workbench-runtime.ts';

const host = '127.0.0.1';
const issuer = 'https://127.0.0.1:10074';
const origin = 'https://127.0.0.1:10073';
const audience = 'harniqo-workbench';
const desktopClientId = 'harniqo-desktop';

function database(url: URL) {
  return new Kysely<unknown>({ dialect: new PostgresDialect({ pool: new Pool({ connectionString: url.href, max: 2 }) }) });
}

test('HTTPS workbench authenticates a JWT and accepts an authorized Run with its queue and budget', { timeout: 30_000 }, async () => {
  const adminUrl = new URL(process.env.HARNIQO_TEST_POSTGRES_URL!);
  assert.equal(adminUrl.protocol, 'postgresql:');
  assert.equal(adminUrl.hostname, host);
  assert.equal(adminUrl.pathname, '/harniqo_workbench_route_test');
  assert.equal(adminUrl.search, '');
  const temp = await mkdtemp(join(tmpdir(), 'harniqo-workbench-routes-'));
  const roles = ['harniqo_migrator', 'harniqo_control', 'harniqo_worker'].map(name => {
    const url = new URL(adminUrl.href);
    url.username = name;
    url.password = randomBytes(32).toString('hex');
    return url;
  });
  const [ownerUrl, runtimeUrl, workerUrl] = roles as [URL, URL, URL];
  const admin = database(adminUrl);
  const owner = database(ownerUrl);
  const worker = database(workerUrl);
  const organizationId = randomUUID();
  const projectId = randomUUID();
  const workspaceId = randomUUID();
  const sessionId = randomUUID();
  const principalId = randomUUID();
  const executionId = randomUUID();
  const submission = {
    deployment_revision_id: randomUUID(), configuration_revision_id: randomUUID(),
    input_snapshot_id: randomUUID(), budget_policy_id: randomUUID(), prompt: 'qualification command',
    required_capabilities: [],
  };
  const projectPath = `/v1/organizations/${organizationId}/projects/${projectId}`;
  const sessionPath = `${projectPath}/sessions/${sessionId}`;
  const ca = getCACertificates();
  let provider: ReturnType<typeof createServer> | undefined;
  let workbench: Awaited<ReturnType<typeof loadWorkbench>> | undefined;
  let server: ReturnType<typeof createControlPlane> | undefined;
  let consumer: PgBoss | undefined;
  try {
    for (const role of roles) {
      await sql`CREATE ROLE ${sql.id(role.username)} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD ${sql.lit(role.password)}`.execute(admin);
    }
    await sql`GRANT CREATE ON DATABASE harniqo_workbench_route_test TO harniqo_migrator`.execute(admin);
    await sql.raw(getConstructionPlans(WORKBENCH_QUEUE_SCHEMA)).execute(owner);
    const migrations = await Promise.all([
      './workbench-store/001-first-run.sql', './workbench-access/002-external-identities.sql',
      './workbench-queue/003-workbench-queue.sql', './workbench-execution/004-hermes-execution.sql',
    ].map(path => readFile(new URL(path, import.meta.url), 'utf8')));
    const epoch = randomUUID();
    await owner.transaction().execute(async tx => {
      for (const migration of migrations) await sql.raw(migration).execute(tx);
      await sql`INSERT INTO harniqo.project_memberships VALUES (${organizationId}, ${projectId}, ${principalId}, true, true)`.execute(tx);
      await sql`INSERT INTO harniqo.external_identities (issuer, subject, organization_id, principal_id)
        VALUES (${issuer}, 'authorized', ${organizationId}, ${principalId}),
          (${issuer}, 'no-project', ${organizationId}, ${randomUUID()})`.execute(tx);
      await sql`INSERT INTO harniqo.run_targets (
        organization_id, project_id, principal_id, session_id, workspace_id, deployment_id, deployment_name,
        deployment_revision_id, configuration_revision_id, input_snapshot_id, budget_policy_id, execution_target_id
      ) VALUES (${organizationId}, ${projectId}, ${principalId}, ${sessionId}, ${workspaceId}, ${randomUUID()}, 'Hermes fixture',
        ${submission.deployment_revision_id}, ${submission.configuration_revision_id}, ${submission.input_snapshot_id},
        ${submission.budget_policy_id}, ${executionId})`.execute(tx);
      await sql`INSERT INTO harniqo.hermes_execution_bindings (
        organization_id, project_id, principal_id, execution_target_id, deployment_revision_id,
        workspace_id, runtime_instance_id, harness_release_id, platform_epoch, instance_generation, enabled, ready_until
      ) VALUES (${organizationId}, ${projectId}, ${principalId}, ${executionId}, ${submission.deployment_revision_id},
        ${workspaceId}, ${randomUUID()}, ${randomUUID()}, ${epoch}, 1, true, now() + interval '30 seconds')`.execute(tx);
      await sql`INSERT INTO harniqo.workbench_organization_budgets (organization_id, run_limit, concurrent_limit)
        VALUES (${organizationId}, 1, 1)`.execute(tx);
      await sql`INSERT INTO harniqo.workbench_project_budgets (organization_id, project_id, budget_policy_id, run_limit, concurrent_limit)
        VALUES (${organizationId}, ${projectId}, ${submission.budget_policy_id}, 1, 1)`.execute(tx);
    });
    const keyPath = join(temp, 'key.pem');
    const certPath = join(temp, 'cert.pem');
    execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes',
      '-keyout', keyPath, '-out', certPath, '-days', '1', '-subj', '/CN=harniqo-workbench-test',
      '-addext', 'subjectAltName=IP:127.0.0.1'], { stdio: 'ignore', timeout: 5000 });
    const tls = { key: await readFile(keyPath), cert: await readFile(certPath) };
    setDefaultCACertificates([...ca, tls.cert]);
    const { publicKey, privateKey } = await generateKeyPair('RS256');
    const identityKey = { ...await exportJWK(publicKey), kid: 'fixture', use: 'sig', alg: 'RS256' };
    provider = createServer(tls, (req, res) => {
      if (req.url !== '/jwks') { res.writeHead(404).end(); return; }
      res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({ keys: [identityKey] }));
    });
    await new Promise<void>((resolve, reject) => {
      provider!.once('error', reject);
      provider!.listen(10074, host, resolve);
    });
    const configPath = join(temp, 'workbench.json');
    const urlPath = join(temp, 'database-url');
    const discovery = { issuer, client_id: desktopClientId, required_scopes: ['openid', 'profile'], authorization_code_pkce_required: true };
    const unsafeDatabaseUrl = new URL(runtimeUrl);
    unsafeDatabaseUrl.search = '?sslmode=verify-full&sslmode=disable';
    await writeFile(urlPath, unsafeDatabaseUrl.href, { mode: 0o600 });
    await writeFile(configPath, JSON.stringify({
      access: { organizationId, issuer, jwksUri: issuer + '/jwks', audience, desktopClientId, accessTokenProfile: 'keycloak', algorithm: 'RS256' },
      platform: { platform_id: randomUUID(), platform_epoch: epoch, build_id: 'qualification', software_version: '0.1.0',
        selected_contract_version: '0.1.0', supported_contract_versions: ['0.1.0'], feature_flags: [],
        minimum_component_versions: { desktop: '0.1.0', runner: '0.1.0', execution_agent: '0.1.0' } },
    }));
    await assert.rejects(loadWorkbench(discovery, configPath, urlPath), /invalid_workbench_database_options/u);
    await writeFile(urlPath, runtimeUrl.href);
    workbench = await loadWorkbench(discovery, configPath, urlPath);
    assert.throws(() => createControlPlane(discovery, null, workbench), /workbench_tls_required/u);
    server = createControlPlane(discovery, tls, workbench);
    await server.listen({ host, port: 10073 });
    const jwt = (sub: string, typ = 'Bearer') => new SignJWT({ typ, azp: desktopClientId })
      .setProtectedHeader({ kid: 'fixture', alg: 'RS256', typ: 'JWT' })
      .setIssuer(issuer).setAudience(audience).setSubject(sub).setIssuedAt().setExpirationTime('2 minutes').sign(privateKey);
    const bearer = await jwt('authorized');
    const denied = await jwt('no-project');
    const http = (path: string, token?: string, input?: unknown, key?: string) => fetch(origin + path, {
      method: input === undefined ? 'GET' : 'POST',
      headers: {
        ...(token && { authorization: 'Bearer ' + token }), ...(key && { 'idempotency-key': key }),
        ...(input !== undefined && { 'content-type': 'application/json' }),
      },
      ...(input !== undefined && { body: JSON.stringify(input) }),
      redirect: 'error', signal: AbortSignal.timeout(5000),
    });
    const list = '/v1/workbench/deployments';
    assert.equal((await http('/v1/auth/discovery')).status, 200);
    assert.equal((await http(list)).status, 401);
    assert.equal((await http(list, await jwt('authorized', 'ID'))).status, 401);
    assert.deepEqual(await (await http(list, denied)).json(), { deployments: [] });
    const handshake = { component: 'desktop', build_id: 'qualification', software_version: '0.1.0', supported_contract_versions: ['0.1.0'] };
    assert.equal((await http('/v1/handshakes', bearer, { ...handshake, software_version: '0.0.9' })).status, 409);
    assert.equal((await http('/v1/handshakes', bearer, handshake)).status, 200);
    const deployments = await (await http(list, bearer)).json() as { deployments: { session_id: string }[] };
    assert.equal(deployments.deployments[0]?.session_id, sessionId);
    const key = randomUUID();
    assert.equal((await http(sessionPath + '/runs', denied, submission, key)).status, 403);
    await sql`UPDATE harniqo.hermes_execution_bindings SET platform_epoch = ${randomUUID()}`.execute(owner);
    assert.deepEqual(await (await http(list, bearer)).json(), { deployments: [] });
    assert.equal((await http(sessionPath + '/runs', bearer, submission, key)).status, 503);
    assert.equal((await http(sessionPath + '/run-submissions:lookup', bearer, undefined, key)).status, 404);
    await sql`UPDATE harniqo.hermes_execution_bindings SET platform_epoch = ${epoch}`.execute(owner);
    const accepted = await http(sessionPath + '/runs', bearer, submission, key);
    const receipt = await accepted.json() as { run_id?: string; code?: string };
    assert.equal(accepted.status, 202, receipt.code);
    assert.ok(receipt.run_id);
    const lookup = await http(sessionPath + '/run-submissions:lookup', bearer, undefined, key);
    assert.equal(lookup.status, 200);
    assert.deepEqual(await lookup.json(), receipt);
    assert.equal((await http(projectPath + '/runs/' + receipt.run_id, bearer)).status, 200);
    assert.equal((await http(projectPath + '/runs/' + receipt.run_id, denied)).status, 403);
    const events = await (await http(projectPath + '/runs/' + receipt.run_id + '/events?after=0&limit=4', bearer)).json() as { next_after: number };
    assert.equal(events.next_after, 1);
    const queued = await sql<{ jobs: number; holds: number }>`
      SELECT (SELECT count(*) FROM harniqo_queue.job)::int AS jobs,
        (SELECT count(*) FROM harniqo.workbench_budget_reservations)::int AS holds`.execute(owner);
    assert.deepEqual(queued.rows[0], { jobs: 1, holds: 1 });

    consumer = new PgBoss({ connectionString: workerUrl.href, schema: WORKBENCH_QUEUE_SCHEMA,
      supervise: false, schedule: false, createSchema: false, migrate: false, registerInstance: false });
    await consumer.start();
    const [job] = await consumer.fetch<WorkbenchRunJob>(WORKBENCH_RUN_QUEUE);
    assert.ok(job);
    assert.equal(job.id, receipt.run_id);
    assert.equal(await assignHermesJob(worker, job.data), 'assigned');
    await consumer.complete(WORKBENCH_RUN_QUEUE, job.id);
    const nativeRunId = 'run_' + randomBytes(16).toString('hex');
    const fixtureReply = 'Reply from the controlled execution fixture.';
    const open: OpenAuthorizedExecution = async identity => ({
      identity,
      async assertLive() { return identity.workspaceId === workspaceId; },
      async hasNativeSession() { return true; },
      async confirmTerminal(id) { return { nativeRunId: id, writerExited: true, servedModel: 'fixture-model' }; },
      hermes: {
        async attach(nativeSessionId) { return { nativeSessionId }; },
        async start(input) {
          assert.equal(input.text, submission.prompt);
          return { nativeRunId, replayed: false };
        },
        async inspect(id, nativeSessionId) {
          return { nativeRunId: id, nativeSessionId, status: 'completed', output: fixtureReply };
        },
        async readReply() { return fixtureReply; },
      },
    });
    assert.equal(await executeHermesAssignment(worker, job.data, open), 'pending');
    assert.equal(await executeHermesAssignment(worker, job.data, open), 'terminal');
    const finished = await http(projectPath + '/runs/' + receipt.run_id, bearer);
    assert.equal(finished.status, 200);
    assert.equal((await finished.json() as { run: { state: string } }).run.state, 'succeeded');
    const finalPage = await http(projectPath + '/runs/' + receipt.run_id + '/events?after=0&limit=8', bearer);
    assert.equal(finalPage.status, 200);
    const page = await finalPage.json() as { events: { source?: { type: string; payload: { text?: string } } }[] };
    assert.equal(page.events.find(event => event.source?.type === 'text.delta')?.source?.payload.text, fixtureReply);
    await sql`UPDATE harniqo.hermes_execution_bindings SET ready_until = now() - interval '1 second'`.execute(owner);
    assert.deepEqual(await (await http(list, bearer)).json(), { deployments: [] });
  } finally {
    await server?.close();
    await workbench?.close?.();
    await consumer?.stop();
    if (provider) {
      const instance = provider;
      await new Promise<void>(resolve => { instance.close(() => resolve()); instance.closeAllConnections(); });
    }
    setDefaultCACertificates(ca);
    await Promise.all([owner.destroy(), admin.destroy(), worker.destroy()]);
    await rm(temp, { force: true, recursive: true });
  }
});
