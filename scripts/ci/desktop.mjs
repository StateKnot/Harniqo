// Native build evidence only. This does not certify installation or release signing.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createReadStream, mkdirSync, readFileSync, realpathSync, statSync, statfsSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../../', import.meta.url));
const evidence = path.join(root, 'ci-evidence', 'desktop');
const ci = process.env.GITHUB_ACTIONS === 'true';
const configPaths = ['package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', '.npmrc', '.node-version', 'apps/desktop/package.json'];
const outcomes = ['INSTALL', 'TYPECHECK', 'TEST', 'BUILD', 'PACKAGE', 'SMOKE', 'ARCHIVE'];
mkdirSync(evidence, { recursive: true });

function run(command, args) {
  const result = spawnSync(command, args, { cwd: root, encoding: 'utf8', timeout: 120_000, maxBuffer: 4 * 1024 * 1024 });
  assert.ifError(result.error);
  assert.equal(result.status, 0, `${command} failed: ${result.stderr}`);
  return result.stdout.trim();
}
function readJson(file) { return JSON.parse(readFileSync(file, 'utf8')); }
function writeJson(name, value) { writeFileSync(path.join(evidence, name), `${JSON.stringify(value, null, 2)}\n`); }
function digest(value) { return createHash('sha256').update(value).digest('hex'); }
function inputs() { return Object.fromEntries(configPaths.map((file) => [file, digest(readFileSync(path.join(root, file)))])); }
function source() {
  const commit = run('git', ['rev-parse', 'HEAD']);
  assert.match(commit, /^[a-f0-9]{40}$/);
  const dirty = run('git', ['status', '--porcelain', '--untracked-files=normal']).length !== 0;
  if (process.env.HARNIQO_SOURCE_SHA) assert.equal(commit, process.env.HARNIQO_SOURCE_SHA, 'Declared source differs from checkout');
  if (ci) {
    assert.equal(commit, process.env.GITHUB_SHA, 'Checkout differs from Actions revision');
    assert.equal(dirty, false, 'CI must start and finish with a clean source tree');
  }
  return { commit, dirty };
}
function nativePlatform() {
  const actual = { platform: process.platform, arch: process.arch };
  if (process.env.HARNIQO_EXPECTED_PLATFORM) assert.equal(actual.platform, process.env.HARNIQO_EXPECTED_PLATFORM);
  if (process.env.HARNIQO_EXPECTED_ARCH) assert.equal(actual.arch, process.env.HARNIQO_EXPECTED_ARCH);
  return actual;
}
function withinArtifacts(value) {
  assert.equal(typeof value, 'string', 'Missing package path');
  const file = realpathSync(path.resolve(root, value));
  const base = realpathSync(path.join(root, 'apps', 'desktop', 'artifacts'));
  const relative = path.relative(base, file);
  assert.ok(relative && relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative), 'Package path escapes desktop artifacts');
  return file;
}
function packageManifest() {
  const manifest = readJson(path.join(root, 'apps', 'desktop', 'artifacts', 'package.json'));
  const actual = nativePlatform();
  assert.equal(manifest.platform, actual.platform, 'Package must target the actual host');
  assert.equal(manifest.arch, actual.arch, 'Cross-compilation is not native evidence');
  assert.equal(manifest.signed, false, 'This job only creates unsigned internal build archives');
  const appPath = withinArtifacts(manifest.appPath);
  assert.ok(statSync(appPath).isDirectory());
  const executablePath = withinArtifacts(manifest.executablePath);
  const relative = path.relative(appPath, executablePath);
  assert.ok(relative && relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative), 'Executable escapes package');
  assert.ok(statSync(executablePath).isFile());
  return { manifest, appPath };
}
async function fileSha256(file) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  return hash.digest('hex');
}

