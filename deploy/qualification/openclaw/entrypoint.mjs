import fs from 'node:fs';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export function privateFile(path, maximum = 16384) {
  const descriptor = fs.openSync(path, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    const stat = fs.fstatSync(descriptor);
    if (!stat.isFile() || stat.uid !== process.getuid() || (stat.mode & 0o077) || stat.nlink !== 1 || stat.size > maximum) {
      throw new Error('Private configuration ownership, mode, type or size rejected');
    }
    const value = fs.readFileSync(descriptor);
    if (value.length > maximum) throw new Error('Private configuration exceeded read bound');
    return value.toString('utf8');
  } finally { fs.closeSync(descriptor); }
}

export function nativeConfiguration(model) {
  if (!model || typeof model !== 'object' || Array.isArray(model)
      || Object.keys(model).some((key) => !['base_url', 'model', 'context', 'output'].includes(key))
      || typeof model.model !== 'string' || !/^[A-Za-z0-9_.:/-]{1,200}$/.test(model.model)) {
    throw new Error('Unexpected model configuration');
  }
  const url = new URL(model.base_url);
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) {
    throw new Error('Model endpoint must be an explicit HTTPS API prefix');
  }
  const context = model.context ?? 32768;
  const output = model.output ?? 1024;
  if (!Number.isInteger(context) || context < 16384 || context > 32768
      || !Number.isInteger(output) || output < 128 || output > 1024) {
    throw new Error('Qualification context/output budget rejected');
  }
  const modelReference = 'harniqo/' + model.model;
  const reference = (id) => ({ source: 'env', provider: 'qualification', id });
  return {
    gateway: {
      mode: 'local', bind: 'lan', port: 18789,
      auth: { mode: 'token', token: reference('HARNIQO_GATEWAY_TOKEN') },
      controlUi: { enabled: false }, terminal: { enabled: false },
      cliAgents: { enabled: false }, uploads: { enabled: false }, reload: { mode: 'off' },
    },
    secrets: { providers: { qualification: {
      source: 'env', allowlist: ['HARNIQO_MODEL_API_KEY', 'HARNIQO_GATEWAY_TOKEN'],
    } } },
    models: {
      mode: 'replace', catalogRefresh: { enabled: false },
      providers: { harniqo: {
        baseUrl: url.href.replace(/\/$/, ''), api: 'openai-completions',
        apiKey: reference('HARNIQO_MODEL_API_KEY'), auth: 'api-key', timeoutSeconds: 45,
        models: [{ id: model.model, name: model.model, input: ['text'], reasoning: false,
          contextWindow: context, contextTokens: 16384, maxTokens: output,
          compat: { supportsUsageInStreaming: true, supportsDeveloperRole: false } }],
      } },
    },
    agents: { defaults: {
      workspace: '/data/workspace', cwd: '/data/workspace', skills: [],
      skipBootstrap: true, contextInjection: 'never',
      model: { primary: modelReference, fallbacks: [] }, utilityModel: '',
      models: { [modelReference]: { params: { maxTokens: output } } },
      thinkingDefault: 'off', reasoningDefault: 'off', elevatedDefault: 'off',
      maxConcurrent: 1, timeoutSeconds: 90,
    } },
    tools: { profile: 'full', allow: ['read', 'write'], deny: ['edit', 'apply_patch'],
      fs: { workspaceOnly: true }, elevated: { enabled: false },
      loopDetection: { enabled: true } },
    plugins: { enabled: false }, mcp: { servers: {} }, hooks: { enabled: false },
    cron: { enabled: false }, browser: { enabled: false },
    update: { checkOnStart: false, auto: { enabled: false } },
    discovery: { mdns: { mode: 'off' } }, telemetry: { enabled: false },
    // This release always redacts tools; the old redactSensitive key is invalid.
    logging: { level: 'warn', consoleLevel: 'warn', file: '/data/evidence/gateway.log', maxFileBytes: 4 * 1024 * 1024 },
  };
}

function privateDirectory(path) {
  const stat = fs.lstatSync(path);
  if (!stat.isDirectory() || stat.uid !== process.getuid() || (stat.mode & 0o077)) {
    throw new Error('Private runtime directory rejected');
  }
}

export function launch() {
  process.umask(0o077);
  for (const path of ['/data/openclaw', '/data/workspace', '/data/evidence']) privateDirectory(path);
  const model = JSON.parse(privateFile('/run/config/model.json'));
  const key = privateFile('/run/secrets/model-api-key', 8192).trim();
  const token = privateFile('/run/secrets/api-token', 8192).trim();
  if (!key || /\s|[\x00-\x1f]/.test(key) || !/^[A-Za-z0-9_-]{32,128}$/.test(token)) {
    throw new Error('Private credential shape rejected');
  }
  const configuration = nativeConfiguration(model);
  const path = '/data/openclaw/openclaw.json';
  const temporary = path + '.new-' + process.pid;
  fs.writeFileSync(temporary, JSON.stringify(configuration, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
  fs.renameSync(temporary, path);
  const environment = {
    ...process.env,
    HOME: '/data/openclaw', OPENCLAW_STATE_DIR: '/data/openclaw', OPENCLAW_CONFIG_PATH: path,
    OPENCLAW_AGENT_DIR: '/data/openclaw/agents/main/agent',
    OPENCLAW_NO_AUTO_UPDATE: '1', OPENCLAW_DISABLE_BONJOUR: '1', OPENCLAW_SKIP_CANVAS_HOST: '1',
    HARNIQO_MODEL_API_KEY: key, HARNIQO_GATEWAY_TOKEN: token,
    HARNIQO_EXPECTED_MODEL: model.model, HARNIQO_MODEL_API_BASE: model.base_url,
    HARNIQO_AUDIT_FILE: '/data/evidence/transport.jsonl',
    NODE_OPTIONS: '--import=/opt/harniqo/transport_audit.mjs',
  };
  const command = process.argv.includes('--validate')
    ? ['config', 'validate'] : ['gateway', '--port', '18789', '--bind', 'lan'];
  const child = spawn(process.execPath, ['/app/openclaw.mjs', ...command], { env: environment, stdio: 'inherit' });
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(signal, () => child.kill(signal));
  child.on('error', () => { process.exitCode = 1; });
  child.on('exit', (code, signal) => { process.exitCode = code ?? (signal ? 1 : 0); });
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) launch();
