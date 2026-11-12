import { StrictMode, useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import type { PlatformProbe, RuntimeInfo, SignedInAccount, SignInResult } from '../shared/interface';
import './style.css';

const probeErrors: Record<Extract<PlatformProbe, { error: string }>['error'], string> = {
  invalid_platform_origin: '请输入平台的 HTTP 或 HTTPS 地址，只填写协议、主机名和可选端口。',
  certificate_rejected: '证书校验失败。请核对平台主机名、证书有效期及受信证书链。',
  discovery_timeout: '探测超时，请检查网络和平台服务。',
  discovery_unreachable: '无法访问平台，请检查地址、网络和服务状态。',
  discovery_http_status: '未收到身份发现配置，请核对 Harniqo 控制面地址。',
  identity_not_configured: '已连到 Harniqo 控制面。登录服务待配置。',
  invalid_discovery: '平台返回的身份发现配置不符合 Harniqo 契约。',
  discovery_too_large: '平台返回的身份发现配置超出大小限制。',
  probe_busy: '正在检查另一个平台地址，请稍候。',
  user_action_required: '请使用“测试连接”按钮发起探测。',
};

type LoginError = Extract<SignInResult, { error: string }>['error'] | 'login_expired' | 'sign_out_failed';
const loginErrors: Record<LoginError, string> = {
  user_action_required: '请使用“在浏览器登录”按钮开始登录。',
  identity_mismatch: '身份配置不匹配，请核对管理员提供的地址和客户端 ID。',
  platform_not_verified: '请重新测试平台连接后再登录。',
  login_busy: '正在等待浏览器登录，请稍候。',
  login_failed: '登录未完成，请检查浏览器并重试。',
  login_cancelled: '已取消登录。',
  login_expired: '登录已过期，请重新登录。',
  sign_out_failed: '退出未完成，请重试。',
};

type LoginState =
  | { status: 'signing_in' | 'signed_out' }
  | { status: 'signed_in'; account: SignedInAccount; error?: 'sign_out_failed' }
  | { status: 'signing_out'; account: SignedInAccount }
  | { status: 'error'; error: LoginError };

function App() {
  const [runtime, setRuntime] = useState<RuntimeInfo>();
  const [failed, setFailed] = useState(false);
  const [mounted, setMounted] = useState(false);
  const [checking, setChecking] = useState(false);
  const [connection, setConnection] = useState<{ origin: string; probe: PlatformProbe }>();
  const [login, setLogin] = useState<LoginState>();
  const probe = connection?.probe;
  const platform = probe && 'issuer' in probe ? probe : undefined;
  const account = login && 'account' in login ? login.account : undefined;
  const loginError = login && 'error' in login ? login.error : undefined;
  const signingIn = login?.status === 'signing_in';
  const signingOut = login?.status === 'signing_out';
  const platformLocked = checking || signingIn || !!account;

  async function readRuntime(): Promise<void> {
    setFailed(false);
    try { setRuntime(await window.harniqo.getRuntimeInfo()); }
    catch { setFailed(true); }
  }
  useEffect(() => { setMounted(true); void readRuntime(); }, []);

  useEffect(() => {
    if (login?.status !== 'signed_in') return;
    let timer: number;
    function expire(): void {
      if (login?.status !== 'signed_in') return;
      const remaining = login.account.expiresAt - Date.now();
      if (remaining > 0) {
        timer = window.setTimeout(expire, Math.min(remaining, 2_147_483_647));
      } else {
        setLogin(current => current === login ? { status: 'error', error: 'login_expired' } : current);
      }
    }
    expire();
    return () => window.clearTimeout(timer);
  }, [login]);

  async function checkPlatform(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (platformLocked) return;
    const address = String(new FormData(event.currentTarget).get('platform') ?? '').trim();
    setChecking(true);
    setConnection(undefined);
    setLogin(undefined);
    try { setConnection({ origin: address, probe: await window.harniqo.probePlatform(address) }); }
    catch { setConnection({ origin: address, probe: { error: 'discovery_unreachable' } }); }
    finally { setChecking(false); }
  }

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (platformLocked || !connection || !platform) return;
    const fields = new FormData(event.currentTarget);
    const confirmation = platform.requiresIdentityConfirmation ? {
      issuer: String(fields.get('issuer') ?? '').trim(),
      clientId: String(fields.get('clientId') ?? '').trim(),
    } : undefined;
    const attempt = { status: 'signing_in' } as const;
    setLogin(attempt);
    try {
      const result = await window.harniqo.signIn(connection.origin, confirmation);
      const next: LoginState = 'account' in result
        ? { status: 'signed_in', account: result.account }
        : { status: 'error', error: result.error };
      setLogin(current => current === attempt ? next : current);
    } catch {
      setLogin(current => current === attempt ? { status: 'error', error: 'login_failed' } : current);
    }
  }

  async function cancelSignIn() {
    if (!signingIn) return;
    try {
      await window.harniqo.signOut();
      setLogin({ status: 'error', error: 'login_cancelled' });
    } catch {
      setLogin({ status: 'error', error: 'login_failed' });
    }
  }

  async function signOut() {
    if (!account || signingOut) return;
    setLogin({ status: 'signing_out', account });
    try {
      await window.harniqo.signOut();
      setLogin({ status: 'signed_out' });
    } catch {
      setLogin({ status: 'signed_in', account, error: 'sign_out_failed' });
    }
  }

  return (
    <main data-react-mounted={mounted ? 'true' : 'false'}>
      <header>
        <div className="brand"><span className="brand-mark" aria-hidden="true">H</span><span>Harniqo</span></div>
        <span className="edition">工程版本</span>
      </header>
      <section className="connection" aria-labelledby="connection-title"
        data-platform-state={checking ? 'checking' : probe ? ('issuer' in probe ? 'discovered'
          : probe.error === 'identity_not_configured' ? 'identity_pending' : 'error') : 'disconnected'}
        data-login-state={login?.status ?? 'idle'}>
        <h1 id="connection-title">平台连接</h1>
        <form onSubmit={(event) => void checkPlatform(event)} className="probe">
          <label htmlFor="platform-address">Harniqo 控制面地址（HTTP 或 HTTPS）</label>
          <div className="probe-controls">
            <input id="platform-address" name="platform" autoComplete="off" spellCheck={false}
              placeholder="https://platform.example.com" required maxLength={2048} disabled={platformLocked}
              onChange={() => { setConnection(undefined); setLogin(undefined); }} />
            <button type="submit" disabled={platformLocked}>测试连接</button>
          </div>
        </form>
        <div className="probe-result" aria-live="polite" aria-atomic="true">
          {checking ? '正在检查平台…' : probe && ('issuer' in probe
            ? <p>已读取平台身份配置。{!probe.requiresIdentityConfirmation && <>登录服务为 <bdi>{probe.issuer}</bdi>。</>}</p>
            : <p className={probe.error === 'identity_not_configured' ? undefined : 'probe-error'}
              data-probe-error={probe.error}>{probeErrors[probe.error]}</p>)}
        </div>
        {platform && <div className="platform-login">
          {!account && <form id="platform-login" onSubmit={(event) => void signIn(event)}>
            {platform.requiresIdentityConfirmation && <div className="identity-fields">
              <p id="identity-help">通过 HTTP 连接时，请填写管理员提供的登录配置。</p>
              <div className="identity-inputs">
                <div>
                  <label htmlFor="identity-issuer">身份服务地址（HTTPS）</label>
                  <input id="identity-issuer" name="issuer" type="url" pattern="https://.+" required maxLength={2048}
                    autoComplete="off" spellCheck={false} aria-describedby="identity-help" disabled={signingIn} />
                </div>
                <div>
                  <label htmlFor="identity-client-id">客户端 ID</label>
                  <input id="identity-client-id" name="clientId" required maxLength={256}
                    autoComplete="off" spellCheck={false} aria-describedby="identity-help" disabled={signingIn} />
                </div>
              </div>
            </div>}
            <button id="platform-sign-in" type="submit" disabled={signingIn}>在浏览器登录</button>
          </form>}
          <div className="login-result" role="status" aria-live="polite" aria-atomic="true">
            {signingIn && <p>请在系统浏览器完成登录。</p>}
            {account && <p>已登录 <bdi>{account.displayName}</bdi></p>}
            {login?.status === 'signed_out' && <p>已退出此客户端。</p>}
            {loginError && <p className="probe-error" data-login-error={loginError}>{loginErrors[loginError]}</p>}
          </div>
          {signingIn && <button type="button" onClick={() => void cancelSignIn()}>取消登录</button>}
          {account && <button id="platform-sign-out" type="button" disabled={signingOut} onClick={() => void signOut()}>
            {signingOut ? '正在退出…' : '退出此客户端'}
          </button>}
        </div>}
      </section>
      <section className="build" aria-labelledby="build-title">
        <div className="section-heading"><h2 id="build-title">关于此构建</h2><span>来自当前应用的真实信息</span></div>
        <div aria-live="polite">
          {failed ? <div role="alert" className="error">无法读取桌面运行信息。<button onClick={() => void readRuntime()}>重试</button></div> : runtime ? <>
            <dl className="summary">
              <div><dt>客户端版本</dt><dd>{runtime.appVersion}</dd></div>
              <div><dt>当前系统</dt><dd>{runtime.platform} / {runtime.arch}</dd></div>
              <div><dt>构建形式</dt><dd>{runtime.isPackaged ? '内部验收包' : '源码运行'}</dd></div>
            </dl>
            <details>
              <summary>查看源码与运行时标识</summary>
              <dl className="technical">
                <div><dt>源码提交</dt><dd>{runtime.sourceCommit}</dd></div>
                <div><dt>工作区</dt><dd>{runtime.sourceDirty ? '含未提交修改' : '无未提交修改'}</dd></div>
                <div><dt>构建输入 SHA-256</dt><dd>{runtime.sourceInputHash}</dd></div>
                <div><dt>Electron / Chromium</dt><dd>{runtime.electron} / {runtime.chrome}</dd></div>
                <div><dt>Node.js（主进程）</dt><dd>{runtime.node}</dd></div>
              </dl>
            </details>
          </> : <p className="loading" role="status">正在读取桌面运行信息…</p>}
        </div>
      </section>
    </main>
  );
}

const root = document.getElementById('root');
if (!root) throw new Error('RENDER_ROOT_MISSING');
createRoot(root).render(<StrictMode><App /></StrictMode>);
