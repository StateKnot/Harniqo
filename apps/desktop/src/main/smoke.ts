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
  if (renderer.apiKeys.sort().join(',') !== 'getRuntimeInfo,listHarnessDeployments,probePlatform,readHarnessRun,signIn,signOut,startHarnessRun') {
    throw new Error('PRELOAD_SURFACE_MISMATCH');
  }
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
  const harnessRequested = process.env['HARNIQO_SMOKE_HARNESS_PROMPT'] !== undefined
    || process.env['HARNIQO_SMOKE_WORKBENCH_EXPECT_DENIED'] === '1';
  if (harnessRequested && !loginOrigin) throw new Error('HARNESS_SMOKE_LOGIN_REQUIRED');
  const denied = await window.webContents.executeJavaScript(
    `window.harniqo.probePlatform(${JSON.stringify(origin ?? httpOrigin ?? 'https://localhost:10050')})`,
  ) as { error?: string };
  if (denied.error !== 'user_action_required') throw new Error('PROBE_ACTIVATION_MISMATCH');
  const deniedLogin = await window.webContents.executeJavaScript(
    `window.harniqo.signIn(${JSON.stringify(loginOrigin ?? 'https://localhost:10050')})`,
  ) as { error?: string };
  if (deniedLogin.error !== 'user_action_required') throw new Error('LOGIN_ACTIVATION_MISMATCH');
  const deniedRun = await window.webContents.executeJavaScript(
    "window.harniqo.startHarnessRun('deployment', 'hello', 'key')",
  ) as { error?: string };
  if (deniedRun.error !== 'user_action_required') throw new Error('RUN_ACTIVATION_MISMATCH');
  const anonymous = await window.webContents.executeJavaScript('window.harniqo.listHarnessDeployments()') as { error?: string };
  if (anonymous.error !== 'sign_in_required') throw new Error('WORKBENCH_SIGN_IN_REQUIRED');
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
  let workbench: object | undefined;
  if (loginOrigin) {
    if (!await submit(loginOrigin, 'discovered')) throw new Error('LOGIN_DISCOVERY_SMOKE_FAILED');
    workbench = await inspectBrowserSignIn(window, loginOrigin);
  }
  return {
    userActionRequired: true,
    ...(origin ? { discovered: true, certificateRejected: true } : {}),
    ...(httpOrigin ? { httpDiscovered: true } : {}),
    ...(unconfiguredOrigin ? { identityPending: true } : {}),
    ...(loginOrigin ? { browserLogin: true, signedOutLocally: true } : {}),
    ...(workbench ? { workbench } : {}),
  };
}

