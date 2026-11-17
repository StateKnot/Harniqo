import { HARNESS_RUN_STATES } from '../shared/interface';
import type { HarnessDeployment, HarnessRun, WorkspaceResult } from '../shared/interface';

const uuid = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i;
const idempotencyKey = /^[A-Za-z0-9._:-]{16,128}$/u;
const maxDeployments = 100;
const maxRuns = 128;
const maxResponseBytes = 2 * 1024 * 1024;
const maxReplyLength = 256 * 1024;
const eventPageSize = 4;
const contractVersion = '0.1.0';

type Target = HarnessDeployment & {
  organization_id: string;
  project_id: string;
  session_id: string;
  configuration_revision_id: string;
  deployment_revision_id: string;
  input_snapshot_id: string;
  budget_policy_id: string;
};

type RunLocation = { target: Target; terminal?: HarnessRun };
type Submission = { target: Target; input: string; sending: boolean };

class WorkbenchError extends Error {}

export function httpsApiOrigin(input: unknown): string | undefined {
  if (typeof input !== 'string' || input.length > 2048
    || !/^https:\/\/[^/@?#\\\s\p{Cc}]+\/?$/iu.test(input)) return;
  const url = URL.parse(input);
  return url?.protocol === 'https:' && url.hostname && url.pathname === '/' ? url.origin : undefined;
}

function object(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new WorkbenchError('invalid_workbench_response');
  }
  return value as Record<string, unknown>;
}

function identifier(value: unknown): string {
  if (typeof value !== 'string' || !uuid.test(value)) throw new WorkbenchError('invalid_workbench_response');
  return value;
}

function parseTarget(value: unknown): Target {
  const target = object(value);
  if (target.harness !== 'hermes' || typeof target.name !== 'string' || !target.name.trim()
    || target.name.length > 128 || /[\p{Cc}\p{Cf}]/u.test(target.name)) {
    throw new WorkbenchError('invalid_workbench_response');
  }
  return {
    id: identifier(target.id),
    name: target.name,
    harness: target.harness,
    organization_id: identifier(target.organization_id),
    project_id: identifier(target.project_id),
    session_id: identifier(target.session_id),
    configuration_revision_id: identifier(target.configuration_revision_id),
    deployment_revision_id: identifier(target.deployment_revision_id),
    input_snapshot_id: identifier(target.input_snapshot_id),
    budget_policy_id: identifier(target.budget_policy_id),
  };
}

async function jsonResponse(response: Response): Promise<Record<string, unknown>> {
  if (!/^application\/json(?:\s*;|$)/iu.test(response.headers.get('content-type') ?? '') || !response.body) {
    await response.body?.cancel();
    throw new WorkbenchError('invalid_workbench_response');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let bytes = 0;
  let json = '';
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > maxResponseBytes) {
        await reader.cancel();
        throw new WorkbenchError('invalid_workbench_response');
      }
      json += decoder.decode(value, { stream: true });
    }
    return object(JSON.parse(json + decoder.decode()));
  } catch (error) {
    if (error instanceof TypeError || error instanceof SyntaxError) throw new WorkbenchError('invalid_workbench_response');
    throw error;
  } finally {
    reader.releaseLock();
  }
}

function projectPath(target: Target): string {
  return `/v1/organizations/${target.organization_id}/projects/${target.project_id}`;
}

export class DesktopWorkbench {
  private readonly signal = new AbortController();
  private targets = new Map<string, Target>();
  private readonly runs = new Map<string, RunLocation>();
  private readonly submissions = new Map<string, Submission>();
  private readonly origin: string | undefined;
  private negotiated = false;

  constructor(
    origin: string | undefined,
    private accessToken: string,
    private readonly expiry: number,
    private readonly transport: typeof fetch,
    private readonly build: { sourceCommit: string; appVersion: string },
  ) {
    this.origin = httpsApiOrigin(origin);
  }

  close(): void {
    this.signal.abort();
    this.accessToken = '';
    this.targets.clear();
    this.runs.clear();
    this.submissions.clear();
  }

