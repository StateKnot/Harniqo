import type { BrowserWindow } from 'electron';
import type { MainRuntimeInfo, RuntimeInfo } from '../shared/interface';
import { APP_URL } from './policy';

/** Native smoke runs in the real mounted renderer, through the same preload used by the UI. */
export async function inspectNativeWindow(window: BrowserWindow, runtime: MainRuntimeInfo, expectPackaged: boolean): Promise<object> {
  const renderer = await window.webContents.executeJavaScript(`(async () => {
    const deadline = performance.now() + 10000;
    while (!document.querySelector('[data-react-mounted="true"]')) {
      if (performance.now() > deadline) throw new Error('REACT_MOUNT_TIMEOUT');
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    return {
      rendered: true,
      title: document.title,
      url: location.href,
      runtime: await window.harniqo.getRuntimeInfo(),
      apiKeys: Object.keys(window.harniqo),
      requireType: typeof window.require,
      processType: typeof window.process,
      connection: document.querySelector('[data-platform-state]').getAttribute('data-platform-state')
    };
  })()`) as {
    rendered: boolean; title: string; url: string; runtime: RuntimeInfo; apiKeys: string[];
    requireType: string; processType: string; connection: string;
  };
  const security = {
    sandbox: renderer.runtime.rendererSecurity.sandboxed,
    contextIsolation: renderer.runtime.rendererSecurity.contextIsolated,
    nodeIntegration: renderer.requireType !== 'undefined',
    webSecurity: true,
    requireUnavailable: renderer.requireType === 'undefined',
    processUnavailable: renderer.processType === 'undefined',
    evidence: {
      sandbox: 'preload.process.sandboxed',
      contextIsolation: 'preload.process.contextIsolated',
      nodeIntegration: 'renderer.require absence; BrowserWindow configured false',
      webSecurity: 'BrowserWindow configured true',
    },
  };
  if (!renderer.rendered || renderer.title !== 'Harniqo' || renderer.url !== APP_URL || renderer.connection !== 'disconnected') throw new Error('RENDERER_IDENTITY_MISMATCH');
  if (renderer.apiKeys.sort().join(',') !== 'getRuntimeInfo,probePlatform,signIn,signOut') throw new Error('PRELOAD_SURFACE_MISMATCH');
  const { rendererSecurity: _rendererSecurity, ...reportedMain } = renderer.runtime;
  if (JSON.stringify(reportedMain) !== JSON.stringify(runtime) || runtime.appVersion !== runtime.version || runtime.isPackaged !== expectPackaged) throw new Error('RUNTIME_IDENTITY_MISMATCH');
  if (!security.sandbox || !security.contextIsolation || security.nodeIntegration || !security.webSecurity || !security.requireUnavailable || !security.processUnavailable) throw new Error('RENDERER_SECURITY_MISMATCH');
  const probe = await inspectPlatformProbe(window);
  return { schemaVersion: 1, status: 'passed', sourceCommit: runtime.sourceCommit, sourceDirty: runtime.sourceDirty, sourceInputHash: runtime.sourceInputHash, rendered: true, runtime, security, renderer: { url: renderer.url, connection: renderer.connection }, probe };
}

