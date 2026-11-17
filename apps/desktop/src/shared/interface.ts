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
  readonly signIn: (origin: string, confirmation?: IdentityConfirmation) => Promise<SignInResult>;
  readonly signOut: () => Promise<void>;
  readonly listHarnessDeployments: () => Promise<WorkspaceResult<{ deployments: HarnessDeployment[] }>>;
  readonly startHarnessRun: (deploymentId: string, input: string, key: string) => Promise<WorkspaceResult<{ run: HarnessRun }>>;
  readonly readHarnessRun: (runId: string) => Promise<WorkspaceResult<{ run: HarnessRun }>>;
}

export type WorkspaceResult<T> = ({ ok: true } & T) | { ok: false; error: string };

export interface HarnessDeployment {
  id: string;
  name: string;
  harness: 'hermes';
}

export const HARNESS_RUN_STATES = [
  'accepted', 'queued', 'provisioning', 'running', 'waiting_approval', 'waiting_input', 'stopping',
  'reconciling', 'succeeded', 'failed', 'cancelled',
] as const;

export interface HarnessRun {
  id: string;
  deploymentId: string;
  state: typeof HARNESS_RUN_STATES[number];
  reply?: string;
  message?: string;
}

export type PlatformProbe = { readonly issuer: string; readonly clientId: string; readonly requiresIdentityConfirmation: boolean } |
  { readonly error: 'invalid_platform_origin' | 'discovery_unreachable' | 'discovery_timeout'
    | 'discovery_http_status' | 'invalid_discovery' | 'discovery_too_large' | 'identity_not_configured'
    | 'certificate_rejected' | 'probe_busy' | 'user_action_required' };

export interface IdentityConfirmation {
  readonly issuer: string;
  readonly clientId: string;
  readonly apiOrigin?: string;
}

export interface SignedInAccount {
  readonly issuer: string;
  readonly subject: string;
  readonly displayName: string;
  readonly expiresAt: number;
}

export type SignInResult = { readonly account: SignedInAccount } |
  { readonly error: 'user_action_required' | 'identity_mismatch' | 'platform_not_verified'
    | 'login_busy' | 'login_failed' | 'login_cancelled' | 'invalid_api_origin' };

export const RUNTIME_INFO_CHANNEL = 'harniqo:runtime-info';
export const PLATFORM_PROBE_CHANNEL = 'harniqo:platform-probe';
export const SIGN_IN_CHANNEL = 'harniqo:sign-in';
export const SIGN_OUT_CHANNEL = 'harniqo:sign-out';
export const LIST_HARNESS_DEPLOYMENTS_CHANNEL = 'harniqo:list-harness-deployments';
export const START_HARNESS_RUN_CHANNEL = 'harniqo:start-harness-run';
export const READ_HARNESS_RUN_CHANNEL = 'harniqo:read-harness-run';
