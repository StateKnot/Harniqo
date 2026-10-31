"""Synthetic pressure-gate regressions; no VM, native libvirt or host cgroups.

Real temporary console logs retain the queued-byte boundary. Fake monotonic
time and resource samples exercise qualification decisions, not VM isolation.
"""
import builtins
import json
import os
from pathlib import Path
import selectors
import socket
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import qualify_resource_broker as operator


class Clock:
    def __init__(self):
        self.now = 0.0

    def advance(self, seconds):
        self.now += seconds
        return self.now


class Samples:
    """A continuously busy one-core VM, including time spent reading samples."""
    def __init__(self, clock, *, latency=0, changed_identity=None):
        self.clock = clock
        self.latency = latency
        self.changed_identity = changed_identity
        self.calls = {}

    def resource_limits(self, resource_id):
        self.clock.advance(self.latency)
        count = self.calls.get(resource_id, 0) + 1
        self.calls[resource_id] = count
        sample = {
            'pid': 100 if resource_id == 'a' else 200,
            'process_start_time': 7,
            'cgroup_path': '/machine.slice/' + resource_id,
            'cpu_stat_monotonic': self.clock.now,
            'cpu_stat': {'usage_usec': round(self.clock.now * 1e6),
                         'nr_throttled': round(self.clock.now * 100),
                         'throttled_usec': round(self.clock.now * 10000)},
            'memory_current_bytes': 100,
            'memory_peak_bytes': 200,
            'memory_max_bytes': 300,
            'memory_events': {'oom': 0, 'oom_kill': 0},
        }
        if self.changed_identity and count == 2:
            target, field = self.changed_identity
            if resource_id == target:
                sample[field] = '/different.scope' if field == 'cgroup_path' else sample[field] + 1
        return sample


class Streams:
    """Expose a report connection and console bytes through real log files."""
    def __init__(self, clock, instances, logs, *, reply='matching', queued=b''):
        self.clock = clock
        self.instances = instances
        self.logs = {}
        self.files = dict(zip((item['uuid'] for item in instances), logs))
        self.reply = reply
        self.queued = queued
        self.sent = []
        self.pending = []
        for instance in instances:
            identity = instance['uuid']
            self.files[identity].seek(0)
            self.files[identity].truncate()
            self.logs[identity] = {'fd': self.files[identity].fileno(), 'bytes_saved': 0}
            self.append(identity, 'HARNIQO-RESOURCE-START ' + instance['nonce'] + '\n')
        channel = SimpleNamespace(sendall=self.send)
        self.report_channels = {instances[1]['uuid']: channel}

    def append(self, identity, value):
        data = value.encode('ascii') if isinstance(value, str) else value
        self.files[identity].write(data)
        self.files[identity].flush()
        self.logs[identity]['bytes_saved'] += len(data)

    def send(self, payload):
        self.sent.append(payload)
        self.pending.append(payload)

    def _poll(self, timeout):
        self.clock.advance(timeout)
        control = self.instances[1]
        if self.queued:
            self.append(control['uuid'], self.queued)
            self.queued = b''
        for payload in self.pending:
            if self.reply == 'none':
                continue
            nonce = control['nonce'] if self.reply == 'matching' else 'foreign-nonce'
            self.append(control['uuid'], 'HARNIQO-HEARTBEAT ' + nonce + ' ' + payload.decode('ascii'))
        self.pending.clear()


class ResourcePressureTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.instances = [{'uuid': 'a', 'nonce': 'nonce-a'}, {'uuid': 'b', 'nonce': 'nonce-b'}]
        self.files = [tempfile.TemporaryFile() for _ in self.instances]
        for stream in self.files:
            self.addCleanup(stream.close)

    def measure(self, *, latency=0, reply='matching', queued=b'', changed_identity=None):
        streams = Streams(self.clock, self.instances, self.files, reply=reply, queued=queued)
        broker = Samples(self.clock, latency=latency, changed_identity=changed_identity)
        result = {'checks': {}}
        with patch.object(operator.time, 'monotonic', lambda: self.clock.now):
            operator.resource_pressure(broker, self.instances, streams, result)
        return result, streams

    def test_queued_legacy_heartbeats_cannot_prove_liveness_in_the_new_window(self):
        queued = b'HARNIQO-HEARTBEAT nonce-b\n' * 3
        streams = Streams(self.clock, self.instances, self.files, reply='none', queued=queued)
        result = {'checks': {}}
        with patch.object(operator.time, 'monotonic', lambda: self.clock.now):
            with self.assertRaisesRegex(RuntimeError, 'liveness predicate failed'):
                operator.resource_pressure(Samples(self.clock, latency=1), self.instances, streams, result)
        self.assertEqual(len(streams.sent), 3)
        self.assertFalse(result['checks']['b_heartbeat_during_pressure'])
        self.assertEqual(result['resource_pressure']['b_heartbeats_delta'], 0)
        self.files[1].seek(0)
        self.assertIn(queued, self.files[1].read())

    def test_slow_sampling_keeps_a_correct_one_core_cpu_measurement_valid(self):
        result, streams = self.measure(latency=1)
        sample = result['resource_pressure']
        self.assertTrue(all(result['checks'].values()))
        self.assertEqual(len(set(streams.sent)), 3)
        self.assertGreater(sample['seconds'], sample['challenge_window_seconds'] + 1.9)
        self.assertAlmostEqual(sample['a_cpu_usage_usec_delta'] / 1e6, sample['seconds'])
        self.assertEqual(sample['b_heartbeats_delta'], 3)

    def test_matching_challenges_from_another_guest_do_not_prove_liveness(self):
        streams = Streams(self.clock, self.instances, self.files, reply='foreign-nonce')
        result = {'checks': {}}
        with patch.object(operator.time, 'monotonic', lambda: self.clock.now):
            with self.assertRaisesRegex(RuntimeError, 'liveness predicate failed'):
                operator.resource_pressure(Samples(self.clock), self.instances, streams, result)
        self.assertFalse(result['checks']['b_heartbeat_during_pressure'])

    def test_changed_sample_identity_is_rejected_for_either_vm(self):
        for resource_id in ('a', 'b'):
            for field in ('pid', 'process_start_time', 'cgroup_path'):
                with self.subTest(resource_id=resource_id, field=field):
                    with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                        self.measure(changed_identity=(resource_id, field))

    def test_generated_control_fixture_answers_a_real_newline_delimited_challenge(self):
        original = {'write_files': [{'content': "import os,time\nC={'nonce':'nonce-b'}\n"}],
                    'runcmd': [['unused']]}
        with patch.object(operator.kvm, 'guest_config', return_value='#cloud-config\n' + json.dumps(original)):
            config = operator.sealed_guest_config(self.instances[1], self.instances[0], Path('/synthetic'),
                resource_profile='sealed-raw8g-q35-limited-v2')
        program = json.loads(config.split('\n', 1)[1])['bootcmd'][0][2]
        challenge = 'a' * 40
        output = []
        namespace = {'__builtins__': {**vars(builtins), 'print': lambda *args, **kwargs: output.append(' '.join(args))}}
        with patch.object(operator.os, 'open', return_value=77), \
                patch.object(operator.os, 'read', return_value=(challenge + '\n').encode('ascii')), \
                patch.object(operator.os, 'close'), \
                patch('select.select', return_value=([77], [], [])), \
                patch.object(operator.time, 'monotonic', lambda: self.clock.advance(1)):
            exec(compile(program, '<synthetic-control-guest>', 'exec'), namespace)
        self.assertIn('HARNIQO-HEARTBEAT nonce-b ' + challenge, output)


