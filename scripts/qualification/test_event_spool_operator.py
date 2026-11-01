"""Operator safety decisions only; these tests do not establish SQLite durability."""
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import qualify_event_spool as operator


def statistics():
    limits = operator.pressure.LIMITS
    return {
        'pending_records': 0, 'pending_bytes': 0, 'database_bytes': 12288, 'wal_bytes': 0,
        'shm_bytes': 0, 'page_count': 3, 'database_bound_bytes': limits['max_pages'] * 4096,
        'wal_bound_bytes': 32 + (limits['max_pages'] + 32) * 4120, 'committed_through': 0,
        'contiguous_received_through': 0, 'highest_received': 0, 'gaps': [], 'failure_code': None,
        'node_version': 'v24.22.1', 'sqlite_version': '3.53.4',
        'sqlite_source_id': operator.pressure.SQLITE_SOURCE_ID, 'limits': dict(limits),
        'sqlite_compile_options': ['THREADSAFE=1'],
        'sqlite_compile_options_sha256': hashlib.sha256(b'["THREADSAFE=1"]').hexdigest(),
        'scope_sha256': 'a' * 64, 'configuration_sha256': 'b' * 64,
        'pragmas': dict(operator.PRAGMAS),
    }


class NativeOperatorSafety(unittest.TestCase):
    def qualification(self, folder):
        return operator.Qualification(Path(folder), Path(folder), {'observations': [], 'kills': []},
                                      data_run=Path(folder))

    def test_cli_requires_explicit_private_data_root(self):
        with patch('sys.argv', ['qualify_event_spool.py', '--root', '/example']), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error: operator.main()
        self.assertEqual(error.exception.code, 2)

    def test_cli_passes_source_and_live_data_roots_separately(self):
        arguments = ['qualify_event_spool.py', '--root', '/source', '--spool-data-root', '/private-live-data']
        with patch('sys.argv', arguments), patch.object(operator, 'execute', return_value=0) as execute:
            self.assertEqual(operator.main(), 0)
        execute.assert_called_once_with(Path('/source'), Path('/private-live-data'))

    def test_live_store_never_inherits_shared_source_checkout_permissions(self):
        with tempfile.TemporaryDirectory(dir=Path('/tmp').resolve()) as folder:
            root = Path(folder)
            checkout = root / 'shared-source'
            checkout.mkdir(mode=0o775)
            checkout.chmod(0o775)
            reports = checkout / 'reports'
            operator.private_directory(reports)
            live = root / 'live'
            operator.private_directory(live)
            q = operator.Qualification(checkout, reports, {}, data_run=live)
            evidence, directory, config, _ = q.store('replay')
            self.assertEqual(evidence, reports / 'replay')
            self.assertEqual(directory, live / 'replay/store')
            self.assertFalse(directory.is_relative_to(checkout))
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(config.parent, evidence)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(checkout.stat().st_mode & 0o777, 0o775)

    def test_source_bound_symlinks_and_hardlinks_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path('/tmp').resolve()) as folder:
            path = Path(folder) / 'source'
            path.write_bytes(b'12345')
            self.assertEqual(operator.bounded_read(path, 5), b'12345')
            with self.assertRaises(RuntimeError): operator.bounded_read(path, 4)
            linked = Path(folder) / 'link'
            linked.symlink_to(path)
            with self.assertRaises(RuntimeError): operator.bounded_read(linked, 5)
            linked.unlink()
            os.link(path, linked)
            with self.assertRaises(RuntimeError): operator.bounded_read(path, 5)

    def test_evidence_is_exclusive_private_and_source_snapshot_is_exact(self):
        with tempfile.TemporaryDirectory(dir=Path('/tmp').resolve()) as folder:
            root = Path(folder)
            source = root / 'input.py'
            source.write_bytes(b'exact source\n')
            reports = root / 'reports'
            operator.private_directory(reports)
            with patch.object(operator, 'SOURCE_FILES', ('input.py',)):
                hashes = operator.snapshot_sources(root, reports)
            saved = reports / 'source/input.py'
            self.assertEqual(saved.read_bytes(), source.read_bytes())
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            self.assertEqual(hashes['input.py'], hashlib.sha256(source.read_bytes()).hexdigest())
            with self.assertRaises(FileExistsError): operator.write(saved, b'replaced')
            source.write_bytes(b'changed')
            self.assertNotEqual(hashes['input.py'], hashlib.sha256(source.read_bytes()).hexdigest())

    def test_actual_readback_identity_and_native_bounds_are_required(self):
        with tempfile.TemporaryDirectory(dir=Path('/tmp').resolve()) as folder:
            q = self.qualification(folder)
            worker = SimpleNamespace(directory=Path(folder) / 'store', case='native')
            q.statistics(worker, statistics())
            changes = (
                {'wal_bytes': statistics()['wal_bound_bytes'] + 1}, {'shm_bytes': 1},
                {'pending_records': False}, {'sqlite_source_id': 'different'},
                {'pragmas': {**operator.PRAGMAS, 'cache_spill': 1}},
                {'pragmas': {**operator.PRAGMAS, 'cache_spill': False}},
                {'scope_sha256': 'c' * 64},
            )
            for change in changes:
                with self.subTest(change=change), self.assertRaises(RuntimeError):
                    q.statistics(worker, {**statistics(), **change})

    def test_replay_rejects_wrong_hash_cursor_order_and_gap_metadata(self):
        with tempfile.TemporaryDirectory(dir=Path('/tmp').resolve()) as folder:
            q = self.qualification(folder)
            body = b'exact event bytes'
            entry = {'source_seq': 2, 'sha256': hashlib.sha256(body).hexdigest(),
                     'body': base64.b64encode(body).decode()}
            stats = {**statistics(), 'pending_records': 1, 'pending_bytes': len(body),
                     'highest_received': 2, 'gaps': [{'from': 1, 'through': 1}]}
            reply = {'ok': True, 'stats': stats, 'batch': {**stats, 'events': [entry]}}
            q.rpc = lambda *_: json.loads(json.dumps(reply))
            q.retained(object(), {2: body})
            for mutation in ('hash', 'cursor', 'gap', 'duplicate'):
                bad = json.loads(json.dumps(reply))
                if mutation == 'hash': bad['batch']['events'][0]['sha256'] = '0' * 64
                elif mutation == 'cursor': bad['batch']['committed_through'] = 1
                elif mutation == 'gap': bad['batch']['gaps'] = []
                else: bad['batch']['events'].append(dict(entry))
                q.rpc = lambda *_, value=bad: value
                with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                    q.retained(object(), {2: body})

    def test_kill_never_targets_unregistered_or_exited_process(self):
        with tempfile.TemporaryDirectory(dir=Path('/tmp').resolve()) as folder:
            q = self.qualification(folder)
            process = SimpleNamespace(poll=lambda: None, kill=Mock())
            worker = SimpleNamespace(process=process)
            with self.assertRaises(RuntimeError): q.kill(worker, 'not-owned')
            process.kill.assert_not_called()
            q.workers.append(worker)
            process.poll = lambda: 0
            with self.assertRaises(RuntimeError): q.kill(worker, 'already-exited')
            process.kill.assert_not_called()


if __name__ == '__main__': unittest.main()