  private async request(path: string, expectedStatus: number, init?: RequestInit, signal?: AbortSignal) {
    if (!this.origin) throw new WorkbenchError('api_origin_required');
    if (this.expiry <= Date.now()) throw new WorkbenchError('sign_in_required');
    this.signal.signal.throwIfAborted();
    const response = await this.transport(new URL(path, this.origin), {
      ...init,
      headers: { ...init?.headers, accept: 'application/json', authorization: `Bearer ${this.accessToken}` },
      signal: AbortSignal.any([this.signal.signal, AbortSignal.timeout(8000), ...(signal ? [signal] : [])]),
      redirect: 'error',
      credentials: 'omit',
      cache: 'no-store',
    });
    if (response.status === expectedStatus) return jsonResponse(response);
    if (response.status === 404 && path.endsWith('/run-submissions:lookup')) {
      const problem = await jsonResponse(response);
      if (problem.code === 'NOT_FOUND') throw new WorkbenchError('receipt_not_found');
      throw new WorkbenchError('business_unavailable');
    }
    if ([400, 409, 422, 429].includes(response.status)) {
      const problem = await jsonResponse(response);
      if (problem.code === 'IDEMPOTENCY_CONFLICT') throw new WorkbenchError('idempotency_conflict');
      if (problem.code === 'VERSION_INCOMPATIBLE') throw new WorkbenchError('version_incompatible');
      if (problem.code === 'QUOTA_EXCEEDED') throw new WorkbenchError('quota_exceeded');
      if (problem.code === 'BUDGET_EXCEEDED') throw new WorkbenchError('budget_exceeded');
      if (problem.code === 'VALIDATION_FAILED') throw new WorkbenchError('invalid_workbench_input');
      throw new WorkbenchError('business_unavailable');
    }
    await response.body?.cancel();
    if (response.status === 401) throw new WorkbenchError('sign_in_required');
    if (response.status === 403) throw new WorkbenchError('access_denied');
    if (response.status === 404 && expectedStatus === 202) throw new WorkbenchError('deployment_changed');
    throw new WorkbenchError('business_unavailable');
  }

  private async result<T>(operation: () => Promise<T>): Promise<WorkspaceResult<T>> {
    if (this.signal.signal.aborted || this.expiry <= Date.now()) {
      this.close();
      return { ok: false, error: 'sign_in_required' };
    }
    try {
      const result = await operation();
      this.signal.signal.throwIfAborted();
      return { ok: true, ...result };
    } catch (error) {
      return {
        ok: false,
        error: this.signal.signal.aborted ? 'sign_in_required'
          : error instanceof WorkbenchError ? error.message : 'business_unavailable',
      };
    }
  }

