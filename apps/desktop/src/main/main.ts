import { app, BrowserWindow, dialog, ipcMain, Menu, protocol, session, shell } from 'electron';
import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { lstatSync, realpathSync } from 'node:fs';
import path from 'node:path';
import { promisify } from 'node:util';
import { discoverPlatform, DiscoveryError } from '@harniqo/client';
import { PLATFORM_PROBE_CHANNEL, RUNTIME_INFO_CHANNEL, SIGN_IN_CHANNEL, SIGN_OUT_CHANNEL } from '../shared/interface';
import type { BuildInfo, MainRuntimeInfo, PlatformProbe, SignInResult } from '../shared/interface';
import { signIn } from './oidc';
import type { LoginMetadata } from './oidc';
import { APP_URL, CONTENT_SECURITY_POLICY, assertDesktopRequest, resolveAssetPath } from './policy';
import { inspectNativeWindow } from './smoke';

const smoke = process.env['HARNIQO_SMOKE'] === '1';
app.setName('Harniqo');
app.enableSandbox();
protocol.registerSchemesAsPrivileged([{ scheme: 'harniqo', privileges: { standard: true, secure: true, supportFetchAPI: true } }]);

if (smoke) {
  const profile = process.env['HARNIQO_SMOKE_PROFILE'];
  if (!profile || !path.isAbsolute(profile)) throw new Error('PRIVATE_SMOKE_PROFILE_REQUIRED');
  const stat = lstatSync(profile);
  if (!stat.isDirectory() || stat.isSymbolicLink() || realpathSync(profile) !== profile || (process.platform !== 'win32' && ((stat.mode & 0o077) !== 0 || stat.uid !== process.getuid?.()))) throw new Error('PRIVATE_SMOKE_PROFILE_REQUIRED');
  app.setPath('userData', profile);
  app.setPath('sessionData', path.join(profile, 'session'));
}

let window: BrowserWindow | null = null;
let runtime: MainRuntimeInfo;
let platformProbe: AbortController | undefined;
let verifiedPlatform: { origin: string; metadata: LoginMetadata; requiresIdentityConfirmation: boolean } | undefined;
let login: AbortController | undefined;
let smokeFinished = false;
const smokeTimer = smoke ? setTimeout(() => finishSmoke(new Error('NATIVE_SMOKE_TIMEOUT')), 25000) : undefined;

function finishSmoke(error?: Error, receipt?: object): void {
  if (smokeFinished) return;
  smokeFinished = true;
  if (smokeTimer) clearTimeout(smokeTimer);
  process.stdout.write(`HARNIQO_SMOKE_RESULT=${JSON.stringify(error ? { schemaVersion: 1, status: 'failed', error: error.message } : receipt)}\n`, () => app.exit(error ? 1 : 0));
}

