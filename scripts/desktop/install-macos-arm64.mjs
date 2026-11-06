import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { lstat, mkdir, open, readFile, realpath, rm } from 'node:fs/promises';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const repository = path.resolve(import.meta.dirname, '../..');
const desktop = path.join(repository, 'apps/desktop');
const run = (command, args, options = {}) =>
  execFileSync(command, args, { cwd: repository, encoding: 'utf8', timeout: 45000, ...options }).trim();
const readJson = async (file) => JSON.parse(await readFile(file, 'utf8'));
const hash = (bytes) => createHash('sha256').update(bytes).digest('hex');
const fileHash = async (file) => hash(await readFile(file));
const appArchive = (app) => path.join(app, 'Contents/Resources/app.asar');

const [receiptPath] = process.argv.slice(2);
assert.ok(process.argv.length === 3 && receiptPath && path.isAbsolute(receiptPath),
  'Usage: node scripts/desktop/install-macos-arm64.mjs /absolute/path/to/new-receipt.json');
assert.equal(process.platform, 'darwin');
assert.equal(process.arch, 'arm64');
assert.equal(process.version, 'v24.22.1');
assert.equal(run('/usr/bin/sw_vers', ['-productVersion']).split('.')[0], '26',
  'Use the M1 macOS 26 arm64 acceptance device');
assert.ok((await realpath(path.dirname(receiptPath))) === path.dirname(receiptPath),
  'The receipt parent must be an existing canonical path');

