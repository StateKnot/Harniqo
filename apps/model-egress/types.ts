import type { Writable } from 'node:stream';

export type Address = Readonly<{ address: string; family: 4 | 6 }>;
export type SecretHeaders = Readonly<Record<string, string>>;
export type Route = Readonly<{
  id: string;
  origin: string;
  operations: readonly Readonly<{ method: 'GET' | 'POST'; path: string }>[];
  secretRef: string;
  /** Explicit exact-IP exceptions, bound to this route's origin and port. */
  privateAddressExceptions?: readonly string[];
  /** Explicit replacement trust bundle; certificate verification cannot be disabled. */
  ca?: string | Buffer;
}>;
export type Limits = Readonly<{
  maxConcurrent: number;
  maxConcurrentPerRoute: number;
  maxRequestBytes: number;
  maxResponseBytes: number;
  maxHeaderBytes: number;
  idleTimeoutMs: number;
  totalTimeoutMs: number;
}>;
export type TransportOptions = Readonly<{
  routes: readonly Route[];
  getSecret: (secretRef: string) => Promise<SecretHeaders>;
  /** OS lookup by default; a controlled resolver is useful for private origins. */
  resolve?: (hostname: string) => Promise<readonly Address[]>;
  limits?: Partial<Limits>;
  /** Management networks and deployment-specific translation prefixes: always denied. */
  deniedCidrs?: readonly string[];
}>;
export type ExchangeRequest = Readonly<{
  routeId: string;
  method: 'GET' | 'POST';
  path: string;
  /** Guest authentication, Host and hop-by-hop headers are rejected. */
  headers?: Readonly<Record<string, string>>;
  body?: Buffer;
}>;
export type ResponseHeaders = Readonly<Record<string, string | string[]>>;
export type Destination = Readonly<{
  onHeaders: (status: number, headers: ResponseHeaders) => void;
  body: Writable;
}>;
export type Receipt = Readonly<{
  routeId: string;
  requestExposure: 'not_sent' | 'may_have_been_sent';
  requestBodyBytes: number;
  responseBodyBytes: number;
  upstreamStatus: number | null;
  outcome: 'completed' | 'failed';
}>;
