import { spawn } from 'node:child_process';
import { mkdtemp, chmod, mkdir, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const directory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2).filter((arg) => arg !== '--');
let packaged = false;
let receiptPath;
let executable;
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--packaged') packaged = true;
  else if (args[i] === '--receipt' && args[i + 1]) receiptPath = path.resolve(args[++i]);
  else if (args[i] === '--app' && args[i + 1]) { executable = path.resolve(args[++i]); packaged = true; }
  else throw new Error(`Unknown smoke argument: ${args[i]}`);
}
if (packaged && !executable) executable = JSON.parse(await readFile(path.join(directory, 'artifacts/package.json'), 'utf8')).executablePath;
if (!packaged) executable = require('electron');
const expected = JSON.parse(await readFile(path.join(directory, 'dist/build-info.json'), 'utf8'));
const createdProfile = await mkdtemp(path.join(os.tmpdir(), 'harniqo-native-smoke-'));
let profile = createdProfile;
let output = '';
let errorOutput = '';
let timedOut = false;
let result;
try {
  profile = await realpath(createdProfile);
  if (process.platform !== 'win32') await chmod(profile, 0o700);
  await mkdir(path.join(profile, 'session'));
  const environment = { ...process.env, HARNIQO_SMOKE: '1', HARNIQO_SMOKE_PROFILE: profile, HARNIQO_SMOKE_EXPECT_PACKAGED: packaged ? '1' : '0' };
  // Avoid inherited Electron/Node debugging or run-as-Node modes changing what the smoke actually exercises.
  for (const key of ['ELECTRON_RUN_AS_NODE', 'NODE_OPTIONS', 'NODE_INSPECT_RESUME_ON_START', 'ELECTRON_ENABLE_LOGGING']) delete environment[key];
  const child = spawn(executable, packaged ? [] : [directory], { cwd: directory, env: environment, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: false });
  const timer = setTimeout(() => { timedOut = true; child.kill('SIGKILL'); }, 35000);
  child.stdout.on('data', (chunk) => { output = (output + chunk.toString()).slice(-131072); });
  child.stderr.on('data', (chunk) => { errorOutput = (errorOutput + chunk.toString()).slice(-32768); });
  let exitCode;
  try { exitCode = await new Promise((resolve, reject) => { child.once('error', reject); child.once('close', resolve); }); }
  finally { clearTimeout(timer); }
  const records = output.split(/\r?\n/).filter((line) => line.startsWith('HARNIQO_SMOKE_RESULT='));
  if (timedOut || exitCode !== 0 || records.length !== 1) {
    const failure = records.length === 1 ? JSON.parse(records[0].slice('HARNIQO_SMOKE_RESULT='.length)) : undefined;
    const reason = typeof failure?.error === 'string' && /^[A-Z0-9_]+(?:: [a-z_]+)?$/.test(failure.error)
      ? failure.error : errorOutput;
    throw new Error(`Native application failed (exit=${exitCode}, timeout=${timedOut}). ${reason}`);
  }
  result = JSON.parse(records[0].slice('HARNIQO_SMOKE_RESULT='.length));
  if (result.status !== 'passed' || !result.rendered || result.runtime?.isPackaged !== packaged) throw new Error('Native application did not pass smoke');
  for (const key of Object.keys(expected)) if (result.runtime[key] !== expected[key]) throw new Error(`Native package identity differs from current build: ${key}`);
  if (result.runtime.platform !== process.platform || result.runtime.arch !== process.arch) throw new Error('Native runtime platform or architecture mismatch');
  console.log(JSON.stringify(result, null, 2));
} catch (error) {
  result = { schemaVersion: 1, status: 'failed', error: error instanceof Error ? error.message : String(error) };
  console.error(JSON.stringify(result, null, 2));
  process.exitCode = 1;
} finally {
  try {
    if (receiptPath) { await mkdir(path.dirname(receiptPath), { recursive: true }); await writeFile(receiptPath, JSON.stringify(result, null, 2) + '\n'); }
  } finally {
    await rm(createdProfile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
}
