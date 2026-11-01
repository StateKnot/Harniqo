/** One immutable owner and lease binding per store. A new binding needs a new store/stream. */
export type SpoolScope = Readonly<{
  organization_id: string;
  project_id: string;
  run_id: string;
  attempt_id: string;
  runtime_instance_id: string;
  stream_id: string;
  binding: Readonly<{ platform_epoch: string; lease_generation: number }>;
}>;

export type SpoolLimits = Readonly<{
  max_pages: number;
  max_pending_bytes: number;
  max_pending_records: number;
  max_event_bytes: number;
  max_batch_bytes: number;
  max_batch_records: number;
  max_forward_gap: number;
}>;

export type SpoolOptions = Readonly<{
  /** Existing, private, local Linux directory; never a network filesystem. */
  directory: string;
  scope: SpoolScope;
  limits?: Partial<SpoolLimits>;
}>;

export type AppendEvent = Readonly<{
  source_seq: number;
  /** Exact UTF-8 JSON SourceEvent bytes. Ingress must validate the full business contract. */
  body: Uint8Array;
}>;

/** This local receipt is neither a platform commit nor authorization to discard guest events. */
export type DurableReceipt = Readonly<{
  level: 'durable_received';
  source_seq: number;
  duplicate: boolean;
}>;

/** Only an authenticated, trusted uploader may supply this acknowledgement. */
export type CommittedAck = Readonly<{
  attempt_id: string;
  stream_id: string;
  binding: Readonly<{ platform_epoch: string; lease_generation: number }>;
  level: 'committed';
  committed_through: number;
}>;

export type PendingEvent = Readonly<{ source_seq: number; body: Uint8Array; sha256: string }>;
export type SequenceState = Readonly<{
  committed_through: number;
  contiguous_received_through: number;
  highest_received: number;
  /** Inclusive missing ranges, bounded by the finite pending-record limit. */
  gaps: readonly Readonly<{ from: number; through: number }>[];
}>;
export type PendingBatch = SequenceState & Readonly<{ events: readonly PendingEvent[] }>;
export type AckReceipt = Readonly<{
  committed_through: number;
  deleted_records: number;
  deleted_bytes: number;
}>;

export type SpoolErrorCode =
  | 'invalid_configuration' | 'unsupported_runtime' | 'unsafe_path'
  | 'store_exists' | 'store_missing' | 'incompatible_store' | 'store_locked'
  | 'integrity_failure' | 'invalid_event' | 'event_too_large' | 'event_scope_mismatch'
  | 'event_conflict' | 'event_already_committed' | 'forward_gap_exceeded'
  | 'queue_full' | 'storage_full' | 'invalid_ack' | 'ack_scope_mismatch' | 'ack_crosses_gap'
  | 'storage_io' | 'checkpoint_failed' | 'storage_bounds_exceeded' | 'faulted' | 'closed';

export type SpoolStats = SequenceState & Readonly<{
  pending_records: number;
  pending_bytes: number;
  database_bytes: number;
  wal_bytes: number;
  shm_bytes: number;
  page_count: number;
  database_bound_bytes: number;
  wal_bound_bytes: number;
  failure_code: SpoolErrorCode | null;
  node_version: string;
  sqlite_version: string;
  sqlite_source_id: string;
  sqlite_compile_options: readonly string[];
  sqlite_compile_options_sha256: string;
  configuration_sha256: string;
  scope_sha256: string;
  limits: SpoolLimits;
  /** Actual connection settings read and checked before this observation. */
  pragmas: Readonly<Record<string, string | number>>;
}>;