  list() {
    return this.result(async () => {
      if (!this.negotiated) {
        const platform = await this.request('/v1/handshakes', 200, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            component: 'desktop', build_id: this.build.sourceCommit, software_version: this.build.appVersion,
            supported_contract_versions: [contractVersion],
          }),
        });
        identifier(platform.platform_id);
        identifier(platform.platform_epoch);
        if (platform.selected_contract_version !== contractVersion || !Array.isArray(platform.supported_contract_versions)
          || !platform.supported_contract_versions.includes(contractVersion)) {
          throw new WorkbenchError('version_incompatible');
        }
        this.negotiated = true;
      }
      const body = await this.request('/v1/workbench/deployments', 200);
      if (!Array.isArray(body.deployments) || body.deployments.length > maxDeployments) {
        throw new WorkbenchError('invalid_workbench_response');
      }
      this.signal.signal.throwIfAborted();
      const targets = body.deployments.map(parseTarget);
      const indexed = new Map(targets.map(target => [target.id, target]));
      if (indexed.size !== targets.length) throw new WorkbenchError('invalid_workbench_response');
      this.targets = indexed;
      return { deployments: targets.map(({ id, name, harness }) => ({ id, name, harness })) };
    });
  }

  start(deploymentId: unknown, input: unknown, key: unknown) {
    return this.result(async () => {
      if (typeof deploymentId !== 'string' || !uuid.test(deploymentId) || typeof input !== 'string'
        || !input.trim() || input.length > 16384 || typeof key !== 'string' || !idempotencyKey.test(key)) {
        throw new WorkbenchError('invalid_workbench_input');
      }
      const previous = this.submissions.get(key);
      if (previous && (previous.input !== input || previous.target.id !== deploymentId)) {
        throw new WorkbenchError('idempotency_conflict');
      }
      if (previous?.sending) throw new WorkbenchError('business_unavailable');
      const target = previous?.target ?? this.targets.get(deploymentId);
      if (!target) throw new WorkbenchError('deployment_changed');
      if (!previous && this.submissions.size >= maxRuns) throw new WorkbenchError('business_unavailable');
      const submission = previous ?? { target, input, sending: false };
      this.submissions.set(key, submission);
      submission.sending = true;
      const path = `${projectPath(target)}/sessions/${target.session_id}`;
      const headers = { 'idempotency-key': key };
      try {
        let receipt: Record<string, unknown> | undefined;
        if (previous) {
          try {
            receipt = await this.request(`${path}/run-submissions:lookup`, 200, { headers });
          } catch (error) {
            if (!(error instanceof WorkbenchError) || error.message !== 'receipt_not_found') throw error;
          }
        }
        receipt ??= await this.request(`${path}/runs`, 202, {
          method: 'POST',
          headers: { ...headers, 'content-type': 'application/json' },
          body: JSON.stringify({
            configuration_revision_id: target.configuration_revision_id,
            deployment_revision_id: target.deployment_revision_id,
            input_snapshot_id: target.input_snapshot_id,
            budget_policy_id: target.budget_policy_id,
            prompt: input,
            required_capabilities: [],
          }),
        });
        if (receipt.initial_state !== 'accepted') throw new WorkbenchError('invalid_workbench_response');
        const id = identifier(receipt.run_id);
        this.signal.signal.throwIfAborted();
        this.submissions.delete(key);
        this.runs.delete(id);
        this.runs.set(id, { target });
        if (this.runs.size > maxRuns) this.runs.delete(this.runs.keys().next().value!);
        const run: HarnessRun = { id, deploymentId, state: 'accepted' };
        return { run };
      } catch (error) {
        if (error instanceof WorkbenchError && [
          'idempotency_conflict', 'access_denied', 'deployment_changed', 'invalid_workbench_input',
          'budget_exceeded', 'quota_exceeded',
        ].includes(error.message)) {
          this.submissions.delete(key);
        }
        throw error;
      } finally {
        submission.sending = false;
      }
    });
  }

  read(runId: unknown) {
    return this.result(async () => {
      if (typeof runId !== 'string' || !uuid.test(runId)) throw new WorkbenchError('invalid_workbench_input');
      const location = this.runs.get(runId);
      if (!location) throw new WorkbenchError('invalid_workbench_input');
      const { target } = location;
      const path = `${projectPath(target)}/runs/${runId}`;
      const snapshot = await this.request(path, 200);
      const raw = object(snapshot.run);
      const scope = object(raw.scope);
      const state = HARNESS_RUN_STATES.find(candidate => candidate === raw.state);
      if (raw.id !== runId || raw.session_id !== target.session_id
        || raw.deployment_revision_id !== target.deployment_revision_id
        || scope.organization_id !== target.organization_id || scope.project_id !== target.project_id || !state) {
        throw new WorkbenchError('invalid_workbench_response');
      }
      const run: HarnessRun = { id: runId, deploymentId: target.id, state };
      if (state === 'succeeded') {
        if (location.terminal) return { run: location.terminal };
        const last = snapshot.last_seq;
        if (typeof last !== 'number' || !Number.isSafeInteger(last) || last < 1) {
          throw new WorkbenchError('invalid_workbench_response');
        }
        const reply = await this.readReply(path, runId, last);
        if (reply) run.reply = reply;
        else run.message = '本次运行已结束，服务器未记录文本回复。';
        location.terminal = run;
      }
      return { run };
    });
  }

  private async readReply(path: string, runId: string, last: number): Promise<string> {
    const deadline = AbortSignal.timeout(12_000);
    let after = 0;
    let text = '';
    for (let page = 0; page < 256 && after < last; page++) {
      const result = await this.request(`${path}/events?after=${after}&limit=${eventPageSize}`, 200, undefined, deadline);
      if (result.run_id !== runId || !Array.isArray(result.events) || !result.events.length || result.events.length > eventPageSize) {
        throw new WorkbenchError('invalid_workbench_response');
      }
      for (const item of result.events) {
        const event = object(item);
        if (event.run_id !== runId || event.seq !== ++after || after > last) {
          throw new WorkbenchError('invalid_workbench_response');
        }
        if (event.source === undefined) continue;
        const source = object(event.source);
        if (source.run_id !== runId) throw new WorkbenchError('invalid_workbench_response');
        if (source.type === 'text.delta') {
          const delta = object(source.payload).text;
          if (typeof delta !== 'string' || text.length + delta.length > maxReplyLength) {
            throw new WorkbenchError('invalid_workbench_response');
          }
          text += delta;
        }
      }
      if (result.next_after !== after || result.has_more !== (after < last)) {
        throw new WorkbenchError('invalid_workbench_response');
      }
    }
    if (after !== last) throw new WorkbenchError('invalid_workbench_response');
    return text;
  }
}
