import { useEffect, useRef, useState, type FormEvent } from 'react';
import type { HarnessDeployment, HarnessRun } from '../shared/interface';

const maxInputLength = 16_384;
const pollInterval = 1_000;
const pollWindow = 120_000;

const errors: Record<string, string> = {
  api_origin_required: '请退出此客户端，重新登录时填写管理员提供的 HTTPS 业务接口地址。',
  business_unavailable: '暂时无法访问业务接口。请检查平台服务及网络连接。',
  sign_in_required: '请重新登录后继续。',
  access_denied: '当前账号无权执行此操作。请联系项目管理员。',
  idempotency_conflict: '本次提交编号与服务器记录冲突。',
  invalid_workbench_response: '业务接口返回的数据无法读取。请重试。',
  invalid_workbench_input: '业务接口拒绝了本次输入。请检查部署和指令内容。',
  deployment_changed: '部署已变更。请刷新部署列表后重新选择。',
  version_incompatible: '客户端和平台的接口版本不兼容。请联系管理员。',
  quota_exceeded: '当前会话已有任务，或项目已达到运行配额。指令已保留。',
  budget_exceeded: '项目运行预算不足。指令已保留。',
};

const statusLabels: Record<HarnessRun['state'], string> = {
  accepted: '已受理',
  queued: '排队中',
  provisioning: '正在准备执行环境',
  running: '执行中',
  waiting_approval: '等待审批',
  waiting_input: '等待补充输入',
  stopping: '正在停止',
  reconciling: '执行状态核对中',
  succeeded: '执行结束',
  failed: '执行失败',
  cancelled: '已取消',
};

type Catalog = { status: 'loading' } | { status: 'loaded'; deployments: HarnessDeployment[] }
  | { status: 'error'; error: string };

type Command = { key: string; deployment: HarnessDeployment; input: string };

type Execution = { status: 'idle' | 'sending' }
  | { status: 'rejected'; error: string }
  | { status: 'unconfirmed'; command: Command; error: string }
  | { status: 'received'; command: Command; run: HarnessRun };

function isTerminal(state: HarnessRun['state']): boolean {
  return state === 'succeeded' || state === 'failed' || state === 'cancelled';
}

function errorMessage(error: string): string {
  return (Object.hasOwn(errors, error) && errors[error]) || '操作未完成，请重试。';
}

