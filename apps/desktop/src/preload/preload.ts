import { contextBridge, ipcRenderer } from 'electron';
import type { DesktopInterface, MainRuntimeInfo, RuntimeInfo } from '../shared/interface';

// Sandboxed preloads cannot load local CommonJS modules; keep the wire name type checked without a runtime import.
const channel: typeof import('../shared/interface').RUNTIME_INFO_CHANNEL = 'harniqo:runtime-info';
const desktop: DesktopInterface = Object.freeze({
  getRuntimeInfo: async (): Promise<RuntimeInfo> => {
    const main: MainRuntimeInfo = await ipcRenderer.invoke(channel);
    return Object.freeze({ ...main, rendererSecurity: Object.freeze({ sandboxed: process.sandboxed, contextIsolated: process.contextIsolated }) });
  },
});
contextBridge.exposeInMainWorld('harniqo', desktop);
