import test from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync, verify } from 'node:crypto';
import { deviceProof, fixedFileTrace, idle, canonical, liveText } from './probe_openclaw.mjs';

test('device proof signs the exact native v3 field order and challenge', () => {
  const pair = generateKeyPairSync('ed25519');
  const identity = { privateKey: pair.privateKey.export({ type: 'pkcs8', format: 'pem' }) };
  const proof = deviceProof(identity, 'server-nonce', 12345, 'private-token', ['operator.read']);
  const payload = ['v3', proof.id, 'cli', 'cli', 'operator', 'operator.read', '12345', 'private-token', 'server-nonce', 'linux', ''].join('|');
  assert(verify(null, Buffer.from(payload), pair.publicKey, Buffer.from(proof.signature, 'base64url')));
  assert(!verify(null, Buffer.from(payload.replace('server-nonce', 'different')), pair.publicKey, Buffer.from(proof.signature, 'base64url')));
  assert.throws(() => deviceProof(identity, '', 12345, 'token', []));
});

function trace() {
  return [['read', 'qualification-input.json'], ['write', '/data/workspace/qualification-result.json'],
    ['read', 'qualification-result.json']].flatMap(([name, path], index) => [
    { role: 'assistant', content: [{ type: 'toolCall', id: String(index), name, arguments: { path } }] },
    { role: 'toolResult', toolCallId: String(index), isError: false },
  ]);
}

test('fixed-file evidence requires paired successful native calls in order', () => {
  assert(fixedFileTrace(trace()));
  const failed = trace(); failed[3].isError = true; assert(!fixedFileTrace(failed));
  const wrongPath = trace(); wrongPath[2].content[0].arguments.path = '/etc/passwd'; assert(!fixedFileTrace(wrongPath));
  assert(!fixedFileTrace(trace().slice(1)));
  assert(!fixedFileTrace(trace().filter((_, index) => index !== 3)));
  assert(!fixedFileTrace(trace().reverse()));
});

test('idle requires explicit native absence of active work', () => {
  assert(!idle({})); assert(!idle({ sessionInfo: { hasActiveRun: true } }));
  assert(idle({ sessionInfo: { hasActiveRun: false } }));
  assert(idle({ sessionInfo: { activeRunIds: [] } }));
  assert(!idle({ sessionInfo: { activeRunIds: ['running'] } }));
  assert(!idle({ sessionInfo: { hasActiveRun: false, activeRunIds: ['running'] } }));
  assert(!idle({ sessionInfo: { hasActiveRun: true, activeRunIds: [] } }));
  assert.equal(canonical({ b: 1, a: { d: 2, c: 3 } }), '{"a":{"c":3,"d":2},"b":1}');
});

test('cancellation needs actual text for the same run before its terminal event', () => {
  const delta = { event: 'chat', payload: { runId: 'ours', state: 'delta', deltaText: 'visible output' } };
  assert(liveText([delta], 'ours'));
  assert(!liveText([delta], 'other'));
  assert(!liveText([{ ...delta, payload: { ...delta.payload, deltaText: '' } }], 'ours'));
  for (const state of ['final', 'error', 'aborted']) {
    assert(!liveText([delta, { event: 'chat', payload: { runId: 'ours', state } }], 'ours'));
  }
});
