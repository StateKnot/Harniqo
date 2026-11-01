/** Private stdio acceptance fixture, never a production RPC endpoint or control plane. */
import { constants, closeSync, fstatSync, openSync, readSync, writeSync } from 'node:fs';
import { EventSpool, EventSpoolError } from '../../apps/runner/event-spool/spool.ts';
import { DatabaseSync } from 'node:sqlite';
import { once } from 'node:events';

const INPUT_BYTES = 128 * 1024;
const OUTPUT_BYTES = 2 * 1024 * 1024;
const argv = process.argv.slice(2);
const options = {};
let reopen = false;
let pause = false;
for (let index = 0; index < argv.length; index++) {
  const key = argv[index];
  if (key === '--open') { reopen = true; continue; }
  if (key === '--pause-before-commit') { pause = true; continue; }
  if (!['--directory', '--config'].includes(key) || !argv[index + 1] || options[key]) throw new Error('worker_arguments_invalid');
  options[key] = argv[++index];
}
if (!options['--directory'] || !options['--config']) throw new Error('worker_arguments_invalid');

function privateConfig(path) {
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_CLOEXEC);
  try {
    const info = fstatSync(fd);
    if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o777) !== 0o600 || info.nlink !== 1 || info.size > 65536) throw new Error('worker_configuration_invalid');
    const buffer = Buffer.alloc(65537);
    const length = readSync(fd, buffer, 0, buffer.length, 0);
    if (length !== info.size) throw new Error('worker_configuration_invalid');
    return JSON.parse(buffer.subarray(0, length).toString('utf8'));
  } finally { closeSync(fd); }
}

const config = privateConfig(options['--config']);
let spool;
try {
  spool = EventSpool[reopen ? 'open' : 'create']({ directory: options['--directory'], scope: config.scope, limits: config.limits });
} catch (error) {
  writeSync(1, JSON.stringify({ kind: 'startup_failed', code: error instanceof EventSpoolError ? error.code : 'worker_startup_failed' }) + '\n');
  process.exit(2);
}
let requestId = null;
let armed = false;
// Only the explicit fault fixture pauses before the real SQLite COMMIT. All SQL
// and data writes remain the production Module's; no success is fabricated.
if (pause) {
  const prepare = DatabaseSync.prototype.prepare;
  DatabaseSync.prototype.prepare = function(sql) {
    const statement = prepare.call(this, sql);
    if (armed && sql.trim().toUpperCase() === 'COMMIT') {
      statement.run = function() {
        armed = false;
        writeSync(1, JSON.stringify({ kind: 'paused_before_commit', pid: process.pid, id: requestId }) + '\n');
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 20000);
        throw new EventSpoolError('storage_io', false);
      };
      // This branch intentionally never invokes run: it is the exact precommit
      // pause, followed by operator SIGKILL, not a fabricated successful commit.
    }
    return statement;
  };
}

async function write(value) {
  const bytes = Buffer.from(JSON.stringify(value) + '\n');
  if (bytes.length > OUTPUT_BYTES) throw new Error('worker_output_bound');
  if (!process.stdout.write(bytes)) {
    let timer;
    try {
      await Promise.race([once(process.stdout, 'drain'), new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('worker_output_deadline')), 3000);
      })]);
    } finally { clearTimeout(timer); }
  }
}

function stats() { try { return spool.stats(); } catch { return null; } }
function decodeBody(value) {
  if (typeof value !== 'string' || value.length > INPUT_BYTES || value.length % 4 !== 0) throw new Error('worker_body_invalid');
  const body = Buffer.from(value, 'base64');
  if (body.toString('base64') !== value) throw new Error('worker_body_invalid');
  return body;
}

let closed = false;
await write({ kind: 'ready', pid: process.pid });
let pending = Buffer.alloc(0);
try {
  for await (const chunk of process.stdin) {
    pending = Buffer.concat([pending, chunk]);
    if (pending.length > INPUT_BYTES) throw new Error('worker_input_bound');
    while (pending.includes(10)) {
      const newline = pending.indexOf(10);
      const line = pending.subarray(0, newline); pending = pending.subarray(newline + 1);
      const request = JSON.parse(line.toString('utf8'));
      if (!request || typeof request !== 'object' || !/^[0-9a-f]{40}$/.test(request.id)) throw new Error('worker_request_invalid');
      requestId = request.id;
      try {
        let receipt;
        let batch;
        if (request.operation === 'append') {
          armed = pause;
          receipt = spool.append({ source_seq: request.source_seq, body: decodeBody(request.body) });
        } else if (request.operation === 'ack') {
          armed = pause;
          receipt = spool.applyCommittedAck(request.ack);
        } else if (request.operation === 'batch') {
          const result = spool.pendingBatch();
          batch = { ...result, events: result.events.map(event => ({ ...event, body: Buffer.from(event.body).toString('base64') })) };
        } else if (request.operation !== 'stats' && request.operation !== 'close') throw new Error('worker_operation_invalid');
        const beforeClose = stats();
        if (request.operation === 'close') { spool.close(); closed = true; }
        await write({ id: requestId, ok: true, receipt, batch, stats: beforeClose });
        if (closed) break;
      } catch (error) {
        await write({ id: requestId, ok: false, code: error instanceof EventSpoolError ? error.code : 'worker_request_failed',
          mayHaveCommitted: error instanceof EventSpoolError ? error.mayHaveCommitted : true, stats: stats() });
      }
    }
    if (closed) break;
  }
  if (pending.length) throw new Error('worker_partial_frame');
} finally {
  if (!closed) { spool.close(); closed = true; }
  process.stdin.destroy();
}