async function configureAssets(): Promise<void> {
  Menu.setApplicationMenu(Menu.buildFromTemplate(process.platform === 'darwin'
    ? [{ role: 'appMenu' }, { role: 'editMenu' }, { role: 'windowMenu' }]
    : [{ label: '文件', submenu: [{ role: 'quit' }] }, { role: 'editMenu' }, { role: 'windowMenu' }]));
  const root = path.join(app.getAppPath(), 'dist');
  const build = JSON.parse(await readFile(path.join(root, 'build-info.json'), 'utf8')) as BuildInfo;
  if (build.appId !== 'org.harniqo.desktop' || build.appName !== 'Harniqo' || build.version !== app.getVersion() || !/^[a-f0-9]{40}$/.test(build.sourceCommit) || typeof build.sourceDirty !== 'boolean' || !/^[a-f0-9]{64}$/.test(build.sourceInputHash)) throw new Error('INVALID_BUILD_IDENTITY');
  runtime = Object.freeze({ ...build, appVersion: app.getVersion(), isPackaged: app.isPackaged, platform: process.platform, arch: process.arch, electron: process.versions.electron, chrome: process.versions.chrome, node: process.versions.node });
  const manifest = JSON.parse(await readFile(path.join(root, 'renderer-manifest.json'), 'utf8')) as Record<string, { sha256: string; size: number; contentType: string }>;
  const assets = new Set(Object.keys(manifest));
  if (!assets.has('/index.html') || [...assets].some((key) => resolveAssetPath(`harniqo://app${key}`, 'GET', assets) !== key)) throw new Error('INVALID_ASSET_MANIFEST');
  await protocol.handle('harniqo', async (request) => {
    const key = resolveAssetPath(request.url, request.method, assets);
    if (!key) return new Response(null, { status: 404 });
    const record = manifest[key]!;
    try {
      const content = await readFile(path.join(root, 'renderer', key.slice(1)));
      if (content.length !== record.size || createHash('sha256').update(content).digest('hex') !== record.sha256) return new Response(null, { status: 500 });
      return new Response(Uint8Array.from(content), { headers: { 'Content-Type': record.contentType, 'Content-Security-Policy': CONTENT_SECURITY_POLICY, 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store' } });
    } catch {
      return new Response(null, { status: 500 });
    }
  });
  const desktopSession = session.defaultSession;
  desktopSession.setPermissionCheckHandler(() => false);
  desktopSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  desktopSession.setDevicePermissionHandler(() => false);
  desktopSession.on('will-download', (event) => event.preventDefault());
  desktopSession.webRequest.onBeforeRequest((details, callback) => callback({ cancel: resolveAssetPath(details.url, details.method, assets) === undefined }));
  ipcMain.handle(RUNTIME_INFO_CHANNEL, (event, ...args: unknown[]) => {
    if (!window || window.isDestroyed()) throw new Error('DESKTOP_REQUEST_DENIED');
    assertDesktopRequest(event, args, window.webContents, window.webContents.mainFrame);
    return runtime;
  });
  ipcMain.handle(PLATFORM_PROBE_CHANNEL, async (event, ...args: unknown[]): Promise<PlatformProbe> => {
    if (!window || window.isDestroyed()) throw new Error('DESKTOP_REQUEST_DENIED');
    assertDesktopRequest(event, args, window.webContents, window.webContents.mainFrame, 1);
    const [origin] = args;
    if (typeof origin !== 'string' || origin.length > 2048) return { error: 'invalid_platform_origin' };
    if (platformProbe || login) return { error: 'probe_busy' };
    const probe = new AbortController();
    platformProbe = probe;
    verifiedPlatform = undefined;
    try {
      const metadata = await discoverPlatform(origin, { signal: probe.signal });
      if (probe.signal.aborted) return { error: 'discovery_unreachable' };
      const requiresIdentityConfirmation = new URL(origin).protocol === 'http:';
      verifiedPlatform = { origin, metadata, requiresIdentityConfirmation };
      return { issuer: metadata.issuer, clientId: metadata.client_id, requiresIdentityConfirmation };
    } catch (error) {
      return { error: error instanceof DiscoveryError ? error.code : 'discovery_unreachable' };
    } finally {
      platformProbe = undefined;
    }
  });
  ipcMain.handle(SIGN_IN_CHANNEL, async (event, ...args: unknown[]): Promise<SignInResult> => {
    if (!window || window.isDestroyed()) throw new Error('DESKTOP_REQUEST_DENIED');
    assertDesktopRequest(event, args, window.webContents, window.webContents.mainFrame, 2);
    const loginWindow = window;
    if (login) return { error: 'login_busy' };
    const [origin, confirmation] = args;
    const verified = verifiedPlatform;
    if (!verified || origin !== verified.origin) return { error: 'platform_not_verified' };
    if (verified.requiresIdentityConfirmation && (!confirmation || typeof confirmation !== 'object'
      || !('issuer' in confirmation) || confirmation.issuer !== verified.metadata.issuer
      || !('clientId' in confirmation) || confirmation.clientId !== verified.metadata.client_id)) {
      return { error: 'identity_mismatch' };
    }
    const attempt = new AbortController();
    login = attempt;
    const smokeBrowser = smoke ? process.env['HARNIQO_SMOKE_BROWSER_EXECUTABLE'] : undefined;
    // The HTTP response cannot grant scopes beyond the desktop login defaults.
    const metadata = verified.requiresIdentityConfirmation
      ? { ...verified.metadata, required_scopes: ['openid', 'profile'] }
      : verified.metadata;
    try {
      const account = await signIn(metadata, async url => {
        if (smokeBrowser) {
          await promisify(execFile)(smokeBrowser, [url], { signal: attempt.signal, timeout: 18000 });
          return;
        }
        const authorizationOrigin = new URL(url).origin;
        const consent = await dialog.showMessageBox(loginWindow, {
          type: 'question',
          message: '在系统浏览器登录 Harniqo',
          detail: `控制面 ${new URL(verified.origin).origin}\n身份服务 ${metadata.issuer}\n浏览器将打开 ${authorizationOrigin}`,
          buttons: ['继续', '取消'], defaultId: 1, cancelId: 1, noLink: true,
          signal: attempt.signal,
        });
        if (consent.response !== 0) attempt.abort();
        attempt.signal.throwIfAborted();
        await shell.openExternal(url);
      }, attempt.signal);
      return attempt.signal.aborted ? { error: 'login_cancelled' } : { account };
    } catch {
      return { error: attempt.signal.aborted ? 'login_cancelled' : 'login_failed' };
    } finally {
      login = undefined;
    }
  });
  ipcMain.handle(SIGN_OUT_CHANNEL, (event, ...args: unknown[]): void => {
    if (!window || window.isDestroyed()) throw new Error('DESKTOP_REQUEST_DENIED');
    assertDesktopRequest(event, args, window.webContents, window.webContents.mainFrame);
    login?.abort();
  });
}

function createWindow(): void {
  window = new BrowserWindow({
    title: 'Harniqo', width: 1100, height: 760, minWidth: 640, minHeight: 520,
    show: false, backgroundColor: '#f5f6f8', autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, '../preload/preload.js'),
      sandbox: true, contextIsolation: true, nodeIntegration: false, nodeIntegrationInWorker: false,
      webSecurity: true, allowRunningInsecureContent: false, webviewTag: false, devTools: false,
      navigateOnDragDrop: false, spellcheck: false,
    },
  });
  const current = window;
  current.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  current.webContents.on('will-navigate', (event) => event.preventDefault());
  current.webContents.on('will-frame-navigate', (event) => event.preventDefault());
  current.webContents.on('will-redirect', (event) => event.preventDefault());
  current.webContents.on('will-attach-webview', (event) => event.preventDefault());
  current.webContents.on('preload-error', (_event, _preload, error) => smoke ? finishSmoke(new Error(`PRELOAD_FAILED: ${error.message}`)) : console.error('Harniqo preload failed', error.message));
  current.webContents.on('render-process-gone', (_event, details) => smoke ? finishSmoke(new Error(`RENDERER_GONE: ${details.reason}`)) : app.exit(1));
  current.webContents.on('did-fail-load', (_event, code) => { if (smoke) finishSmoke(new Error(`RENDERER_LOAD_FAILED: ${code}`)); });
  if (!smoke) current.once('ready-to-show', () => current.show());
  current.on('closed', () => {
    if (window !== current) return;
    platformProbe?.abort();
    login?.abort();
    verifiedPlatform = undefined;
    window = null;
  });
  if (smoke) current.webContents.once('did-finish-load', () => {
    void inspectNativeWindow(current, runtime, process.env['HARNIQO_SMOKE_EXPECT_PACKAGED'] === '1').then((receipt) => finishSmoke(undefined, receipt), (error: unknown) => finishSmoke(error instanceof Error ? error : new Error('NATIVE_SMOKE_FAILED')));
  });
  void current.loadURL(APP_URL).catch((error: unknown) => {
    if (smoke) finishSmoke(error instanceof Error ? error : new Error('RENDERER_LOAD_FAILED'));
    else { console.error('Harniqo could not load its local assets'); app.exit(1); }
  });
}

if (!smoke && !app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { if (window) { if (window.isMinimized()) window.restore(); window.focus(); } });
  void app.whenReady().then(async () => { await configureAssets(); createWindow(); }).catch((error: unknown) => {
    if (smoke) finishSmoke(error instanceof Error ? error : new Error('STARTUP_FAILED'));
    else { console.error('Harniqo startup failed', error instanceof Error ? error.message : 'unknown'); app.exit(1); }
  });
  app.on('activate', () => { if (runtime && !window) createWindow(); });
  app.on('window-all-closed', () => { if (smoke) finishSmoke(new Error('WINDOW_CLOSED')); else if (process.platform !== 'darwin') app.quit(); });
}