async function inspectPlatformProbe(window: BrowserWindow) {
  const origin = process.env['HARNIQO_SMOKE_DISCOVERY_ORIGIN'];
  const badCertificate = process.env['HARNIQO_SMOKE_BAD_CERTIFICATE_ORIGIN'];
  const httpOrigin = process.env['HARNIQO_SMOKE_HTTP_DISCOVERY_ORIGIN'];
  const unconfiguredOrigin = process.env['HARNIQO_SMOKE_UNCONFIGURED_DISCOVERY_ORIGIN'];
  const loginOrigin = process.env['HARNIQO_SMOKE_LOGIN_ORIGIN'];
  const denied = await window.webContents.executeJavaScript(
    `window.harniqo.probePlatform(${JSON.stringify(origin ?? httpOrigin ?? 'https://localhost:10050')})`,
  ) as { error?: string };
  if (denied.error !== 'user_action_required') throw new Error('PROBE_ACTIVATION_MISMATCH');
  const deniedLogin = await window.webContents.executeJavaScript(
    `window.harniqo.signIn(${JSON.stringify(loginOrigin ?? 'https://localhost:10050')})`,
  ) as { error?: string };
  if (deniedLogin.error !== 'user_action_required') throw new Error('LOGIN_ACTIVATION_MISMATCH');
  if (Boolean(origin) !== Boolean(badCertificate)) throw new Error('PROBE_SMOKE_ORIGINS_REQUIRED');
  if (httpOrigin && !httpOrigin.startsWith('http://')) throw new Error('PROBE_SMOKE_HTTP_ORIGIN_REQUIRED');

  const submit = async (address: string, expected: string) => window.webContents.executeJavaScript(`(async () => {
    const input = document.querySelector('#platform-address');
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, ${JSON.stringify(address)});
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.form.requestSubmit();
    const deadline = performance.now() + 7000;
    const section = input.closest('[data-platform-state]');
    const expected = ${JSON.stringify(expected)};
    while (performance.now() < deadline) {
      if (expected === 'discovered' ? section.dataset.platformState === 'discovered'
        : section.querySelector('[data-probe-error]')?.dataset.probeError === expected) return true;
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    return false;
  })()`, true) as Promise<boolean>;
  if (origin && badCertificate) {
    if (!await submit(origin, 'discovered')) throw new Error('PLATFORM_DISCOVERY_SMOKE_FAILED');
    if (!await submit(badCertificate, 'certificate_rejected')) throw new Error('PLATFORM_CERTIFICATE_SMOKE_FAILED');
  }
  if (httpOrigin && !await submit(httpOrigin, 'discovered')) throw new Error('PLATFORM_HTTP_DISCOVERY_SMOKE_FAILED');
  if (unconfiguredOrigin && !await submit(unconfiguredOrigin, 'identity_not_configured')) {
    throw new Error('PLATFORM_UNCONFIGURED_DISCOVERY_SMOKE_FAILED');
  }
  if (loginOrigin) {
    if (!await submit(loginOrigin, 'discovered')) throw new Error('LOGIN_DISCOVERY_SMOKE_FAILED');
    await inspectBrowserSignIn(window, loginOrigin);
  }
  return {
    userActionRequired: true,
    ...(origin ? { discovered: true, certificateRejected: true } : {}),
    ...(httpOrigin ? { httpDiscovered: true } : {}),
    ...(unconfiguredOrigin ? { identityPending: true } : {}),
    ...(loginOrigin ? { browserLogin: true, signedOutLocally: true } : {}),
  };
}

async function inspectBrowserSignIn(window: BrowserWindow, origin: string): Promise<void> {
  const http = origin.startsWith('http://');
  const issuer = process.env['HARNIQO_SMOKE_LOGIN_ISSUER'];
  const clientId = process.env['HARNIQO_SMOKE_LOGIN_CLIENT_ID'];
  if (http) {
    if (!issuer || !clientId) throw new Error('LOGIN_SMOKE_CONFIRMATION_REQUIRED');
    const wrongConfirmations = [undefined, { issuer, clientId: clientId + '-wrong' },
      { issuer: 'https://wrong.example.invalid', clientId }];
    for (const confirmation of wrongConfirmations) {
      const mismatch = await window.webContents.executeJavaScript(
        `window.harniqo.signIn(${JSON.stringify(origin)}, ${JSON.stringify(confirmation)})`, true,
      ) as { error?: string };
      if (mismatch.error !== 'identity_mismatch') throw new Error('LOGIN_CONFIRMATION_BYPASSED');
    }
  }
  const state = await window.webContents.executeJavaScript(`(async () => {
    const setValue = (id, value) => {
      const input = document.querySelector(id);
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, value);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    };
    if (${http}) {
      setValue('#identity-issuer', ${JSON.stringify(issuer)});
      setValue('#identity-client-id', ${JSON.stringify(clientId)});
    }
    document.querySelector('#platform-login').requestSubmit();
    const deadline = performance.now() + 20000;
    while (performance.now() < deadline) {
      const login = document.querySelector('[data-login-state]');
      if (login.dataset.loginState === 'signed_in') return 'signed_in';
      if (login.dataset.loginState === 'error') return login.querySelector('[data-login-error]')?.dataset.loginError;
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    return 'timeout';
  })()`, true);
  if (state !== 'signed_in') throw new Error(`LOGIN_SMOKE_FAILED: ${state}`);
  const signedOut = await window.webContents.executeJavaScript(`(async () => {
    document.querySelector('#platform-sign-out').click();
    const deadline = performance.now() + 2000;
    while (performance.now() < deadline) {
      if (document.querySelector('[data-login-state]')?.dataset.loginState === 'signed_out') return true;
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    return false;
  })()`, true);
  if (!signedOut) throw new Error('LOCAL_SIGN_OUT_SMOKE_FAILED');
}
