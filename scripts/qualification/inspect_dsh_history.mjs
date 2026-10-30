// Readonly, no-network supplement. The actual SDK resume already completed;
// now verify its retained compressed native history against the live wire.
import fs from 'node:fs';
import { zstdDecompressSync } from 'node:zlib';
import { isDeepStrictEqual } from 'node:util';
import { createHash } from 'node:crypto';
const hash=v=>createHash('sha256').update(v).digest('hex');
const raw=fs.readFileSync('/run/session.zstd');
if(raw.length>4*1024*1024)throw Error('history_bound');
const decoded=zstdDecompressSync(raw,{maxOutputLength:4*1024*1024});
const rows=decoded.toString('utf8').trim().split('\n').map(JSON.parse);
const initial=fs.readFileSync('/run/initial-wire.jsonl','utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
const restored=fs.readFileSync('/run/restored-wire.jsonl','utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
const events=initial.filter(v=>v.method==='session.event').map(v=>v.params.event);
const persisted=rows.filter(v=>!['session','session/end-seed'].includes(v.type));
if(rows[0].type!=='session'||rows[0].version!==4||!initial.filter(v=>v.method==='session.event').every(v=>v.params.sessionId===rows[0].id)
  ||!isDeepStrictEqual(events,persisted))throw Error('native_history_diverged');
if(restored.some(v=>v.method==='session.event'&&['turn/start','request/header','assistant/message','tool/call'].includes(v.params.event.type)))throw Error('resume_generated_new_turn');
const output={schema_version:1,status:'passed',scope:'readonly_post_resume_history_supplement',model_requests:0,
  native_session_id:rows[0].id,native_format_version:4,compressed_bytes:raw.length,compressed_sha256:hash(raw),
  stored_rows:rows.length,live_events:events.length,all_initial_native_events_preserved:true,
  restored_wire_has_no_new_turn:true,tool_calls:events.filter(e=>e.type==='tool/call').length};
console.log(JSON.stringify(output));
