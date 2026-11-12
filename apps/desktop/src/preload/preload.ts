import { contextBridge, ipcRenderer } from 'electron';
import type { DesktopInterface, IdentityConfirmation, MainRuntimeInfo, RuntimeInfo } from '../shared/interface';

// Sandboxed preloads cannot load local CommonJS modules; keep the wire name type checked without a runtime import.
const channel: typeof import('../shared/interface').RUNTIME_INFO_CHANNEL = 'harniqo:runtime-info';
const probeChannel: typeof import('../shared/interface').PLATFORM_PROBE_CHANNEL = 'harniqo:platform-probe';
const signInChannel: typeof import('../shared/interface').SIGN_IN_CHANNEL = 'harniqo:sign-in';
const signOutChannel: typeof import('../shared/interface').SIGN_OUT_CHANNEL = 'harniqo:sign-out';
const desktop: DesktopInterface = Object.freeze({
  getRuntimeInfo: async (): Promise<RuntimeInfo> => {
    const main: MainRuntimeInfo = await ipcRenderer.invoke(channel);
    return Object.freeze({ ...main, rendererSecurity: Object.freeze({ sandboxed: process.sandboxed, contextIsolated: process.contextIsolated }) });
  },
  probePlatform: async (origin: string) => {
    if (!navigator.userActivation.isActive) return { error: 'user_action_required' } as const;
    return ipcRenderer.invoke(probeChannel, origin);
  },
  signIn: async (origin: string, confirmation?: IdentityConfirmation) => {
    if (!navigator.userActivation.isActive) return { error: 'user_action_required' } as const;
    return ipcRenderer.invoke(signInChannel, origin, confirmation);
  },
  signOut: async () => {
    if (navigator.userActivation.isActive) await ipcRenderer.invoke(signOutChannel);
  },
});
contextBridge.exposeInMainWorld('harniqo', desktop);