async function inspectBrowserSignIn(window: BrowserWindow, origin: string): Promise<object | undefined> {
  const http = origin.startsWith('http://');
  const issuer = process.env['HARNIQO_SMOKE_LOGIN_ISSUER'];
  const clientId = process.env['HARNIQO_SMOKE_LOGIN_CLIENT_ID'];
  const apiOrigin = process.env['HARNIQO_SMOKE_API_ORIGIN'];
  if (apiOrigin && (!/^https:\/\/[^/@?#\\\s]+\/?$/u.test(apiOrigin) || !URL.parse(apiOrigin)?.hostname)) {
    throw new Error('HARNESS_SMOKE_API_ORIGIN_INVALID');
  }
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
      if (${JSON.stringify(apiOrigin)} !== undefined) setValue('#identity-api-origin', ${JSON.stringify(apiOrigin)});
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
  const workbench = await inspectHarnessWorkbench(window);
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
  return workbench;
}

async function inspectHarnessWorkbench(window: BrowserWindow): Promise<object | undefined> {
  const prompt = process.env['HARNIQO_SMOKE_HARNESS_PROMPT'];
  const expected = process.env['HARNIQO_SMOKE_HARNESS_EXPECT_REPLY'];
  const followupPrompt = process.env['HARNIQO_SMOKE_HARNESS_FOLLOWUP_PROMPT'];
  const followupExpected = process.env['HARNIQO_SMOKE_HARNESS_FOLLOWUP_EXPECT_REPLY'];
  const deploymentId = process.env['HARNIQO_SMOKE_HARNESS_DEPLOYMENT_ID'];
  const expectDenied = process.env['HARNIQO_SMOKE_WORKBENCH_EXPECT_DENIED'] === '1';
  if (prompt === undefined && !expectDenied) {
    if (expected !== undefined || followupPrompt !== undefined || followupExpected !== undefined) {
      throw new Error('HARNESS_SMOKE_CONFIGURATION_INVALID');
    }
    return;
  }
  if (expectDenied && (prompt !== undefined || expected !== undefined || followupPrompt !== undefined || followupExpected !== undefined)) {
    throw new Error('HARNESS_SMOKE_CONFIGURATION_INVALID');
  }
  if (deploymentId && !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/u.test(deploymentId)) {
    throw new Error('HARNESS_SMOKE_CONFIGURATION_INVALID');
  }
  const catalog = await window.webContents.executeJavaScript(`(async () => {
    const deadline = performance.now() + 15000;
    while (performance.now() < deadline) {
      const workspace = document.querySelector('[data-catalog-state]');
      if (workspace?.dataset.catalogState === 'error') return { status: 'error' };
      if (workspace?.dataset.catalogState === 'loaded') {
        const select = document.querySelector('#harness-deployment');
        return {
          status: 'loaded',
          deployments: select ? Array.from(select.options).filter(option => option.value).map(option => option.value) : [],
          formPresent: Boolean(document.querySelector('#harness-submit'))
        };
      }
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    return { status: 'timeout' };
  })()`) as { status: string; deployments?: string[]; formPresent?: boolean };
  if (catalog.status !== 'loaded' || !catalog.deployments) throw new Error('HARNESS_CATALOG_SMOKE_FAILED');
  if (expectDenied) {
    if (catalog.deployments.length !== 0 || catalog.formPresent) throw new Error('HARNESS_UNAUTHORIZED_CATALOG_VISIBLE');
    return { accessDenied: true, deploymentsVisible: 0 };
  }
  const selected = deploymentId ?? (catalog.deployments.length === 1 ? catalog.deployments[0] : undefined);
  if (!selected || !catalog.deployments.includes(selected) || !catalog.formPresent) {
    throw new Error('HARNESS_SMOKE_DEPLOYMENT_REQUIRED');
  }
  const turns = [[prompt, expected]];
  if (followupPrompt !== undefined || followupExpected !== undefined) turns.push([followupPrompt, followupExpected]);
  if (turns.some(([input, reply]) => !input?.trim() || input.length > 16384 || !reply || reply.length > 256 * 1024)) {
    throw new Error('HARNESS_SMOKE_CONFIGURATION_INVALID');
  }
  const runs: { runId: string; state: 'succeeded'; replyMatched: true }[] = [];
  for (const [input, reply] of turns) {
    const previousRunId = runs.at(-1)?.runId;
    const result = await window.webContents.executeJavaScript(`(async () => {
      const select = document.querySelector('#harness-deployment');
      const text = document.querySelector('#harness-input');
      const button = document.querySelector('#harness-submit');
      if (!select || !text || !button || select.disabled || text.disabled) return { status: 'form_unavailable' };
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, ${JSON.stringify(selected)});
      select.dispatchEvent(new Event('change', { bubbles: true }));
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(text, ${JSON.stringify(input)});
      text.dispatchEvent(new Event('input', { bubbles: true }));
      await new Promise(resolve => setTimeout(resolve, 0));
      if (button.disabled) return { status: 'form_unavailable' };
      text.form.requestSubmit();
      const deadline = performance.now() + 95000;
      while (performance.now() < deadline) {
        const section = document.querySelector('[data-run-state]');
        const runId = section?.dataset.runId;
        if (runId && runId !== ${JSON.stringify(previousRunId)}) {
          const state = section.dataset.runState;
          if (state === 'succeeded') {
            return { status: state, runId, replyMatched: section.querySelector('.workbench-reply p')?.textContent === ${JSON.stringify(reply)} };
          }
          if (state === 'failed' || state === 'cancelled') return { status: state };
          if (section.querySelector('.workbench-notice')) return { status: 'status_unavailable' };
        }
        if (document.querySelector('.workbench > .probe-error') || document.querySelector('.workbench > .workbench-notice')) {
          return { status: 'submission_unconfirmed' };
        }
        await new Promise(resolve => setTimeout(resolve, 50));
      }
      return { status: 'timeout' };
    })()`, true) as { status: string; runId?: string; replyMatched?: boolean };
    if (result.status !== 'succeeded' || !result.runId || !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/u.test(result.runId)
      || !result.replyMatched) throw new Error('HARNESS_DIALOGUE_SMOKE_FAILED');
    runs.push({ runId: result.runId, state: 'succeeded', replyMatched: true });
  }
  return { deploymentId: selected, runs };
}
