/** Read-only desktop metadata. */
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
  readonly probePlatform: (origin: string) => Promise<PlatformProbe>;
}

export type PlatformProbe = { readonly issuer: string } |
  { readonly error: 'invalid_platform_origin' | 'discovery_unreachable' | 'discovery_timeout'
    | 'discovery_http_status' | 'invalid_discovery' | 'discovery_too_large' | 'identity_not_configured'
    | 'certificate_rejected' | 'probe_busy' | 'user_action_required' };

export const RUNTIME_INFO_CHANNEL = 'harniqo:runtime-info';
export const PLATFORM_PROBE_CHANNEL = 'harniqo:platform-probe';
