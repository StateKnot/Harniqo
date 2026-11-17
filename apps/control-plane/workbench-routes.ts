import { randomUUID } from 'node:crypto';
import { contractValidator } from '@harniqo/contracts/validation';
import semver from 'semver';
import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import type { Kysely } from 'kysely';
import {
  RunStoreError, acceptRun, listRunTargets, lookupRunSubmission, readRunEvents, readRunSnapshot,
} from './workbench-store/index.ts';
import type { Principal, ProjectPrincipal, ReserveAndEnqueue, SubmitRun } from './workbench-store/index.ts';

const contractVersion = '0.1.0';
const idempotencyPattern = /^[A-Za-z0-9._:-]{16,128}$/u;
const projectRoute = '/v1/organizations/:organizationId/projects/:projectId';
const sessionRoute = projectRoute + '/sessions/:sessionId';
const runRoute = projectRoute + '/runs/:runId';
const jsonLimit = 400 * 1024;
const validateIdentifier = contractValidator<string>('Identifier');
const validateSubmit = contractValidator<SubmitRun>('SubmitRunRequest');
const validatePlatformMetadata = contractValidator<PlatformMetadata>('PlatformMetadata');
const validateHandshake = contractValidator<DesktopHandshake>('DesktopHandshakeRequest');
const validateProblem = contractValidator('Problem');
const responseValidators = {
  WorkbenchDeploymentList: contractValidator('WorkbenchDeploymentList'),
  AcceptedRun: contractValidator('AcceptedRun'),
  RunSnapshot: contractValidator('RunSnapshot'),
  EventPage: contractValidator('EventPage'),
};

type DesktopHandshake = {
  software_version: string;
  supported_contract_versions: string[];
};

export type PlatformMetadata = {
  platform_id: string;
  platform_epoch: string;
  build_id: string;
  software_version: string;
  selected_contract_version: string;
  supported_contract_versions: string[];
  minimum_component_versions: { desktop: string; runner: string; execution_agent: string };
  feature_flags: string[];
};

type RouteParams = Record<string, unknown>;

export type WorkbenchOptions = {
  db: Kysely<unknown>;
  authenticate: (authorization: string | undefined) => Promise<Principal>;
  reserveAndEnqueue: ReserveAndEnqueue;
  platform: unknown;
  close?: () => Promise<void>;
};

const problems = {
  UNAUTHENTICATED: [401, 'Authentication is required.'],
  FORBIDDEN: [403, 'Access is not granted.'],
  NOT_FOUND: [404, 'The requested resource is unavailable.'],
  VALIDATION_FAILED: [400, 'The request does not match the workbench API.'],
  CAPABILITY_UNSUPPORTED: [422, 'The selected deployment does not support the required capabilities.'],
  VERSION_INCOMPATIBLE: [409, 'This desktop and platform do not share a supported version.'],
  IDEMPOTENCY_CONFLICT: [409, 'This command key has been used for another request.'],
  BUDGET_EXCEEDED: [409, 'The project has no remaining execution budget.'],
  QUOTA_EXCEEDED: [429, 'The session or project has reached its execution limit.'],
  UPSTREAM_UNAVAILABLE: [503, 'A required platform resource is temporarily unavailable.'],
  INTERNAL_ERROR: [500, 'The platform could not complete the request.'],
} as const;

type ProblemCode = keyof typeof problems;
export class WorkbenchProblem extends Error {
  readonly code: ProblemCode;

  constructor(code: ProblemCode) {
    super(code);
    this.code = code;
  }
}

function problemCode(error: unknown): ProblemCode {
  if (error instanceof WorkbenchProblem || error instanceof RunStoreError) return error.code;
  // The Access and queue modules expose only these stable failure codes.
  if (error instanceof Error && 'code' in error
    && (error.code === 'UNAUTHENTICATED' || error.code === 'FORBIDDEN' || error.code === 'BUDGET_EXCEEDED'
      || error.code === 'QUOTA_EXCEEDED' || error.code === 'UPSTREAM_UNAVAILABLE')) return error.code;
  if (error instanceof Error && 'statusCode' in error
    && (error.statusCode === 400 || error.statusCode === 413 || error.statusCode === 415)) return 'VALIDATION_FAILED';
  return 'INTERNAL_ERROR';
}

function setResponseHeaders(reply: FastifyReply): void {
  reply.type('application/json').header('cache-control', 'no-store').header('x-content-type-options', 'nosniff');
}

function sendProblem(reply: FastifyReply, code: ProblemCode) {
  const [status, message] = problems[code];
  const problem = {
    code, request_id: randomUUID(), message,
    retry_class: code === 'UPSTREAM_UNAVAILABLE' ? 'idempotent_transport' : 'never',
  };
  if (!validateProblem(problem)) throw new Error('invalid_workbench_problem');
  if (code === 'UNAUTHENTICATED') reply.header('www-authenticate', 'Bearer');
  setResponseHeaders(reply);
  return reply.code(status).send(problem);
}

function identifier(value: unknown): string {
  if (!validateIdentifier(value)) throw new WorkbenchProblem('VALIDATION_FAILED');
  return value;
}

