import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { readFile, writeFile, readdir, mkdir, rm } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'vite';

const require = createRequire(import.meta.url);
const appDirectory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const repository = path.resolve(appDirectory, '../..');
if (process.version !== 'v24.22.1') throw new Error('Build requires pinned Node.js 24.22.1');
const pkg = JSON.parse(await readFile(path.join(appDirectory, 'package.json'), 'utf8'));
const sourceCommit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repository, encoding: 'utf8' }).trim();
if (!/^[a-f0-9]{40}$/.test(sourceCommit)) throw new Error('Invalid Git source commit');
if (process.env.HARNIQO_SOURCE_SHA && process.env.HARNIQO_SOURCE_SHA !== sourceCommit) throw new Error('HARNIQO_SOURCE_SHA must match checked-out Git commit');
const sourceDirty = execFileSync('git', ['status', '--porcelain', '--untracked-files=normal'], { cwd: repository, encoding: 'utf8' }).trim().length !== 0;
if (process.env.CI && sourceDirty) throw new Error('CI builds require a clean source checkout');

async function files(directory, excluded = new Set()) {
  const result = [];
  for (const item of (await readdir(directory, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name, 'en'))) {
    if (excluded.has(item.name)) continue;
    const target = path.join(directory, item.name);
    if (item.isSymbolicLink()) throw new Error(`Build input must not be a symlink: ${target}`);
    if (item.isDirectory()) result.push(...await files(target, excluded));
    else if (item.isFile()) result.push(target);
  }
  return result;
}
const inputs = await files(appDirectory, new Set(['dist', 'node_modules', 'artifacts', '.test-build', '.DS_Store']));
for (const filename of ['package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', '.npmrc', '.node-version', '.nvmrc', 'tsconfig.base.json', 'LICENSE']) {
  const target = path.join(repository, filename);
  try { await readFile(target); inputs.push(target); } catch (error) { if (error.code !== 'ENOENT') throw error; }
}
const sourceFiles = {};
for (const input of inputs.sort()) sourceFiles[path.relative(repository, input).split(path.sep).join('/')] = createHash('sha256').update(await readFile(input)).digest('hex');
const sourceInputHash = createHash('sha256').update(JSON.stringify(sourceFiles)).digest('hex');
const info = { appId: 'org.harniqo.desktop', appName: 'Harniqo', version: pkg.version, sourceCommit, sourceDirty, sourceInputHash };
const output = path.join(appDirectory, 'dist');
await rm(output, { recursive: true, force: true });
execFileSync(process.execPath, [require.resolve('typescript/bin/tsc'), '-p', 'tsconfig.main.json'], { cwd: appDirectory, stdio: 'inherit' });
await build({
  configFile: false,
  root: path.join(appDirectory, 'src/renderer'),
  base: './',
  build: { outDir: path.join(output, 'renderer'), emptyOutDir: true, sourcemap: false, target: 'es2023' },
});
const manifest = {};
for (const filename of await files(path.join(output, 'renderer'))) {
  const content = await readFile(filename);
  const extension = path.extname(filename);
  const contentType = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml' }[extension];
  if (!contentType) throw new Error(`Unexpected renderer asset type: ${extension}`);
  manifest['/' + path.relative(path.join(output, 'renderer'), filename).split(path.sep).join('/')] = { sha256: createHash('sha256').update(content).digest('hex'), size: content.length, contentType };
}
await mkdir(output, { recursive: true });
await writeFile(path.join(output, 'build-info.json'), JSON.stringify(info, null, 2) + '\n');
await writeFile(path.join(output, 'source-inputs.json'), JSON.stringify({ ...info, files: sourceFiles }, null, 2) + '\n');
await writeFile(path.join(output, 'renderer-manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
console.log(JSON.stringify({ status: 'built', ...info }));
