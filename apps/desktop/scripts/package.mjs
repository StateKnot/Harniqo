import { cp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { packager } from '@electron/packager';

const require = createRequire(import.meta.url);
const directory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
if (process.version !== 'v24.22.1') throw new Error('Packaging requires pinned Node.js 24.22.1');
const build = JSON.parse(await readFile(path.join(directory, 'dist/build-info.json'), 'utf8'));
const pkg = JSON.parse(await readFile(path.join(directory, 'package.json'), 'utf8'));
if (pkg.version !== build.version) throw new Error('Build version is stale; rebuild before packaging');
const sources = JSON.parse(await readFile(path.join(directory, 'dist/source-inputs.json'), 'utf8'));
for (const [relative, expected] of Object.entries(sources.files)) {
  const actual = createHash('sha256').update(await readFile(path.resolve(directory, '../..', relative))).digest('hex');
  if (actual !== expected) throw new Error(`Build input changed; rebuild before packaging: ${relative}`);
}
const artifacts = path.join(directory, 'artifacts');
const stage = path.join(artifacts, 'stage');
await rm(path.join(artifacts, 'package.json'), { force: true });
await rm(stage, { recursive: true, force: true });
await mkdir(stage, { recursive: true });
await cp(path.join(directory, 'dist'), path.join(stage, 'dist'), { recursive: true });
await cp(path.resolve(directory, '../../LICENSE'), path.join(stage, 'LICENSE'));
await writeFile(path.join(stage, 'package.json'), JSON.stringify({ name: pkg.name, productName: build.appName, version: pkg.version, description: pkg.description, author: pkg.author, license: pkg.license, type: 'commonjs', main: pkg.main }, null, 2) + '\n');
let licenses = 'Bundled JavaScript dependencies. Electron and Chromium notices are also included by Electron in the application bundle.\n';
for (const name of ['react', 'react-dom', 'scheduler']) {
  const packageFile = require.resolve(`${name}/package.json`, { paths: [path.dirname(require.resolve('react-dom/package.json'))] });
  licenses += `\n===== ${name} =====\n${await readFile(path.join(path.dirname(packageFile), 'LICENSE'), 'utf8')}\n`;
}
await writeFile(path.join(stage, 'THIRD-PARTY-LICENSES.txt'), licenses);
const bundles = await packager({
  dir: stage, out: path.join(artifacts, 'native'), name: 'Harniqo',
  platform: process.platform, arch: process.arch, electronVersion: pkg.devDependencies.electron,
  download: { cacheRoot: process.env.electron_config_cache, checksums: require('electron/checksums.json') },
  asar: true, prune: false, overwrite: true,
  appBundleId: build.appId, appVersion: build.version,
  appCopyright: 'Copyright (c) 2026 StateKnot',
  executableName: 'Harniqo',
});
if (bundles.length !== 1) throw new Error('Expected exactly one native package');
const bundleDirectory = path.resolve(bundles[0]);
const appPath = process.platform === 'darwin' ? path.join(bundleDirectory, 'Harniqo.app') : bundleDirectory;
const executablePath = process.platform === 'darwin' ? path.join(appPath, 'Contents/MacOS/Harniqo') : path.join(appPath, process.platform === 'win32' ? 'Harniqo.exe' : 'Harniqo');
const manifest = { schemaVersion: 1, platform: process.platform, arch: process.arch, appPath, executablePath, bundleDirectory, signed: false, sourceSha: build.sourceCommit, ...build };
await writeFile(path.join(artifacts, 'package.json'), JSON.stringify(manifest, null, 2) + '\n');
await rm(stage, { recursive: true, force: true });
console.log(JSON.stringify(manifest));