class ReportChannelIntegrationTests(unittest.TestCase):
    """Use the real collector and socketpairs without a VM or listening socket."""
    def setUp(self):
        self.instances = [{'uuid': name, 'name': name, 'nonce': 'nonce-' + name} for name in ('a', 'b')]

    def collected_streams(self, *, retain=None):
        temporary = tempfile.TemporaryDirectory(prefix='pressure-channel-')
        self.addCleanup(temporary.cleanup)
        for instance in self.instances:
            # Constructor listeners stay idle; the connected socketpairs below
            # begin at the same boundary as sockets accepted from QEMU.
            for field in ('listener', 'console_listener'):
                listener, peer = socket.socketpair()
                self.addCleanup(listener.close)
                self.addCleanup(peer.close)
                instance[field] = listener
        logs = []
        options = {} if retain is None else {'retain_report_channels': retain}
        streams = operator.kvm.GuestStreams(self.instances, Path(temporary.name), 1, logs, **options)
        for log in logs:
            self.addCleanup(os.close, log['fd'])
        self.addCleanup(streams._close)
        peers = {}
        hosts = {}
        for instance in self.instances:
            for role in ('report', 'console'):
                host, peer = socket.socketpair()
                host.setblocking(False)
                peer.settimeout(.5)
                self.addCleanup(peer.close)
                identity = (instance['uuid'], role)
                peers[identity] = peer
                hosts[identity] = host
                streams.connections.add(host)
                streams.selector.register(host, selectors.EVENT_READ, (instance, role, False))
                if role == 'report':
                    streams.buffers[host] = bytearray()
                    payload = json.dumps({'instance_uuid': instance['uuid'], 'nonce': instance['nonce']})
                else:
                    streams.logs[instance['uuid']]['connected'] = True
                    payload = 'HARNIQO-RESOURCE-START ' + instance['nonce']
                peer.sendall((payload + '\n').encode('ascii'))
        self.assertEqual(set(streams.collect(1)), {'a', 'b'})
        return streams, peers, hosts

    def test_default_v1_collector_closes_report_channels_after_the_initial_report(self):
        streams, peers, hosts = self.collected_streams()
        self.assertFalse(streams.report_channels)
        for identity in (('a', 'report'), ('b', 'report')):
            self.assertEqual(hosts[identity].fileno(), -1)
            self.assertEqual(peers[identity].recv(1), b'')

    def test_retained_report_channel_supports_pressure_after_real_report_collection(self):
        streams, peers, hosts = self.collected_streams(retain=True)
        self.assertEqual(set(streams.report_channels), {'a', 'b'})
        clock = Clock()
        poll = streams._poll
        control = peers[('b', 'report')]
        control.setblocking(False)
        challenges = []

        def answer_challenge(timeout):
            clock.advance(timeout)
            try:
                challenge = control.recv(4096)
            except BlockingIOError:
                challenge = b''
            if challenge:
                challenges.append(challenge)
                peers[('b', 'console')].sendall(b'HARNIQO-HEARTBEAT nonce-b ' + challenge)
            poll(0)

        result = {'checks': {}}
        with patch.object(operator.time, 'monotonic', lambda: clock.now), \
                patch.object(streams, '_poll', side_effect=answer_challenge):
            operator.resource_pressure(Samples(clock, latency=1), self.instances, streams, result)
        self.assertTrue(all(result['checks'].values()))
        self.assertEqual(len(set(challenges)), 3)
        self.assertEqual(result['resource_pressure']['b_heartbeats_delta'], 3)
        streams.finish()
        self.assertFalse(streams.report_channels)
        self.assertTrue(all(host.fileno() == -1 for host in hosts.values()))


if __name__ == '__main__':
    unittest.main()
