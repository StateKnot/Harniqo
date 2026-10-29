#!/usr/bin/env python3
"""Launch one private, bounded native qualification case and confirm cleanup.

This is a trusted qualification operator, not a production execution backend.
No model credentials, endpoint or container logs are printed. Probe results and
container cleanup are independent: a failed test still requires a stopped worker.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import resource
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.parse

from probe_hermes import (API, COMMIT, IMAGE, Rejected, digest, json_value,
                          no_links, private_directory, read_file, require, write_report)

TASK_LABEL = "io.harniqo.qualification-attempt"
DOCKER_HOST = "unix:///var/run/docker.sock"
INSPECT = ('{"id":{{json .Id}},"image":{{json .Image}},'
           '"running":{{json .State.Running}},"pid":{{json .State.Pid}},'
           '"attempt":{{json (index .Config.Labels "' + TASK_LABEL + '")}},'
           '"ports":{{json .NetworkSettings.Ports}}}')


def docker(*arguments, timeout=30, check=True):
    result = subprocess.run(["docker", "--host", DOCKER_HOST, *arguments],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    require(len(result.stdout) <= 65536 and len(result.stderr) <= 65536, "docker_output_limit")
    if check:
        require(result.returncode == 0, "docker_command_failed")
    return result


def inspect(name):
    result = docker("inspect", "--format", INSPECT, name, check=False)
    if result.returncode:
        # Absence must be proved independently; daemon/network errors are unknown.
        listed = docker("ps", "-a", "--no-trunc", "--filter", "name=^/" + name + "$",
                        "--format", "{{.ID}}")
        require(not listed.stdout.strip(), "container_inspection_unavailable")
        return None
    return json_value(result.stdout)


def owned(value, attempt, image_id, expected_id=None):
    require(isinstance(value, dict) and value.get("attempt") == attempt
            and value.get("image") == image_id
            and re.fullmatch(r"[0-9a-f]{64}", value.get("id", ""))
            and (expected_id is None or value["id"] == expected_id), "container_ownership_mismatch")
    return value["id"]


def capture_logs(identity, case, phase):
    """Bound output at the OS file limit; archive private bytes without printing."""
    path = case / ("native-" + phase + ".log")
    result = {"path": path.name, "complete": False}
    try:
        def bounded_file():
            resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024**2, 4 * 1024**2))
        with path.open("xb") as output:
            os.chmod(path, 0o600)
            process = subprocess.run(["docker", "--host", DOCKER_HOST, "logs", "--timestamps", identity],
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                     timeout=20, preexec_fn=bounded_file)
            output.flush()
            os.fsync(output.fileno())
        raw = read_file(path, 4 * 1024**2)
        result.update(complete=process.returncode == 0, exit_code=process.returncode,
                      bytes=len(raw), sha256=digest(raw))
    except BaseException as error:
        result["failure"] = {"code": type(error).__name__}
    return result


def cleanup(name, attempt, image_id, expected_id=None, case=None):
    value = inspect(name)
    if value is None:
        return {"container_absent": True, "execution_stop_confirmed": False,
                "reason": "no_observed_container"}
    identity = owned(value, attempt, image_id, expected_id)
    logs = [capture_logs(identity, case, "before-stop")] if case is not None else []
    if value.get("running") or value.get("pid") != 0:
        docker("stop", "--time", "10", identity, timeout=25)
    stopped = inspect(identity)
    owned(stopped, attempt, image_id, identity)
    require(stopped.get("running") is False and stopped.get("pid") == 0,
            "container_stop_not_confirmed")
    if case is not None:
        logs.append(capture_logs(identity, case, "after-stop"))
    docker("rm", identity)
    require(inspect(name) is None, "container_removal_not_confirmed")
    return {"container_id": identity, "native_process_stopped": True,
            "host_pid_after_stop": 0, "execution_stop_confirmed": True,
            "container_removed": True, "private_home_and_workspace_retained": True,
            "native_logs": logs, "logs_complete": all(item.get("complete") is True for item in logs)}


def port_available(port):
    with socket.socket() as listener:
        # Closed HTTP connections may remain in TIME_WAIT after a confirmed
        # container stop. Reuse that closed tuple without sharing a live listener.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def select_endpoint(path, endpoint_id):
    private_directory(path.parent)
    config = json_value(read_file(path, 65536))
    require(isinstance(config, dict) and config.get("schema_version") == 1
            and isinstance(config.get("endpoints"), list), "invalid_endpoint_configuration")
    matches = [value for value in config["endpoints"]
               if isinstance(value, dict) and value.get("id") == endpoint_id]
    require(len(matches) == 1, "endpoint_identity_missing_or_duplicate")
    selected = matches[0]
    model, base_url = selected.get("model"), selected.get("base_url")
    require(isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", model)
            and isinstance(base_url, str) and len(base_url) <= 2048, "invalid_model_configuration")
    parsed = urllib.parse.urlsplit(base_url)
    require(parsed.scheme == "https" and parsed.hostname and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment,
            "invalid_endpoint_url")
    key_reference = selected.get("api_key_file")
    require(isinstance(key_reference, str) and key_reference, "api_key_file_required")
    key_file = no_links(path.parent / key_reference)
    private_directory(key_file.parent)
    key = read_file(key_file, 8192).decode().strip()
    require(key and not any(c.isspace() or ord(c) < 32 for c in key), "invalid_api_key_file")
    # Only the original file path is mounted; the value is never copied to a
    # launch argument, Docker ENV, build context, case report or local machine.
    del key
    return {"model": model, "base_url": base_url}, key_file


def create_private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def launch_arguments(name, attempt, image_id, case, key_file, port):
    mounts = [(case / "home", "/data/hermes", False),
              (case / "workspace", "/data/workspace", False),
              (case / "model.json", "/run/config/model.json", True),
              (key_file, "/run/secrets/model-api-key", True),
              (case / "api-token", "/run/secrets/api-token", True)]
    arguments = ["run", "--detach", "--pull", "never", "--name", name,
                 "--label", TASK_LABEL + "=" + attempt,
                 "--label", "io.harniqo.task=m0-native-qualification",
                 "--user", str(os.geteuid()) + ":" + str(os.getegid()),
                 "--init", "--read-only", "--cap-drop", "ALL",
                 "--security-opt", "no-new-privileges", "--pids-limit", "128",
                 "--memory", "4g", "--cpus", "2", "--log-driver", "local",
                 "--log-opt", "max-size=10m", "--log-opt", "max-file=2",
                 "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777",
                 "--publish", "127.0.0.1:" + str(port) + ":8642"]
    for source, target, readonly in mounts:
        require("," not in str(source) and "\n" not in str(source), "mount_path_rejected")
        arguments += ["--mount", "type=bind,src=" + str(source) + ",dst=" + target
                      + (",readonly" if readonly else "")]
    return [*arguments, image_id]


def run(args):
    root = no_links(args.root)
    require(sys.platform == "linux", "linux_host_required")
    require(shutil.disk_usage(root).free >= 10 * 1024**3, "insufficient_workspace_space")
    require(10000 <= args.port <= 10100 and args.port != 10023, "port_outside_authorized_range")
    require(re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.endpoint_id), "invalid_endpoint_id")
    provenance_path = no_links(args.provenance)
    provenance = json_value(read_file(provenance_path, 4 * 1024**2, private=False))
    image_id = provenance.get("image_id", "")
    require(IMAGE.fullmatch(image_id) and provenance.get("platform") == "linux/amd64"
            and provenance.get("inputs", {}).get("source_commit") == COMMIT,
            "unexpected_provenance_identity")
    profile_hash = digest(read_file(root / "deploy/qualification/hermes/entrypoint.py", 65536, private=False))
    require(provenance.get("inputs", {}).get("recipe_sha256", {}).get("entrypoint.py") == profile_hash,
            "qualification_profile_identity_mismatch")
    configuration, key_file = select_endpoint(no_links(args.config), args.endpoint_id)
    case = no_links(args.case)
    require(not case.exists(), "case_directory_already_exists")
    private_directory(case.parent, create=True)
    locks = private_directory(root / ".private/qualification/locks", create=True)
    lock_fd = os.open(locks / ("hermes-port-" + str(args.port)),
                      os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Rejected("qualification_port_in_use") from None
        require(port_available(args.port), "qualification_port_in_use")
        case.mkdir(mode=0o700)
        for path in (case / "home", case / "workspace"):
            path.mkdir(mode=0o700)
        token = secrets.token_urlsafe(48)
        create_private(case / "api-token", token.encode())
        create_private(case / "model.json", json.dumps(configuration, sort_keys=True).encode())
        attempt = secrets.token_hex(16)
        name = "harniqo-qualification-" + attempt
        report = {"schema_version": 1, "kind": "hermes_qualification_operator",
                  "attempt_id": attempt, "source_commit": COMMIT, "image_id": image_id,
                  "tool_profile_sha256": profile_hash, "endpoint_id": args.endpoint_id,
                  "expected_model": configuration["model"], "probe": args.probe,
                  "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "status": "running", "stage": "prepared", "milestone_complete": False,
                  "saas_isolation_passed": False, "native_case_passed": False,
                  "scope": "trusted_native_qualification_not_production_executor"}
        report_path = case / "operator.json"
        write_report(report_path, report)
        observed_id = None
        try:
            # Fixed local daemon; the probe receives the same explicit endpoint.
            os.environ.pop("DOCKER_CONTEXT", None)
            os.environ["DOCKER_HOST"] = DOCKER_HOST
            local = docker("info", "--format", '{{json .OSType}}')
            require(json_value(local.stdout) == "linux", "linux_docker_required")
            report["stage"] = "launch_requested"
            write_report(report_path, report)
            docker(*launch_arguments(name, attempt, image_id, case, key_file, args.port))
            value = inspect(name)
            observed_id = owned(value, attempt, image_id)
            require(value.get("running") is True and value.get("ports") == {
                "8642/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(args.port)}]},
                "runtime_launch_state_mismatch")
            report.update(stage="container_started", container_id=observed_id)
            write_report(report_path, report)
            api = API(args.port, token)
            deadline = time.monotonic() + 60
            while True:
                try:
                    api.request("/api/sessions?limit=1")
                    break
                except (Rejected, OSError):
                    require(time.monotonic() < deadline, "native_gateway_start_timeout")
                    current = inspect(observed_id)
                    owned(current, attempt, image_id, observed_id)
                    require(current.get("running") is True, "native_gateway_exited")
                    time.sleep(0.25)
            report.update(stage="native_gateway_ready")
            write_report(report_path, report)
            script = {"tools": "probe_hermes.py", "lifecycle": "probe_hermes_lifecycle.py",
                      "model-lock": "probe_hermes_model_lock.py"}[args.probe]
            require((root / "scripts/qualification" / script).is_file(), "probe_not_implemented")
            command = [sys.executable, str(root / "scripts/qualification" / script),
                       "--root", str(root), "--container", name, "--port", str(args.port),
                       "--provenance", str(provenance_path), "--token-file", str(case / "api-token"),
                       "--workspace", str(case / "workspace"), "--expected-model", configuration["model"],
                       "--report", str(case / "probe.json")]
            if args.probe == "model-lock":
                command += ["--model-config-file", str(case / "model.json")]
            elif args.probe == "lifecycle":
                command += ["--cases", *args.lifecycle_cases]
            with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, start_new_session=True) as process:
                try:
                    exit_code = process.wait(timeout=360)
                except BaseException:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
                    raise
            observed = json_value(read_file(case / "probe.json", 2 * 1024**2))
            passed = observed.get({"tools": "native_api_case_passed",
                                   "lifecycle": "lifecycle_cases_passed",
                                   "model-lock": "native_api_case_passed"}[args.probe]) is True
            report.update(probe_exit_code=exit_code, native_case_passed=exit_code == 0 and passed,
                          stage="probe_finished", status="passed" if exit_code == 0 and passed else "failed")
        except BaseException as error:
            report.update(status="failed", native_case_passed=False,
                          failure={"code": error.code if isinstance(error, Rejected) else type(error).__name__})
        finally:
            # A successful native Run is independent from its host stop/removal.
            # Conversely, process termination is not a native cancellation pass.
            try:
                report["cleanup"] = cleanup(name, attempt, image_id, observed_id, case)
                if observed_id is not None:
                    require(report["cleanup"].get("execution_stop_confirmed") is True,
                            "observed_execution_stop_not_confirmed")
                    require(report["cleanup"].get("logs_complete") is True,
                            "native_log_evidence_incomplete")
                report["cleanup"]["loopback_port_released"] = port_available(args.port)
                require(report["cleanup"]["loopback_port_released"], "qualification_port_not_released")
            except BaseException as error:
                report.update(status="failed", native_case_passed=False,
                              cleanup_failure={"code": error.code if isinstance(error, Rejected)
                                               else type(error).__name__})
            report["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            write_report(report_path, report)
        print(json.dumps({"status": report["status"], "native_case_passed": report["native_case_passed"],
                          "cleanup_confirmed": report.get("cleanup", {}).get("execution_stop_confirmed", False),
                          "report": str(report_path), "milestone_complete": False}))
        return 0 if report["status"] == "passed" else 1


def main():
    os.umask(0o077)
    def interrupted(signum, _frame):
        raise Rejected("operator_signal_" + signal.Signals(signum).name.lower())
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--endpoint-id", required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--port", type=int, default=10010)
    parser.add_argument("--probe", choices=("tools", "lifecycle", "model-lock"), default="tools")
    parser.add_argument("--lifecycle-cases", nargs="+", choices=("cancellation", "approval"),
                        default=["cancellation", "approval"])
    try:
        return run(parser.parse_args())
    except (Rejected, OSError, ValueError) as error:
        print(json.dumps({"status": "rejected", "code": error.code if isinstance(error, Rejected)
                          else type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