export function Workbench() {
  const [catalog, setCatalog] = useState<Catalog>({ status: 'loading' });
  const [catalogAttempt, setCatalogAttempt] = useState(0);
  const [deploymentId, setDeploymentId] = useState('');
  const [draft, setDraft] = useState('');
  const [execution, setExecution] = useState<Execution>({ status: 'idle' });
  const [pollAttempt, setPollAttempt] = useState(0);
  const [pollIssue, setPollIssue] = useState<string>();
  const sending = useRef(false);
  const deployment = catalog.status === 'loaded' ? catalog.deployments.find(item => item.id === deploymentId) : undefined;
  const run = execution.status === 'received' ? execution.run : undefined;
  const activeRunId = run && !isTerminal(run.state) ? run.id : undefined;
  const busy = execution.status === 'sending' || execution.status === 'unconfirmed' || !!activeRunId;

  useEffect(() => {
    let current = true;
    const timer = window.setTimeout(async () => {
      const result = await window.harniqo.listHarnessDeployments().catch(() => ({ ok: false, error: 'business_unavailable' }) as const);
      if (!current) return;
      if (!result.ok) {
        setCatalog({ status: 'error', error: result.error });
        return;
      }
      const { deployments } = result;
      setCatalog({ status: 'loaded', deployments });
      setDeploymentId(selected => deployments.some(item => item.id === selected) ? selected
        : deployments.length === 1 ? deployments[0]?.id ?? '' : '');
    }, 0);
    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [catalogAttempt]);

  useEffect(() => {
    if (!activeRunId) return;
    const runId = activeRunId;
    const deadline = Date.now() + pollWindow;
    let current = true;
    let timer = window.setTimeout(() => void poll(), pollInterval);

    async function poll() {
      const result = await window.harniqo.readHarnessRun(runId).catch(() => ({ ok: false, error: 'business_unavailable' }) as const);
      if (!current) return;
      if (!result.ok) {
        setPollIssue(errorMessage(result.error));
        return;
      }
      setExecution(state => state.status === 'received' && state.run.id === runId ? { ...state, run: result.run } : state);
      if (isTerminal(result.run.state)) return;
      if (Date.now() >= deadline) {
        setPollIssue('暂未收到最终状态。可继续查看此运行的状态。');
      } else {
        timer = window.setTimeout(() => void poll(), pollInterval);
      }
    }

    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [activeRunId, pollAttempt]);

  function refreshCatalog() {
    if (busy || catalog.status === 'loading') return;
    if (execution.status === 'rejected') setExecution({ status: 'idle' });
    setCatalog({ status: 'loading' });
    setCatalogAttempt(attempt => attempt + 1);
  }

  async function send(command: Command, confirming = false) {
    if (sending.current) return;
    sending.current = true;
    setExecution({ status: 'sending' });
    setPollIssue(undefined);
    try {
      const result = await window.harniqo.startHarnessRun(command.deployment.id, command.input, command.key)
        .catch(() => ({ ok: false, error: 'business_unavailable' }) as const);
      if (result.ok) {
        setDraft('');
        setExecution({ status: 'received', command, run: result.run });
      } else if (result.error === 'business_unavailable' || result.error === 'invalid_workbench_response'
        || (confirming && result.error === 'sign_in_required')) {
        setExecution({ status: 'unconfirmed', command, error: result.error });
      } else {
        setExecution({ status: 'rejected', error: result.error });
      }
    } finally {
      sending.current = false;
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || sending.current || !deployment || !draft.trim()) return;
    void send({ key: window.crypto.randomUUID(), deployment, input: draft });
  }

  return (
    <section className="workbench" aria-labelledby="workbench-title" data-catalog-state={catalog.status}>
      <div className="workbench-heading">
        <h2 id="workbench-title">Harness 工作台</h2>
        <button type="button" disabled={busy || catalog.status === 'loading'} onClick={refreshCatalog}>刷新部署</button>
      </div>
      {catalog.status === 'loading' && <p role="status">正在读取可用部署…</p>}
      {catalog.status === 'error' && <p className="probe-error" role="alert">{errorMessage(catalog.error)}</p>}
      {catalog.status === 'loaded' && catalog.deployments.length === 0 && <p>当前账号没有可用的 Hermes 部署。请联系项目管理员。</p>}
      {catalog.status === 'loaded' && catalog.deployments.length > 0 && <form className="workbench-form" onSubmit={submit}>
        <label htmlFor="harness-deployment">Harness 部署</label>
        <select id="harness-deployment" value={deploymentId} disabled={busy} required onChange={event => setDeploymentId(event.currentTarget.value)}>
          <option value="" disabled>选择部署</option>
          {catalog.deployments.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
        <label htmlFor="harness-input">文本指令</label>
        <textarea id="harness-input" rows={4} required maxLength={maxInputLength} value={draft} disabled={busy}
          onChange={event => setDraft(event.currentTarget.value)} />
        <button id="harness-submit" type="submit" disabled={busy || !deployment || !draft.trim()}>下发指令</button>
      </form>}
      {execution.status === 'sending' && <p role="status">正在提交指令…</p>}
      {execution.status === 'rejected' && <p className="probe-error" role="alert">{errorMessage(execution.error)}</p>}
      {execution.status === 'unconfirmed' && <div className="workbench-notice">
        <p className="probe-error" role="alert">尚未确认服务器是否受理。{errorMessage(execution.error)}</p>
        <button type="button" onClick={() => void send(execution.command, true)}>重新确认提交</button>
      </div>}
      {execution.status === 'received' && <section className="workbench-run" aria-labelledby="run-title"
        data-run-state={execution.run.state} data-run-id={execution.run.id}>
        <h3 id="run-title">{execution.command.deployment.name}</h3>
        <p className="workbench-run-id">运行编号 <bdi>{execution.run.id}</bdi></p>
        <div className="workbench-turn">
          <h4>下发指令</h4>
          <p>{execution.command.input}</p>
        </div>
        <div aria-live="polite">
          <p className="workbench-run-status">{statusLabels[execution.run.state]}</p>
          {execution.run.message && <p className="workbench-run-message">{execution.run.message}</p>}
          {execution.run.reply && <div className="workbench-turn workbench-reply">
            <h4>Hermes 回复</h4>
            <p>{execution.run.reply}</p>
          </div>}
          {execution.run.state === 'succeeded' && !execution.run.reply && !execution.run.message && <p>服务器未返回文本回复。</p>}
        </div>
        {pollIssue && <div className="workbench-notice">
          <p className="probe-error" role="alert">{pollIssue}</p>
          <button type="button" onClick={() => { setPollIssue(undefined); setPollAttempt(attempt => attempt + 1); }}>继续查看状态</button>
        </div>}
      </section>}
    </section>
  );
}