async function environment() {
  const pkg = readJson(path.join(root, 'package.json'));
  assert.equal(process.versions.node, pkg.engines.node, 'Use the pinned Node toolchain');
  assert.equal(process.env.HARNIQO_PNPM_VERSION, pkg.engines.pnpm, 'Use the pinned pnpm toolchain');
  const disk = statfsSync(root);
  const freeBytes = disk.bavail * disk.bsize;
  const minimum = ci ? 4 * 1024 ** 3 : 50 * 1024 ** 3;
  const actual = nativePlatform();
  const systemDetail = process.platform === 'darwin'
    ? run('sw_vers', [])
    : process.platform === 'win32'
      ? run('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', '(Get-CimInstance Win32_OperatingSystem).Caption'])
      : readFileSync('/etc/os-release', 'utf8').trim();
  const value = {
    schemaVersion: 1,
    task: 'M1-01',
    recordedAt: new Date().toISOString(),
    source: source(),
    inputs: inputs(),
    toolchain: { node: process.version, pnpm: process.env.HARNIQO_PNPM_VERSION },
    native: { ...actual, type: os.type(), release: os.release(), version: os.version(), systemDetail },
    runner: { label: process.env.HARNIQO_RUNNER_LABEL ?? null, image: process.env.ImageOS ?? null, imageVersion: process.env.ImageVersion ?? null },
    disk: {
      freeBytes,
      requiredBytes: minimum,
      assessment: ci
        ? 'Fresh ephemeral runner; one native package and one archive; no cache copy or persistent build cache; require 4 GiB before installation.'
        : process.env.HARNIQO_DISK_ASSESSMENT ?? null,
    },
    limitations: ['Unsigned internal build archive', 'Native packaged launch does not certify OS installation or a supported release', 'Windows Server runner does not establish Windows 11 desktop support'],
  };
  writeJson('environment.json', value);
  assert.ok(freeBytes >= minimum || (!ci && value.disk.assessment), 'Insufficient disk space: assess and recover task-owned generated files before building');
}

async function archive() {
  const baseline = readJson(path.join(evidence, 'environment.json'));
  const { manifest, appPath } = packageManifest();
  assert.equal(manifest.sourceCommit, baseline.source.commit);
  assert.equal(manifest.sourceDirty, baseline.source.dirty);
  assert.match(manifest.sourceInputHash, /^[a-f0-9]{64}$/);
  const extension = process.platform === 'linux' ? 'tar.gz' : 'zip';
  const name = `harniqo-${process.platform}-${process.arch}-${baseline.source.commit.slice(0, 12)}.${extension}`;
  const target = path.join(evidence, name);
  if (process.platform === 'darwin') run('ditto', ['-c', '-k', '--sequesterRsrc', '--keepParent', appPath, target]);
  else {
    // Git Bash may put GNU tar first on PATH; Windows' bundled bsdtar supports ZIP.
    const tar = process.platform === 'win32' ? path.join(process.env.SystemRoot, 'System32', 'tar.exe') : 'tar';
    run(tar, [process.platform === 'linux' ? '-czf' : '-acf', target, '-C', path.dirname(appPath), path.basename(appPath)]);
  }
  const value = {
    name, bytes: statSync(target).size, sha256: await fileSha256(target),
    platform: process.platform, arch: process.arch, sourceCommit: manifest.sourceCommit,
    sourceDirty: manifest.sourceDirty, sourceInputHash: manifest.sourceInputHash,
    signed: false, notarized: false, installationVerified: false,
  };
  assert.ok(value.bytes > 0);
  writeJson('archive.json', value);
  writeFileSync(path.join(evidence, 'SHA256SUMS'), `${value.sha256}  ${name}\n`);
}

async function linuxSandbox() {
  assert.ok(ci && process.platform === 'linux', 'Sandbox setup is confined to an ephemeral Linux CI runner');
  const { appPath } = packageManifest();
  const helper = withinArtifacts(path.join(appPath, 'chrome-sandbox'));
  run('sudo', ['chown', 'root:root', helper]);
  run('sudo', ['chmod', '4755', helper]);
  const stat = statSync(helper);
  assert.equal(stat.uid, 0);
  assert.equal(stat.mode & 0o7777, 0o4755);
  writeJson('linux-sandbox.json', { method: 'Electron Chromium setuid sandbox helper', uid: stat.uid, mode: '4755', sandboxDisabled: false });
}

