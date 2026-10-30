import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { install } from './transport_audit.mjs';

const hash = value => createHash('sha256').update(value).digest('hex');
const fail = () => { throw new Error('OpenCode transport qualification configuration rejected'); };

function receipt(filename, record) {
  const bytes = JSON.stringify(record, null, 2) + '\n';
  try {
    fs.writeFileSync(filename, bytes, { mode: 0o600, flag: 'wx' });
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
    const fd = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
    try {
      const stat = fs.fstatSync(fd);
      if (!stat.isFile() || stat.uid !== process.getuid() || stat.nlink !== 1
          || (stat.mode & 0o777) !== 0o600 || stat.size > 8192) fail();
      const existing = JSON.parse(fs.readFileSync(fd, 'utf8'));
      if (Object.keys(existing).sort().join(',') !== Object.keys(record).sort().join(',')
          || Object.entries(record).some(([key, value]) => existing[key] !== value)) fail();
    } finally { fs.closeSync(fd); }
  }
}

// A local plugin uses the fixed upstream config hook, before provider resolution.
// Its only native configuration mutation is provider.options.fetch.
export default async function transportPlugin(_input, options) {
  if (!options || Object.keys(options).sort().join(',') !== 'auditFile,expectedModel,maxRequests,providerId'
      || typeof options.providerId !== 'string' || !/^[a-z][a-z0-9-]{0,31}$/.test(options.providerId)
      || typeof options.expectedModel !== 'string' || typeof options.auditFile !== 'string'
      || !Number.isInteger(options.maxRequests) || options.maxRequests < 1 || options.maxRequests > 6) fail();
  return {
    async config(config) {
      const provider = config.provider?.[options.providerId];
      if (config.model !== options.providerId + '/' + options.expectedModel
          || config.enabled_providers?.length !== 1 || config.enabled_providers[0] !== options.providerId
          || provider?.npm !== '@ai-sdk/openai-compatible'
          || Object.keys(provider?.models ?? {}).length !== 1 || !provider.models[options.expectedModel]
          || typeof provider.options?.baseURL !== 'string') fail();
      const original = provider.options.fetch ?? globalThis.fetch;
      if (typeof original !== 'function') fail();
      const target = { fetch: original };
      const observer = install({ target, auditFile: options.auditFile, expectedModel: options.expectedModel,
        apiBase: provider.options.baseURL, maxRequests: options.maxRequests });
      const ready = path.join(path.dirname(options.auditFile), 'transport-ready.json');
      const record = { schema: 1, kind: 'native_provider_fetch_hook_installed', provider: options.providerId,
        audit_file_sha256: hash(options.auditFile),
        model_sha256: hash(options.expectedModel), base_url_sha256: hash(provider.options.baseURL.replace(/\/$/, '')),
        max_sdk_fetch_sends: options.maxRequests, scope: observer.scope, uid: process.getuid(),
        bun_version: typeof Bun === 'undefined' ? null : Bun.version,
        bun_revision: typeof Bun === 'undefined' ? null : Bun.revision, node_compat_version: process.version };
      receipt(ready, record);
      provider.options.fetch = target.fetch;
    },
  };
}
