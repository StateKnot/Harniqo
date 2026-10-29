#!/usr/bin/env python3
"""No sudo, sockets, VM, Docker or real firewall changes: owned policy failure seams."""
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import kvm_host_policy as host_policy

from kvm_host_policy import OwnedInputPolicy, PolicyError, PrivateSudo, canonical_rule, credential_bytes, normalized_nft
from qualify_kvm_network import network_xml


class FakePolicy:
    def __init__(self):
        self.base = ["-P INPUT DROP", "-P FORWARD DROP", "-N ufw-user-input"]
        self.rows, self.calls, self.fail, self.foreign = [], [], None, False
        self.backend = "iptables v1.8.10 (nf_tables)"

    def __call__(self, label, arguments):
        self.calls.append((label, arguments))
        if label == "sudo-identity": return "0\n"
        if label == "iptables-backend": return self.backend
        if arguments[-1] == "-S": return "\n".join(self.base + self.rows)
        if arguments[0].endswith("nft"):
            return json.dumps({"nftables": [{"rule": {"chain": "ufw-user-input", "spec": row}}
                                           for row in self.rows] + ([{"foreign": True}] if self.foreign else [])})
        if arguments[0].endswith("iptables-save"): return "private snapshot\n"
        if "-I" in arguments:
            at = arguments.index("-I")
            row = "-A " + arguments[at + 1] + " " + " ".join(arguments[at + 3:])
            self.rows.insert(0, row)
        elif "-D" in arguments:
            at = arguments.index("-D")
            row = "-A " + arguments[at + 1] + " " + " ".join(arguments[at + 2:])
            self.rows.remove(row)
        else: raise AssertionError("unexpected operation")
        if label == self.fail: raise TimeoutError("unknown dispatch result")
        return ""


class OwnedPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.network = {"uuid": "d3bf5a0e-2758-459e-a065-80dd5357d9d9",
                        "name": "harniqo-net-123456abcdef", "bridge": "hqn123456abcd"}
        self.subnet = ipaddress.ip_network("172.29.240.0/28")
        self.guests = [{"ip": "172.29.240.2"}, {"ip": "172.29.240.3"}]
        self.fake = FakePolicy()
        self.policy = OwnedInputPolicy(self.root, self.root, self.network, self.subnet,
                                       self.guests, network_xml(self.network, self.subnet), self.fake)

    def test_exact_two_runtime_rules_and_complete_restore(self):
        self.policy.apply()
        self.assertTrue(self.policy.receipt["applied"])
        self.assertEqual(len(self.fake.rows), 2)
        for row in self.fake.rows:
            self.assertIn("-i hqn123456abcd", row)
            self.assertIn("-d 172.29.240.1/32", row)
            self.assertIn("--dport 10018", row)
            self.assertNotIn("10019", row)
        self.policy.restore()
        self.assertEqual(self.fake.rows, [])
        self.assertTrue(self.policy.receipt["effective_ruleset_unchanged"])
        self.assertEqual((self.root / "host-input-policy/receipt.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse(any("-F" in args or "-P" in args for _, args in self.fake.calls))

    def test_unknown_insert_result_does_not_admit_and_is_reconciled(self):
        self.fake.fail = "insert-1"
        with self.assertRaises(TimeoutError): self.policy.apply()
        self.assertFalse(self.policy.receipt["applied"])
        self.policy.restore()
        self.assertEqual(self.fake.rows, [])
        self.assertTrue(self.policy.receipt["restored"])

    def test_unknown_delete_result_is_confirmed_absent(self):
        self.policy.apply(); self.fake.fail = "delete-0"
        self.policy.restore()
        self.assertTrue(self.policy.receipt["restored"])
        self.assertEqual(self.fake.rows, [])

    def test_preexisting_comment_is_never_claimed_or_removed(self):
        self.fake.rows = ["-A ufw-user-input " + " ".join(self.policy.rules[0])]
        before = list(self.fake.rows)
        with self.assertRaisesRegex(PolicyError, "preexists"): self.policy.apply()
        self.policy.restore()
        self.assertEqual(self.fake.rows, before)
        self.assertFalse(any("-I" in args or "-D" in args for _, args in self.fake.calls))

    def test_changed_selector_and_duplicate_comment_refuse_deletion(self):
        self.policy.apply()
        self.fake.rows[0] = self.fake.rows[0].replace("--dport 10018", "--dport 10019")
        with self.assertRaises(PolicyError): self.policy.restore()
        self.assertEqual(len(self.fake.rows), 1)
        self.assertIn("--dport 10019", self.fake.rows[0])
        self.fake.rows.append(self.fake.rows[0])
        with self.assertRaisesRegex(PolicyError, "not_unique"):
            self.policy._find(self.fake.rows, self.policy.rules[1])

    def test_concurrent_foreign_change_is_reported_not_reverted(self):
        self.policy.apply(); self.fake.foreign = True
        with self.assertRaises(PolicyError): self.policy.restore()
        self.assertEqual(self.fake.rows, [])
        self.assertTrue(self.fake.foreign)
        self.assertFalse(self.policy.receipt["effective_ruleset_unchanged"])

    def test_backend_mismatch_performs_no_mutation(self):
        self.fake.backend = "iptables legacy"
        with self.assertRaisesRegex(PolicyError, "backend"): self.policy.apply()
        self.policy.restore()
        self.assertFalse(any("-I" in args or "-D" in args for _, args in self.fake.calls))

    def test_other_bridge_or_guest_is_not_authorized(self):
        for changed in [dict(self.network, bridge="virbr0"), dict(self.network, name="existing")]:
            with self.assertRaises(PolicyError):
                OwnedInputPolicy(self.root, self.root, changed, self.subnet, self.guests,
                                 network_xml(self.network, self.subnet), self.fake)

    def test_counter_and_handle_changes_do_not_hide_policy_changes(self):
        a = {"nftables": [{"rule": {"handle": 1, "expr": [{"counter": {"packets": 3, "bytes": 10}}, {"accept": None}]}}]}
        b = {"nftables": [{"rule": {"handle": 2, "expr": [{"counter": {"packets": 4, "bytes": 20}}, {"accept": None}]}}]}
        self.assertEqual(normalized_nft(a), normalized_nft(b))
        b["nftables"][0]["rule"]["expr"][-1] = {"drop": None}
        self.assertNotEqual(normalized_nft(a), normalized_nft(b))

    def test_named_counter_identity_and_binding_are_retained(self):
        a = {"counter": {"name": "existing", "table": "filter", "packets": 1, "bytes": 2}}
        b = {"counter": {"name": "replacement", "table": "filter", "packets": 1, "bytes": 2}}
        self.assertNotEqual(normalized_nft(a), normalized_nft(b))
        self.assertNotEqual(normalized_nft({"counter": "existing"}), normalized_nft({"counter": "replacement"}))

    def test_unsettled_writer_blocks_cleanup_even_when_rule_absent(self):
        self.policy.apply()
        self.fake.rows = []
        self.fake.quiesce = Mock(side_effect=PolicyError("late root writer"))
        before = len(self.fake.calls)
        with self.assertRaisesRegex(PolicyError, "unsettled"): self.policy.restore()
        self.assertFalse(self.policy.receipt["restored"])
        self.assertEqual(len(self.fake.calls), before)

    def test_signalled_sudo_exit_does_not_prove_child_exit(self):
        runner = PrivateSudo(self.root / "unused", self.root)
        process = Mock(); process.wait.return_value = -9
        runner.pending = process
        with self.assertRaisesRegex(PolicyError, "exit_unconfirmed"): runner.quiesce()
        self.assertIs(runner.pending, process)
        process.wait.return_value = 1
        runner.quiesce()
        self.assertIsNone(runner.pending)

    def test_launch_interrupt_cannot_escape_pending_registration(self):
        runner = PrivateSudo(self.root / "unused", self.root)
        process = Mock(); process.stdin.closed = False
        def mask(operation, values):
            if operation == host_policy.signal.SIG_BLOCK: return set()
            raise KeyboardInterrupt("delivered after launch registration")
        with patch.object(host_policy, "credential_bytes", return_value=bytearray(b"synthetic\n")), \
                patch.object(host_policy.signal, "pthread_sigmask", side_effect=mask), \
                patch.object(host_policy.subprocess, "Popen", return_value=process):
            with self.assertRaises(KeyboardInterrupt):
                runner("interrupt-fixture", ["/usr/bin/id", "-u"])
        self.assertIs(runner.pending, process)
        process.stdin.close.assert_called_once()
        process.communicate.assert_not_called()
        process.kill.assert_not_called()

    def test_kernel_conntrack_order_and_selector_order_are_canonical(self):
        first = ["-A", "ufw-user-input", *self.policy.rules[0]]
        second = list(first)
        second[second.index("--ctstate") + 1] = "ESTABLISHED,NEW"
        self.assertEqual(canonical_rule(first), canonical_rule(second))
        second[second.index("--dport") + 1] = "10019"
        self.assertNotEqual(canonical_rule(first), canonical_rule(second))


class CredentialTests(unittest.TestCase):
    def test_private_regular_single_link_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "synthetic-credential"
            path.write_bytes(b"synthetic-test-only\n"); path.chmod(0o600)
            self.assertEqual(credential_bytes(path), b"synthetic-test-only\n")
            path.chmod(0o664)
            with self.assertRaisesRegex(PolicyError, "metadata"): credential_bytes(path)
            path.chmod(0o600)
            link = path.with_name("hardlink"); os.link(path, link)
            with self.assertRaisesRegex(PolicyError, "metadata"): credential_bytes(path)
            link.unlink(); link.symlink_to(path)
            with self.assertRaisesRegex(PolicyError, "symlink"): credential_bytes(link)


if __name__ == "__main__": unittest.main()