async function summary() {
  const checks = Object.fromEntries(outcomes.map((name) => [name.toLowerCase(), process.env[`HARNIQO_${name}_OUTCOME`] ?? 'not-recorded']));
  const report = { schemaVersion: 1, task: 'M1-01', passed: false, checks, recordedAt: new Date().toISOString() };
  try {
    for (const [name, outcome] of Object.entries(checks)) assert.equal(outcome, 'success', `${name} did not pass`);
    const baseline = readJson(path.join(evidence, 'environment.json'));
    assert.deepEqual(source(), baseline.source, 'Source tree changed during the build');
    assert.deepEqual(inputs(), baseline.inputs, 'Lock or build configuration changed during the build');
    const { manifest } = packageManifest();
    const smoke = readJson(path.join(evidence, 'smoke.json'));
    const archive = readJson(path.join(evidence, 'archive.json'));
    const pkg = readJson(path.join(root, 'apps', 'desktop', 'package.json'));
    const buildInputs = readJson(path.join(root, 'apps', 'desktop', 'dist', 'source-inputs.json'));
    assert.equal(manifest.sourceCommit, baseline.source.commit);
    assert.equal(manifest.sourceDirty, baseline.source.dirty);
    assert.match(manifest.sourceInputHash, /^[a-f0-9]{64}$/);
    assert.equal(digest(JSON.stringify(buildInputs.files)), manifest.sourceInputHash);
    for (const [file, hash] of Object.entries(baseline.inputs)) assert.equal(buildInputs.files[file], hash, `Build trace omitted pinned input: ${file}`);
    for (const [file, hash] of Object.entries(buildInputs.files)) {
      const input = path.resolve(root, file);
      const relative = path.relative(root, input);
      assert.ok(relative && relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative), 'Build input escapes repository');
      assert.equal(digest(readFileSync(input)), hash, `Build input changed: ${file}`);
    }
    assert.equal(smoke.status, 'passed');
    assert.equal(smoke.sourceCommit, manifest.sourceCommit);
    assert.equal(smoke.sourceDirty, manifest.sourceDirty);
    assert.equal(smoke.sourceInputHash, manifest.sourceInputHash);
    assert.equal(smoke.rendered, true);
    assert.equal(smoke.runtime.isPackaged, true);
    assert.equal(smoke.runtime.platform, process.platform);
    assert.equal(smoke.runtime.arch, process.arch);
    assert.equal(smoke.runtime.electron, pkg.devDependencies.electron);
    assert.equal(smoke.runtime.appVersion, pkg.version);
    assert.equal(smoke.security.sandbox, true);
    assert.equal(smoke.security.contextIsolation, true);
    assert.equal(smoke.security.nodeIntegration, false);
    assert.equal(smoke.security.webSecurity, true);
    assert.equal(smoke.security.requireUnavailable, true);
    assert.equal(smoke.security.processUnavailable, true);
    assert.equal(archive.sha256, await fileSha256(path.join(evidence, archive.name)));
    writeJson('package.json', manifest);
    writeJson('source-inputs.json', buildInputs);
    Object.assign(report, { passed: true, source: baseline.source, inputs: baseline.inputs, native: baseline.native, archive, scope: 'native-build-packaged-render-and-isolation-only', signed: false, installationVerified: false, releaseAccepted: false });
  } catch (error) {
    report.error = error.message;
    process.exitCode = 1;
  }
  writeJson('summary.json', report);
  console.log(JSON.stringify(report, null, 2));
}

const commands = { environment, archive, summary, 'linux-sandbox': linuxSandbox };
assert.ok(Object.hasOwn(commands, process.argv[2]), 'Expected environment, archive, summary or linux-sandbox');
await commands[process.argv[2]]();
