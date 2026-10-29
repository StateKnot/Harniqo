#!/usr/bin/env python3
"""Explicit, temporary host INPUT exception for the owned KVM TLS canary only.

This is an administrator-authorized qualification fixture, not production egress.
Passwords stay in server memory and sudo stdin. Full policies stay in private files.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import resource
import shlex
import signal
import stat
import subprocess
import uuid
import xml.etree.ElementTree as ET


class PolicyError(RuntimeError):
    pass


def credential_bytes(path: Path) -> bytearray:
    for item in (path, *path.parents):
        if item.is_symlink():
            raise PolicyError("credential_symlink")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or not 1 <= before.st_size <= 4096):
            raise PolicyError("credential_metadata")
        value = bytearray(os.read(descriptor, 4097))
        after = os.fstat(descriptor)
        current = path.lstat()
        if (len(value) != before.st_size
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
            raise PolicyError("credential_changed")
    finally:
        os.close(descriptor)
    while value and value[-1] in (10, 13):
        value.pop()
    if not value or any(c in (0, 10, 13) for c in value):
        raise PolicyError("credential_format")
    value.append(10)
    return value


class PrivateSudo:
    def __init__(self, credential: Path, evidence: Path):
        self.credential, self.evidence, self.serial = credential, evidence, 0
        self.pending = None

    def quiesce(self):
        """Never infer a privileged writer stopped merely from a rule being absent.

        Unlike subprocess.run(timeout=...), do not SIGKILL sudo: that could leave
        its privileged child alive. A signalled sudo exit is also unresolved.
        """
        if self.pending is None:
            return
        process = self.pending
        try:
            status = process.wait(timeout=35)
        except subprocess.TimeoutExpired:
            raise PolicyError("privileged_writer_still_pending") from None
        if status < 0:
            raise PolicyError("privileged_writer_exit_unconfirmed")
        self.pending = None

    def __call__(self, label: str, arguments: list[str]) -> str:
        self.quiesce()
        self.serial += 1
        basename = f"{self.serial:03d}-{label}"
        secret = credential_bytes(self.credential)
        try:
            with (self.evidence / (basename + ".stdout")).open("xb") as out, \
                    (self.evidence / (basename + ".stderr")).open("xb") as err:
                os.fchmod(out.fileno(), 0o600); os.fchmod(err.fileno(), 0o600)
                process = None
                try:
                    # Register the sudo child before any handled signal can
                    # transfer control to the operator's cleanup path.
                    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK,
                                                          {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
                    try:
                        def bounded_output():
                            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                            resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024**2, 16 * 1024**2))
                            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                        process = subprocess.Popen(["/usr/bin/sudo", "-S", "-p", "", "--", *arguments],
                                                   stdin=subprocess.PIPE, stdout=out, stderr=err,
                                                   env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
                                                   preexec_fn=bounded_output)
                        self.pending = process
                    finally:
                        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                    process.communicate(input=secret, timeout=30)
                except BaseException:
                    if process is not None and process.stdin and not process.stdin.closed:
                        process.stdin.close()
                    raise
                if process.returncode < 0:
                    raise PolicyError("privileged_writer_exit_unconfirmed")
                self.pending = None
            if process.returncode:
                raise PolicyError("privileged_command_failed:" + label)
            return (self.evidence / (basename + ".stdout")).read_text()
        finally:
            for i in range(len(secret)):
                secret[i] = 0


def canonical_rule(tokens: list[str]) -> tuple:
    values, modules = {}, []
    while tokens:
        key, *tokens = tokens
        if not tokens or key not in {"-A", "-i", "-s", "-d", "-p", "-m", "--dport", "--ctstate", "--comment", "-j"}:
            raise PolicyError("unexpected_owned_rule_selector")
        value, *tokens = tokens
        if key == "-m":
            modules.append(value)
        else:
            if key in values:
                raise PolicyError("duplicate_owned_rule_selector")
            if key == "--ctstate":
                value = ",".join(sorted(value.split(",")))
            elif key in {"-s", "-d"}:
                value = str(ipaddress.ip_network(value, strict=False))
            values[key] = value
    return tuple(sorted(values.items())), tuple(sorted(modules))


def normalized_nft(value):
    if isinstance(value, list):
        return [normalized_nft(v) for v in value if not isinstance(v, dict) or "metainfo" not in v]
    if isinstance(value, dict):
        return {k: (normalized_nft({a: b for a, b in v.items() if a not in {"packets", "bytes"}})
                    if k == "counter" and isinstance(v, dict) else normalized_nft(v))
                for k, v in value.items() if k != "handle"}
    return value


class OwnedInputPolicy:
    def __init__(self, root: Path, reports: Path, network: dict, subnet, instances: list,
                 actual_xml: str, runner=None):
        subnet = ipaddress.ip_network(subnet)
        identity = str(uuid.UUID(network["uuid"]))
        tree = ET.fromstring(actual_xml)
        if (subnet.version != 4 or subnet.prefixlen != 28 or len(instances) != 2
                or not re.fullmatch(r"harniqo-net-[a-f0-9]{12}", network["name"])
                or network["bridge"] != "hqn" + network["name"].removeprefix("harniqo-net-")[:10]
                or tree.findtext("uuid") != identity or tree.findtext("name") != network["name"]
                or tree.find("bridge").get("name") != network["bridge"]
                or tree.find("ip").get("address") != str(subnet[1])
                or tree.find("ip").get("prefix") != "28" or tree.find("forward") is not None
                or tree.find("dns").get("enable") != "no" or tree.findall(".//dhcp")
                or tree.find("port").get("isolated") != "yes"
                or [v["ip"] for v in instances] != [str(subnet[2]), str(subnet[3])]):
            raise PolicyError("owned_topology_required")
        self.evidence = reports / "host-input-policy"
        self.evidence.mkdir(mode=0o700)
        self.runner = runner or PrivateSudo(root / ".private/admin-sudo-password", self.evidence)
        self.rules = []
        for n, item in enumerate(instances):
            self.rules.append(["-i", network["bridge"], "-s", item["ip"] + "/32",
                               "-d", str(subnet[1]) + "/32", "-p", "tcp", "-m", "tcp", "--dport", "10018",
                               "-m", "conntrack", "--ctstate", "NEW,ESTABLISHED", "-m", "comment",
                               "--comment", "harniqo-m0-canary:" + identity + ":" + str(n), "-j", "ACCEPT"])
        self.receipt = {"scope": "temporary_owned_canary_input_only", "network_uuid": identity,
                        "bridge": network["bridge"], "rules": self.rules, "applied": False,
                        "restored": False, "events": [], "persistent_policy_changed": False}
        self.before = None
        self.attempted = []
        self._save()

    def _save(self):
        temporary = self.evidence / "receipt.pending"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.receipt, stream, indent=2); stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        temporary.replace(self.evidence / "receipt.json")

    def _rules(self, label):
        return self.runner(label, ["/usr/sbin/iptables", "--wait", "5", "-S"]).splitlines()

    def _snapshot(self, label):
        rules = self._rules(label + "-filter")
        nft = json.loads(self.runner(label + "-nft", ["/usr/sbin/nft", "-j", "-a", "list", "ruleset"]))
        self.runner(label + "-iptables-save", ["/usr/sbin/iptables-save", "-c"])
        return {"filter": rules, "nft": normalized_nft(nft)}

    def _find(self, rules, expected):
        comment = expected[expected.index("--comment") + 1]
        matches = []
        for line in rules:
            tokens = shlex.split(line)
            if "--comment" in tokens and tokens[tokens.index("--comment") + 1] == comment:
                matches.append(tokens)
        if len(matches) > 1:
            raise PolicyError("owned_rule_not_unique")
        if matches and canonical_rule(matches[0]) != canonical_rule(["-A", "ufw-user-input", *expected]):
            raise PolicyError("owned_rule_identity_changed")
        return bool(matches)

    def apply(self):
        if self.runner("sudo-identity", ["/usr/bin/id", "-u"]).strip() != "0":
            raise PolicyError("sudo_identity")
        if "nf_tables" not in self.runner("iptables-backend", ["/usr/sbin/iptables", "--version"]):
            raise PolicyError("expected_iptables_nft_backend")
        self.before = self._snapshot("before")
        if "-N ufw-user-input" not in self.before["filter"] or "-P INPUT DROP" not in self.before["filter"]:
            raise PolicyError("unexpected_input_chain_or_policy")
        if any(self._find(self.before["filter"], rule) for rule in self.rules):
            raise PolicyError("owned_rule_preexists")
        for index, rule in enumerate(self.rules):
            self.receipt["events"].append({"operation": "insert_intent", "rule": index}); self._save()
            self.attempted.append(index)
            try:
                self.runner("insert-" + str(index), ["/usr/sbin/iptables", "--wait", "5", "-I", "ufw-user-input", "1", *rule])
                if not self._find(self._rules("verify-insert-" + str(index)), rule):
                    raise PolicyError("insert_not_observed")
            except BaseException:
                self.receipt["events"].append({"operation": "insert_failed", "rule": index}); self._save()
                raise
        self.receipt["applied"] = True; self._save()

    def restore(self):
        errors = []
        try:
            if hasattr(self.runner, "quiesce"):
                self.runner.quiesce()
        except Exception as error:
            self.receipt.update(restored=False, pending_privileged_writer=True,
                                cleanup_errors=[{"error_type": type(error).__name__}])
            self._save()
            raise PolicyError("privileged_writer_unsettled_preserve_resources") from None
        if not self.attempted:
            self.receipt["restored"] = True; self._save()
            return
        for index in self.attempted:
            rule = self.rules[index]
            try:
                if self._find(self._rules("before-delete-" + str(index)), rule):
                    try:
                        self.runner("delete-" + str(index), ["/usr/sbin/iptables", "--wait", "5", "-D", "ufw-user-input", *rule])
                    except Exception:
                        self.receipt["events"].append({"operation": "delete_dispatch_error", "rule": index})
                    if self._find(self._rules("verify-delete-" + str(index)), rule):
                        raise PolicyError("owned_rule_remains")
                self.receipt["events"].append({"operation": "absent_confirmed", "rule": index})
            except Exception as error:
                errors.append({"rule": index, "error_type": type(error).__name__})
        try:
            after = self._snapshot("after")
            self.receipt["preexisting_filter_unchanged"] = self.before is not None and after["filter"] == self.before["filter"]
            self.receipt["effective_ruleset_unchanged"] = self.before is not None and after["nft"] == self.before["nft"]
            if self.before is not None and after != self.before:
                errors.append({"error_type": "EffectivePolicyChanged"})
        except Exception as error:
            errors.append({"error_type": type(error).__name__})
        self.receipt["cleanup_errors"] = errors
        self.receipt["restored"] = not errors
        self._save()
        if errors:
            raise PolicyError("owned_host_input_cleanup_unconfirmed")
