import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { nativeConfiguration, privateFile } from '../../deploy/qualification/openclaw/entrypoint.mjs';

const approved = { base_url: 'https://models.example.invalid/v1', model: 'approved-model' };

test('configuration fixes one model, native file tools and secret references', () => {
  const config = nativeConfiguration(approved);
  assert.equal(config.models.providers.harniqo.api, 'openai-completions');
  assert.deepEqual(config.agents.defaults.model, { primary: 'harniqo/approved-model', fallbacks: [] });
  assert.equal(config.models.providers.harniqo.apiKey.source, 'env');
  assert.equal(config.gateway.auth.mode, 'token');
  assert.deepEqual(config.tools.allow, ['read', 'write']);
  assert(config.tools.deny.includes('apply_patch'));
  assert.equal(config.tools.fs.workspaceOnly, true);
  for (const value of [config.cron.enabled, config.plugins.enabled, config.hooks.enabled,
    config.update.checkOnStart, config.models.catalogRefresh.enabled]) assert.equal(value, false);
});

test('credential-shaped config fields and untrusted URL forms are rejected', () => {
  for (const data of [
    { ...approved, api_key: 'synthetic-value' }, { ...approved, model: 'bad\nmodel' },
    ...['http://models.example.invalid/v1', 'https://user:pass@models.example.invalid/v1',
      'https://models.example.invalid/v1?key=synthetic', 'https://models.example.invalid/v1#secret']
      .map((base_url) => ({ ...approved, base_url })),
  ]) assert.throws(() => nativeConfiguration(data));
});

test('qualification budgets cannot silently become unlimited', () => {
  for (const context of [8192, 32769, 3.5, '32768', true]) {
    assert.throws(() => nativeConfiguration({ ...approved, context }));
  }
  for (const output of [0, 127, 1025, 3.5, '512', true]) {
    assert.throws(() => nativeConfiguration({ ...approved, output }));
  }
});

test('private input reader rejects shared modes, symlinks, hardlinks and excess bytes', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'harniqo-openclaw-profile-'));
  const file = path.join(directory, 'secret');
  try {
    fs.writeFileSync(file, 'synthetic-test-value', { mode: 0o600 });
    assert.equal(privateFile(file), 'synthetic-test-value');
    assert.throws(() => privateFile(file, 4));
    fs.chmodSync(file, 0o644); assert.throws(() => privateFile(file));
    fs.chmodSync(file, 0o600);
    fs.symlinkSync(file, path.join(directory, 'symbolic')); assert.throws(() => privateFile(path.join(directory, 'symbolic')));
    fs.linkSync(file, path.join(directory, 'hard')); assert.throws(() => privateFile(file));
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});
