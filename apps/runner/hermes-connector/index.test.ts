import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { randomUUID, X509Certificate } from 'node:crypto';
import { chmod, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { createHermesConnector } from '../../control-plane/workbench-connector/index.ts';
import type { AssignmentIdentity } from '../../control-plane/workbench-execution/index.ts';
import { initializeConnectorState, openHermesConnector } from './index.ts';
import type { ConnectorBinding, ConnectorAuthority, HermesConnectorOptions } from './index.ts';

test('mTLS server binds one workspace and preserves an uncertain native start across restart', { timeout: 20_000 }, async () => {
  const directory = await realpath(await mkdtemp(join(tmpdir(), 'harniqo-connector-server-')));
  await chmod(directory, 0o700);
  const openssl = (...args: string[]) => execFileSync('openssl', args, { stdio: 'ignore', timeout: 5000 });
  const material = (name: string) => join(directory, name);
  const identity: AssignmentIdentity = {
    organizationId: randomUUID(), projectId: randomUUID(), principalId: randomUUID(),
    runId: randomUUID(), commandId: randomUUID(), attemptId: randomUUID(), workspaceId: randomUUID(),
    executionTargetId: randomUUID(), deploymentRevisionId: randomUUID(), configurationRevisionId: randomUUID(),
    inputSnapshotId: randomUUID(), budgetPolicyId: randomUUID(), runtimeInstanceId: randomUUID(),
    harnessReleaseId: randomUUID(), platformEpoch: randomUUID(), instanceGeneration: 1,
    leaseGeneration: 1, nativeSessionId: 'harniqo_' + randomUUID(),
  };
  const { runId: _run, commandId: _command, attemptId: _attempt, leaseGeneration: _lease,
    nativeSessionId: _session, ...binding } = identity;
  let available = true;
  let starts = 0;
  let running: ReturnType<typeof openHermesConnector> | undefined;
  try {
    openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
      '-keyout', material('ca.key'), '-out', material('ca.pem'), '-subj', '/CN=Connector test CA',
      '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign');
    for (const [name, usage] of [['server', 'serverAuth'], ['client', 'clientAuth']]) {
      openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', material(name + '.key'),
        '-out', material(name + '.csr'), '-subj', '/CN=' + name);
      await writeFile(material(name + '.ext'), 'basicConstraints=critical,CA:FALSE\n'
        + 'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=' + usage + '\nsubjectAltName=IP:127.0.0.1\n');
      openssl('x509', '-req', '-in', material(name + '.csr'), '-CA', material('ca.pem'), '-CAkey', material('ca.key'),
        '-CAcreateserial', '-out', material(name + '.pem'), '-days', '1', '-extfile', material(name + '.ext'));
    }
    const [ca, cert, key, clientCert, clientKey] = await Promise.all(
      ['ca.pem', 'server.pem', 'server.key', 'client.pem', 'client.key'].map(name => readFile(material(name))),
    );
    const state = material('state');
    await mkdir(state, { mode: 0o700 });
    initializeConnectorState(state);
    const authority: ConnectorAuthority = {
      async ready() { return available; },
      async authorize() { return available ? { expiresAt: Date.now() + 30_000 } : null; },
      async hasNativeSession() { return true; },
      async confirmTerminal() { return null; },
    };
    const profile: HermesConnectorOptions = {
      tls: { ca: ca!, cert: cert!, key: key! },
      controllerFingerprint256: new X509Certificate(clientCert!).fingerprint256,
      binding: binding as ConnectorBinding, stateDirectory: state, authority,
      hermes: {
        async attach(nativeSessionId) { return { nativeSessionId }; },
        async start() { starts++; throw new Error('native response was lost'); },
        async inspect() { assert.fail('native ID was never confirmed'); },
        async readReply() { assert.fail('native ID was never confirmed'); },
      },
    };
    const open = createHermesConnector({ origin: 'https://127.0.0.1:10085', ca: ca!, cert: clientCert!, key: clientKey! });
    const listen = async () => {
      running = openHermesConnector(profile);
      await new Promise<void>(resolve => running!.server.listen(10085, '127.0.0.1', resolve));
    };
    await listen();
    const signal = AbortSignal.timeout(10_000);
    assert.deepEqual(await open.ready(signal), { binding, ready: true });
    assert.equal(await open({ ...identity, workspaceId: randomUUID() }, signal), null);
    const session = await open(identity, signal);
    assert.ok(session);
    assert.equal(await session.assertLive(signal), true);
    assert.deepEqual(await session.hermes.attach(identity.nativeSessionId, undefined, signal), { nativeSessionId: identity.nativeSessionId });
    const payload = { nativeSessionId: identity.nativeSessionId, text: 'first command', idempotencyKey: identity.attemptId };
    await assert.rejects(session.hermes.start(payload, signal));
    assert.equal(starts, 1);
    await running!.close();
    await listen();
    const recovered = await open(identity, signal);
    assert.ok(recovered);
    await assert.rejects(recovered.hermes.start(payload, signal));
    assert.equal(starts, 1);
    const other = { ...identity, runId: randomUUID(), commandId: randomUUID(), attemptId: randomUUID(), leaseGeneration: 2 };
    assert.equal(await open(other, signal), null);
    available = false;
    assert.deepEqual(await open.ready(signal), { binding, ready: false });
    assert.equal(await recovered.assertLive(signal), false);
    await assert.rejects(recovered.hasNativeSession(signal), { code: 'access_denied' });
  } finally {
    await running?.close();
    await rm(directory, { force: true, recursive: true });
  }
});
