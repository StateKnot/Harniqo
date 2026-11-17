import { contextBridge, ipcRenderer } from 'electron';
import type { DesktopInterface, IdentityConfirmation, MainRuntimeInfo, RuntimeInfo } from '../shared/interface';

// Sandboxed preloads cannot load local CommonJS modules; keep the wire name type checked without a runtime import.
const channel: typeof import('../shared/interface').RUNTIME_INFO_CHANNEL = 'harniqo:runtime-info';
const probeChannel: typeof import('../shared/interface').PLATFORM_PROBE_CHANNEL = 'harniqo:platform-probe';
const signInChannel: typeof import('../shared/interface').SIGN_IN_CHANNEL = 'harniqo:sign-in';
const signOutChannel: typeof import('../shared/interface').SIGN_OUT_CHANNEL = 'harniqo:sign-out';
const listHarnessDeploymentsChannel: typeof import('../shared/interface').LIST_HARNESS_DEPLOYMENTS_CHANNEL = 'harniqo:list-harness-deployments';
const startHarnessRunChannel: typeof import('../shared/interface').START_HARNESS_RUN_CHANNEL = 'harniqo:start-harness-run';
const readHarnessRunChannel: typeof import('../shared/interface').READ_HARNESS_RUN_CHANNEL = 'harniqo:read-harness-run';
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
  listHarnessDeployments: () => ipcRenderer.invoke(listHarnessDeploymentsChannel),
  startHarnessRun: async (deploymentId: string, input: string, key: string) => {
    if (!navigator.userActivation.isActive) return { ok: false, error: 'user_action_required' } as const;
    return ipcRenderer.invoke(startHarnessRunChannel, deploymentId, input, key);
  },
  readHarnessRun: (runId: string) => ipcRenderer.invoke(readHarnessRunChannel, runId),
});
contextBridge.exposeInMainWorld('harniqo', desktop);
