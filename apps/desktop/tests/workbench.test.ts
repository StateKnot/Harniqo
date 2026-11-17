import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DesktopWorkbench } from '../src/main/workbench';

const org = 'bce7ba01-295e-4ff4-9636-4f1f62368a03';
const project = 'c21d7d9b-5cb4-4c66-baea-dd6728e609af';
const deployment = '12fb6724-e729-4c60-ad11-06138d50bbed';
const revision = '538f76cd-36f0-4931-949e-5b1fa7ca33ae';
const firstSession = 'cbc79b7e-b2d4-40fb-af77-14cc75d63e49';
const otherSession = 'b5562cae-898c-4563-b29e-957684241a09';
const run = '4cc7534d-9121-4765-9ce7-c080e4e77112';
const key = '1886d637-5303-4786-9da6-74a4dbcdc698';
const origin = 'https://platform.example.invalid:10051';
const build = { sourceCommit: 'a'.repeat(40), appVersion: '0.1.0' };

test('an ambiguous submit looks up its original receipt after refresh and reads the persisted reply', async () => {
  let session = firstSession;
  let posts = 0;
  let lookups = 0;
  const projectPath = `/v1/organizations/${org}/projects/${project}`;
  const sessionPath = `${projectPath}/sessions/${firstSession}`;
  const runPath = `${projectPath}/runs/${run}`;
  const transport: typeof fetch = async (input, init) => {
    const target = new URL(String(input));
    assert.equal(target.origin, origin);
    assert.equal(new Headers(init?.headers).get('authorization'), 'Bearer fixture-token');
    assert.equal(init?.credentials, 'omit');
    assert.equal(init?.redirect, 'error');
    switch (target.pathname) {
      case '/v1/handshakes':
        assert.equal(init?.method, 'POST');
        return Response.json({ platform_id: org, platform_epoch: revision,
          selected_contract_version: '0.1.0', supported_contract_versions: ['0.1.0'] });
      case '/v1/workbench/deployments':
        return Response.json({ deployments: [{ id: deployment, name: 'Hermes', harness: 'hermes',
          organization_id: org, project_id: project, session_id: session, configuration_revision_id: revision,
          deployment_revision_id: revision, input_snapshot_id: revision, budget_policy_id: revision }] });
      case sessionPath + '/runs':
        posts++;
        assert.equal(new Headers(init?.headers).get('idempotency-key'), key);
        assert.equal(init?.method, 'POST');
        assert.equal(JSON.parse(String(init?.body)).prompt, 'fixture instruction');
        throw new TypeError('connection lost after server acceptance');
      case sessionPath + '/run-submissions:lookup':
        lookups++;
        assert.equal(new Headers(init?.headers).get('idempotency-key'), key);
        return Response.json({ run_id: run, initial_state: 'accepted' });
      case runPath:
        return Response.json({ last_seq: 3, run: { id: run, state: 'succeeded', session_id: firstSession,
          scope: { organization_id: org, project_id: project }, deployment_revision_id: revision } });
      case runPath + '/events':
        assert.equal(target.searchParams.get('after'), '0');
        return Response.json({ run_id: run, next_after: 3, has_more: false, events: [
          { run_id: run, seq: 1 }, { run_id: run, seq: 2, source: { run_id: run, type: 'text.delta', payload: { text: 'Hermes reply' } } },
          { run_id: run, seq: 3 },
        ] });
      default:
        assert.fail('unexpected business route');
    }
  };
  const workspace = new DesktopWorkbench(origin, 'fixture-token', Date.now() + 60_000, transport, build);
  assert.deepEqual(await workspace.list(), { ok: true, deployments: [{ id: deployment, name: 'Hermes', harness: 'hermes' }] });
  assert.deepEqual(await workspace.start(deployment, 'fixture instruction', key), { ok: false, error: 'business_unavailable' });
  session = otherSession;
  assert.equal((await workspace.list()).ok, true);
  assert.deepEqual(await workspace.start(deployment, 'fixture instruction', key), {
    ok: true, run: { id: run, deploymentId: deployment, state: 'accepted' },
  });
  assert.deepEqual(await workspace.read(run), {
    ok: true, run: { id: run, deploymentId: deployment, state: 'succeeded', reply: 'Hermes reply' },
  });
  assert.equal(posts, 1);
  assert.equal(lookups, 1);
});

test('business access tokens are never used on an HTTP origin or after sign-out', async () => {
  const transport: typeof fetch = async () => assert.fail('must not send credentials');
  const untrusted = new DesktopWorkbench('http://platform.example.invalid:10050', 'fixture-token', Date.now() + 60_000, transport, build);
  assert.deepEqual(await untrusted.list(), { ok: false, error: 'api_origin_required' });
  const workspace = new DesktopWorkbench(origin, 'fixture-token', Date.now() + 60_000, transport, build);
  workspace.close();
  assert.deepEqual(await workspace.list(), { ok: false, error: 'sign_in_required' });
});