function scopeFor(auth: Principal, params: RouteParams): ProjectPrincipal {
  const organizationId = identifier(params.organizationId);
  if (organizationId !== auth.organizationId) throw new WorkbenchProblem('FORBIDDEN');
  return { ...auth, projectId: identifier(params.projectId) };
}

function idempotencyKey(request: FastifyRequest): string {
  const value = request.headers['idempotency-key'];
  if (typeof value !== 'string' || !idempotencyPattern.test(value)) throw new WorkbenchProblem('VALIDATION_FAILED');
  return value;
}

function queryInteger(value: unknown, fallback: number, min: number, max: number): number {
  if (value === undefined) return fallback;
  const number = typeof value === 'string' && /^\d{1,16}$/u.test(value) ? Number(value) : Number.NaN;
  if (!Number.isSafeInteger(number) || number < min || number > max) throw new WorkbenchProblem('VALIDATION_FAILED');
  return number;
}

function sendContract(reply: FastifyReply, definition: keyof typeof responseValidators, value: unknown, status = 200) {
  if (!responseValidators[definition](value)) throw new WorkbenchProblem('INTERNAL_ERROR');
  return reply.code(status).send(value);
}

export async function registerWorkbench(server: FastifyInstance, options: WorkbenchOptions): Promise<void> {
  const { db, authenticate, reserveAndEnqueue, platform } = options;
  if (!validatePlatformMetadata(platform)
    || platform.selected_contract_version !== contractVersion
    || !semver.valid(platform.minimum_component_versions.desktop)
    || !semver.valid(platform.software_version)) throw new Error('invalid_workbench_platform');

  const authByRequest = new WeakMap<FastifyRequest, Principal>();
  const principal = (request: FastifyRequest) => {
    const auth = authByRequest.get(request);
    if (!auth) throw new WorkbenchProblem('UNAUTHENTICATED');
    return auth;
  };
  server.setErrorHandler((error, _request, reply) => sendProblem(reply, problemCode(error)));
  if (options.close) server.addHook('onClose', options.close);
  server.addHook('onRequest', async (request, reply) => {
    setResponseHeaders(reply);
    if (request.protocol !== 'https') throw new WorkbenchProblem('UNAUTHENTICATED');
    authByRequest.set(request, await authenticate(request.headers.authorization));
  });

  server.get('/v1/meta', async () => platform);
  server.post('/v1/handshakes', { bodyLimit: 8192 }, async (request) => {
    const handshake = request.body;
    if (!validateHandshake(handshake) || !semver.valid(handshake.software_version)) {
      throw new WorkbenchProblem('VALIDATION_FAILED');
    }
    if (!handshake.supported_contract_versions.includes(contractVersion)
      || semver.lt(handshake.software_version, platform.minimum_component_versions.desktop)) {
      throw new WorkbenchProblem('VERSION_INCOMPATIBLE');
    }
    return platform;
  });

  server.get('/v1/workbench/deployments', async (request, reply) => {
    const deployments = await listRunTargets(db, principal(request), platform.platform_epoch);
    return sendContract(reply, 'WorkbenchDeploymentList', { deployments });
  });
  server.post<{ Params: RouteParams }>(sessionRoute + '/runs', { bodyLimit: jsonLimit }, async (request, reply) => {
    const scope = scopeFor(principal(request), request.params);
    const session = identifier(request.params.sessionId);
    const key = idempotencyKey(request);
    const input = request.body;
    if (!validateSubmit(input) || !input.prompt.trim() || !input.prompt.isWellFormed()
      || input.prompt.includes('\0') || Buffer.byteLength(JSON.stringify(input.prompt)) > 65_300) {
      throw new WorkbenchProblem('VALIDATION_FAILED');
    }
    const accepted = await acceptRun(db, scope, session, key, input, reserveAndEnqueue);
    return sendContract(reply, 'AcceptedRun', accepted, 202);
  });
  server.get<{ Params: RouteParams }>(sessionRoute + '/run-submissions::lookup', async (request, reply) => {
    const scope = scopeFor(principal(request), request.params);
    const result = await lookupRunSubmission(db, scope, identifier(request.params.sessionId), idempotencyKey(request));
    return sendContract(reply, 'AcceptedRun', result);
  });
  server.get<{ Params: RouteParams }>(runRoute, async (request, reply) => {
    const scope = scopeFor(principal(request), request.params);
    return sendContract(reply, 'RunSnapshot', await readRunSnapshot(db, scope, identifier(request.params.runId)));
  });
  server.get<{ Params: RouteParams; Querystring: Record<string, unknown> }>(runRoute + '/events', async (request, reply) => {
    const scope = scopeFor(principal(request), request.params);
    if (Object.keys(request.query).some(key => key !== 'after' && key !== 'limit')) {
      throw new WorkbenchProblem('VALIDATION_FAILED');
    }
    const after = queryInteger(request.query.after, 0, 0, Number.MAX_SAFE_INTEGER);
    const limit = queryInteger(request.query.limit, 100, 1, 256);
    const result = await readRunEvents(db, scope, identifier(request.params.runId), after, Math.min(limit, 32));
    return sendContract(reply, 'EventPage', result);
  });
}
