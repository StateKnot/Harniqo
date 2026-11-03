export const APP_ORIGIN = 'harniqo://app';
export const APP_URL = `${APP_ORIGIN}/index.html`;
export const CONTENT_SECURITY_POLICY = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'none'; object-src 'none'; base-uri 'none'; frame-src 'none'; frame-ancestors 'none'; form-action 'none'";

/** Exact build-manifest membership is the authority, never a caller's filesystem path. */
export function resolveAssetPath(url: string, method: string, assets: ReadonlySet<string>): string | undefined {
  if (method !== 'GET') return undefined;
  try {
    const parsed = new URL(url);
    const path = parsed.pathname;
    if (parsed.protocol !== 'harniqo:' || parsed.host !== 'app' || parsed.username || parsed.password || parsed.search || parsed.hash) return undefined;
    if (url !== `${APP_ORIGIN}${path}` || !/^\/[a-zA-Z0-9_./-]+$/.test(path)) return undefined;
    if (path.slice(1).split('/').some((part) => part === '..' || part === '.' || part === '')) return undefined;
    return assets.has(path) ? path : undefined;
  } catch {
    return undefined;
  }
}

export interface SenderIdentity {
  readonly sender: unknown;
  readonly senderFrame: unknown;
}

/** Called inside each handler; a top-level URL by itself cannot authorize a subframe or another window. */
export function assertRuntimeInfoRequest(
  event: SenderIdentity,
  args: readonly unknown[],
  expectedContents: unknown,
  expectedMainFrame: { readonly url: string },
): void {
  if (event.sender !== expectedContents || event.senderFrame !== expectedMainFrame || expectedMainFrame.url !== APP_URL || args.length !== 0) {
    throw new Error('DESKTOP_REQUEST_DENIED');
  }
}
