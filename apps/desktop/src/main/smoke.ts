import type { BrowserWindow } from 'electron';
import type { MainRuntimeInfo, RuntimeInfo } from '../shared/interface';
import { APP_URL } from './policy';

/** Native smoke runs in the real mounted renderer, through the same read-only preload used by the UI. */
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
  if (renderer.apiKeys.length !== 1 || renderer.apiKeys[0] !== 'getRuntimeInfo') throw new Error('PRELOAD_SURFACE_MISMATCH');
  const { rendererSecurity: _rendererSecurity, ...reportedMain } = renderer.runtime;
  if (JSON.stringify(reportedMain) !== JSON.stringify(runtime) || runtime.appVersion !== runtime.version || runtime.isPackaged !== expectPackaged) throw new Error('RUNTIME_IDENTITY_MISMATCH');
  if (!security.sandbox || !security.contextIsolation || security.nodeIntegration || !security.webSecurity || !security.requireUnavailable || !security.processUnavailable) throw new Error('RENDERER_SECURITY_MISMATCH');
  return { schemaVersion: 1, status: 'passed', sourceCommit: runtime.sourceCommit, sourceDirty: runtime.sourceDirty, sourceInputHash: runtime.sourceInputHash, rendered: true, runtime, security, renderer: { url: renderer.url, connection: renderer.connection } };
}
