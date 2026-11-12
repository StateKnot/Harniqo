import { StrictMode, useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import type { PlatformProbe, RuntimeInfo } from '../shared/interface';
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

function App() {
  const [runtime, setRuntime] = useState<RuntimeInfo>();
  const [failed, setFailed] = useState(false);
  const [mounted, setMounted] = useState(false);
  const [checking, setChecking] = useState(false);
  const [probe, setProbe] = useState<PlatformProbe>();
  async function readRuntime(): Promise<void> {
    setFailed(false);
    try { setRuntime(await window.harniqo.getRuntimeInfo()); }
    catch { setFailed(true); }
  }
  useEffect(() => { setMounted(true); void readRuntime(); }, []);
  async function checkPlatform(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (checking) return;
    const address = String(new FormData(event.currentTarget).get('platform') ?? '').trim();
    setChecking(true);
    setProbe(undefined);
    try { setProbe(await window.harniqo.probePlatform(address)); }
    catch { setProbe({ error: 'discovery_unreachable' }); }
    finally { setChecking(false); }
  }
  return (
    <main data-react-mounted={mounted ? 'true' : 'false'}>
      <header>
        <div className="brand"><span className="brand-mark" aria-hidden="true">H</span><span>Harniqo</span></div>
        <span className="edition">工程版本</span>
      </header>
      <section className="connection" aria-labelledby="connection-title"
        data-platform-state={checking ? 'checking' : probe ? ('issuer' in probe ? 'discovered'
          : probe.error === 'identity_not_configured' ? 'identity_pending' : 'error') : 'disconnected'}>
        <h1 id="connection-title">平台连接</h1>
        <form onSubmit={(event) => void checkPlatform(event)} className="probe">
          <label htmlFor="platform-address">Harniqo 控制面地址（HTTP 或 HTTPS）</label>
          <div className="probe-controls">
            <input id="platform-address" name="platform" autoComplete="off" spellCheck={false}
              placeholder="https://platform.example.com" required maxLength={2048} disabled={checking}
              onChange={() => setProbe(undefined)} />
            <button type="submit" disabled={checking}>测试连接</button>
          </div>
        </form>
        <div className="probe-result" aria-live="polite" aria-atomic="true">
          {checking ? '正在检查平台…' : probe && ('issuer' in probe
            ? <p>已读取平台身份配置。登录服务为 <span>{probe.issuer}</span>。</p>
            : <p className={probe.error === 'identity_not_configured' ? undefined : 'probe-error'}
              data-probe-error={probe.error}>{probeErrors[probe.error]}</p>)}
        </div>
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