const receipt = await open(receiptPath, 'wx', 0o600);
const result = { schemaVersion: 1, status: 'failed', scope: 'macos-arm64-user-bundle-install' };
let installDirectory;
let ownedDirectory;
try {
  const bundle = path.join(desktop, 'artifacts/native/Harniqo-darwin-arm64/Harniqo.app');
  assert.equal(await realpath(bundle), bundle, 'Use the real local package output');
  const manifest = await readJson(path.join(desktop, 'artifacts/package.json'));
  const expected = await readJson(path.join(desktop, 'dist/build-info.json'));
  const inputs = await readJson(path.join(desktop, 'dist/source-inputs.json'));
  assert.equal(manifest.appPath, bundle);
  assert.equal(manifest.platform, 'darwin');
  assert.equal(manifest.arch, 'arm64');
  assert.match(expected.sourceCommit, /^[a-f0-9]{40}$/);
  assert.match(expected.version, /^\d+\.\d+\.\d+$/);
  assert.equal(expected.sourceDirty, false);
  assert.equal(expected.appId, 'org.harniqo.desktop');
  for (const [key, value] of Object.entries(expected)) {
    assert.equal(manifest[key], value, `Native package manifest mismatch: ${key}`);
    assert.equal(inputs[key], value, `Build source manifest mismatch: ${key}`);
  }
  assert.equal(hash(JSON.stringify(inputs.files)), expected.sourceInputHash);
  run('/usr/bin/git', ['merge-base', '--is-ancestor', expected.sourceCommit, 'HEAD']);
  for (const [name, digest] of Object.entries(inputs.files)) {
    const relative = path.relative(repository, path.resolve(repository, name));
    assert.equal(relative, name, 'Expected a repository-relative input');
    assert.ok(relative !== '..' && !relative.startsWith('../'));
    const committed = execFileSync('/usr/bin/git', ['show', `${expected.sourceCommit}:${relative}`],
      { cwd: repository, timeout: 10000 });
    assert.equal(hash(committed), digest, `Build input differs from the recorded commit: ${name}`);
    assert.equal(await fileHash(path.join(repository, relative)), digest, `Build input changed: ${name}`);
  }
  const require = createRequire(path.join(desktop, 'package.json'));
  const packagerRequire = createRequire(require.resolve('@electron/packager'));
  const { extractFile } = await import(pathToFileURL(packagerRequire.resolve('@electron/asar')).href);
  const embedded = JSON.parse(extractFile(appArchive(bundle), 'dist/build-info.json'));
  assert.deepEqual(embedded, expected, 'Bundled source provenance differs from the verified build');
  Object.assign(result, expected, {
    osVersion: run('/usr/bin/sw_vers', ['-productVersion']),
    osBuild: run('/usr/bin/sw_vers', ['-buildVersion']),
    arch: process.arch,
    sourceAsarSha256: await fileHash(appArchive(bundle)),
    signingMode: 'ad-hoc-local',
    hardenedRuntime: false,
    developerIdSigned: false,
    notarized: false,
  });

  const applications = path.join(await realpath(os.homedir()), 'Applications');
  await mkdir(applications, { recursive: true });
  assert.equal(await realpath(applications), applications, 'Refuse a redirected Applications directory');
  installDirectory = path.join(applications, `Harniqo Internal ${expected.version} (${expected.sourceCommit.slice(0, 12)})`);
  // Reserve a fresh directory before copying. Never replace an existing installation.
  await mkdir(installDirectory, { mode: 0o700 });
  ownedDirectory = await lstat(installDirectory);
  const installed = path.join(installDirectory, 'Harniqo.app');
  run('/usr/bin/ditto', [bundle, installed]);
  for (const notice of ['LICENSE', 'LICENSES.chromium.html']) {
    run('/usr/bin/ditto', [path.join(path.dirname(bundle), notice), path.join(installDirectory, notice)]);
  }
  const { sign } = await import(pathToFileURL(packagerRequire.resolve('@electron/osx-sign')).href);
  await sign({
    app: installed, identity: '-', identityValidation: false,
    preAutoEntitlements: false, preEmbedProvisioningProfile: false,
    // Keep the engineering bundle's local runtime policy. The Developer ID
    // distribution flow must apply and verify its own hardened-runtime policy.
    optionsForFile: () => ({ timestamp: 'none', hardenedRuntime: false }),
  });
  run('/usr/bin/codesign', ['--verify', '--deep', '--strict', installed]);
  const signature = spawnSync('/usr/bin/codesign', ['--display', '--verbose=2', installed],
    { encoding: 'utf8', timeout: 45000 });
  assert.equal(signature.status, 0);
  assert.match(signature.stderr, /^Signature=adhoc$/m);
  assert.match(signature.stderr, /^TeamIdentifier=not set$/m);
  const executable = path.join(installed, 'Contents/MacOS/Harniqo');
  assert.equal(run('/usr/bin/lipo', ['-archs', executable]), 'arm64');
  result.installedAsarSha256 = await fileHash(appArchive(installed));
  assert.equal(result.installedAsarSha256, result.sourceAsarSha256);
  result.installedPath = installed;
  result.nativeSmoke = JSON.parse(run(process.execPath,
    [path.join(desktop, 'scripts/smoke.mjs'), '--app', executable]));
  assert.equal(result.nativeSmoke.sourceCommit, expected.sourceCommit);
  assert.equal(result.nativeSmoke.sourceInputHash, expected.sourceInputHash);
  assert.equal(await fileHash(appArchive(bundle)), result.sourceAsarSha256);
  result.status = 'passed';
} catch (error) {
  result.error = error instanceof Error ? error.message : String(error);
  console.error(result.error);
  process.exitCode = 1;
  if (ownedDirectory) {
    try {
      const current = await lstat(installDirectory);
      assert.equal(current.dev, ownedDirectory.dev);
      assert.equal(current.ino, ownedDirectory.ino);
      await rm(installDirectory, { recursive: true });
      result.failedInstallRemoved = true;
    } catch (cleanupError) {
      result.cleanupError = cleanupError.message;
    }
  }
} finally {
  try {
    await receipt.writeFile(JSON.stringify(result, null, 2) + '\n');
  } finally {
    await receipt.close();
  }
}
if (result.status === 'passed') console.log(result.installedPath);
