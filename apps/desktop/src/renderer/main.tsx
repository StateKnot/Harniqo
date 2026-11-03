import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { RuntimeInfo } from '../shared/interface';
import './style.css';

function App() {
  const [runtime, setRuntime] = useState<RuntimeInfo>();
  const [failed, setFailed] = useState(false);
  const [mounted, setMounted] = useState(false);
  async function readRuntime(): Promise<void> {
    setFailed(false);
    try { setRuntime(await window.harniqo.getRuntimeInfo()); }
    catch { setFailed(true); }
  }
  useEffect(() => { setMounted(true); void readRuntime(); }, []);
  return (
    <main data-react-mounted={mounted ? 'true' : 'false'}>
      <header>
        <div className="brand"><span className="brand-mark" aria-hidden="true">H</span><span>Harniqo</span></div>
        <span className="edition">工程版本</span>
      </header>
      <section className="connection" aria-labelledby="connection-title" data-platform-state="disconnected">
        <p className="eyebrow"><span className="status-dot" aria-hidden="true" />本机客户端</p>
        <h1 id="connection-title">未连接平台</h1>
        <p className="description">Harniqo 用于连接云端 Harness，持续对话、比较任务与批量评测。</p>
        <p className="scope">当前版本提供可运行的桌面工程入口。平台登录、环境连接与真实 Harness 会话尚未接入。</p>
        <div className="scope-note">本机运行状态不代表云端服务已连接。</div>
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
      <footer>Harniqo · M1-01 工程入口</footer>
    </main>
  );
}

const root = document.getElementById('root');
if (!root) throw new Error('RENDER_ROOT_MISSING');
createRoot(root).render(<StrictMode><App /></StrictMode>);
