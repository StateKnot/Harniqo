/** Read-only desktop metadata. No credentials, paths, or arbitrary IPC are exposed. */
export interface BuildInfo {
  readonly appId: 'org.harniqo.desktop';
  readonly appName: 'Harniqo';
  readonly version: string;
  readonly sourceCommit: string;
  readonly sourceDirty: boolean;
  readonly sourceInputHash: string;
}

export interface RuntimeInfo extends BuildInfo {
  readonly appVersion: string;
  readonly isPackaged: boolean;
  readonly platform: string;
  readonly arch: string;
  readonly electron: string;
  readonly chrome: string;
  readonly node: string;
  readonly rendererSecurity: {
    readonly sandboxed: boolean;
    readonly contextIsolated: boolean;
  };
}

export type MainRuntimeInfo = Omit<RuntimeInfo, 'rendererSecurity'>;

export interface DesktopInterface {
  readonly getRuntimeInfo: () => Promise<RuntimeInfo>;
}

export const RUNTIME_INFO_CHANNEL = 'harniqo:runtime-info';
