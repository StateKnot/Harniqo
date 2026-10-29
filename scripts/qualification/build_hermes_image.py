#!/usr/bin/env python3
"""Build the pinned, secret-free Hermes native-API qualification image.

Run on the authorized Linux amd64 Docker host. Public source/cache lives in
.state; the Docker archive and its provenance live in images/containers/hermes.
This is an interface qualification package, not a SaaS isolation boundary.
"""
from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import tomllib
import urllib.parse
import urllib.request
import zipfile

COMMIT = "a382af23f4c067d23463b5781958684c2cda82bc"
VERSION = "git-" + COMMIT[:12]
REQUESTED_RELEASE = "v0.22.5"
RELEASE_COMMIT = "bf0203a794a07aac3ff764972ab9087baa4e4f4e"
SOURCE_URL = "https://codeload.github.com/NousResearch/hermes-agent/tar.gz/" + COMMIT
SOURCE_SHA256 = "e4f29f1b0c6aecc9c8867166fc620835047bfc5d76d2c634b823ccc795238a1e"
PYTHON_TAG = "3.14-trixie"
SQLITE_URL = "https://sqlite.org/2026/sqlite-autoconf-3530400.tar.gz"
SQLITE_SHA256 = "0e9483900e92cd5de8fd48d16bf9200145a61f7fd5be542a5ac81d8a9516eb9c"
UV_WHEEL_FILENAME = "uv-0.13.5-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
UV_WHEEL = {
    "format": "wheel", "filename": UV_WHEEL_FILENAME,
    "url": "https://files.pythonhosted.org/packages/3a/ad/3c2a81d66d1839063662e5b6c0002ec31f8ee987dbc5295747627ca6160a/" + UV_WHEEL_FILENAME,
    "sha256": "fa8d26e08e34c29c0c4e820cf3181757086d547240b4f4d31ff1d8bd810ca946",
    "size": 17211825, "metadata_url": "https://pypi.org/pypi/uv/0.13.5/json",
}
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
PUBLIC_HOSTS = {"codeload.github.com", "github.com", "release-assets.githubusercontent.com",
                "objects.githubusercontent.com", "auth.docker.io", "registry-1.docker.io", "sqlite.org",
                "files.pythonhosted.org"}


class PublicRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or target.hostname not in PUBLIC_HOSTS or target.username or target.password:
            raise ValueError("Public download redirected outside the approved HTTPS sources")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_url(url, maximum, headers=None):
    target = urllib.parse.urlsplit(url)
    if target.scheme != "https" or target.hostname not in PUBLIC_HOSTS:
        raise ValueError("Unapproved public source")
    request = urllib.request.Request(url, headers={"User-Agent": "Harniqo-M0/1", **(headers or {})})
    started = time.monotonic()
    deadline = started + 300
    last_progress = started
    next_bytes = 8 * 1024**2
    data = bytearray()
    print(json.dumps({"download": "started", "source_host": target.hostname}), flush=True)
    # This is a cooperative total-duration limit, checked between small read1
    # calls. Socket inactivity is bounded separately. DNS/OS scheduling and an
    # in-flight blocking call can overrun the nominal deadline; callers needing
    # a process hard cutoff must still supply one outside this script.
    with urllib.request.build_opener(PublicRedirects()).open(request, timeout=15) as response:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Public download exceeded its cooperative total-duration limit")
            chunk = response.read1(min(64 * 1024, maximum + 1 - len(data)))
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError("Public download exceeded its cooperative total-duration limit")
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > maximum:
                raise ValueError("Public download exceeds its size bound")
            if len(data) >= next_bytes or now - last_progress >= 5:
                print(json.dumps({"download": "progress", "source_host": target.hostname,
                                  "downloaded_bytes": len(data), "elapsed_seconds": round(now - started, 1)}),
                      flush=True)
                next_bytes = len(data) + 8 * 1024**2
                last_progress = now
        print(json.dumps({"download": "finished", "source_host": target.hostname,
                          "downloaded_bytes": len(data), "elapsed_seconds": round(time.monotonic() - started, 1)}),
              flush=True)
        return bytes(data), response.headers


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024**2):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2, ensure_ascii=False)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def cached_download(url, path, expected, maximum):
    if path.is_symlink():
        raise ValueError("Cached artifact must not be a symlink")
    if path.exists():
        if not path.is_file() or sha(path) != expected:
            raise ValueError("Existing public cache does not match its pinned SHA-256")
        return
    data, _ = read_url(url, maximum)
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Pinned public artifact SHA-256 mismatch; do not change the pin to bypass this")
    temporary = path.with_name(path.name + ".partial-" + str(os.getpid()))
    try:
        with temporary.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_wheelhouse(source, provided, context):
    """Copy only wheels whose bytes match the unchanged upstream lock.

    The Docker recipe independently exports the frozen dependency closure and
    requires its hashes during offline sync. A missing or wrong-platform wheel
    therefore fails installation rather than silently changing the selection.
    """
    destination = context / "wheelhouse"
    destination.mkdir()
    if provided is None:
        return {"mode": "online-frozen-sync", "wheel_count": 0}
    provided = Path(os.path.abspath(provided))
    if any(part.is_symlink() for part in (provided, *provided.parents)) or not provided.is_dir():
        raise ValueError("Wheelhouse must be a real directory without symlink components")
    locked = tomllib.loads((source / "uv.lock").read_text())
    allowed = {}
    for package in locked["package"]:
        for wheel in package.get("wheels", []):
            name = urllib.parse.unquote(urllib.parse.urlsplit(wheel["url"]).path.rsplit("/", 1)[-1])
            expected = wheel["hash"]
            if (Path(name).name != name or not name.endswith(".whl")
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected)):
                raise ValueError("Unexpected wheel identity in the pinned source lock")
            value = {"sha256": expected.removeprefix("sha256:"), "bytes": wheel.get("size")}
            if name in allowed and allowed[name] != value:
                raise ValueError("Ambiguous wheel filename in the pinned source lock")
            allowed[name] = value
    candidates = sorted(provided.glob("*.whl"))
    if not 1 <= len(candidates) <= 1000:
        raise ValueError("Offline wheelhouse must contain a bounded nonempty wheel set")
    total, records = 0, []
    for path in candidates:
        metadata = path.lstat()
        expected = allowed.get(path.name)
        if (not stat.S_ISREG(metadata.st_mode) or expected is None
                or (expected["bytes"] is not None and metadata.st_size != expected["bytes"])
                or not 0 < metadata.st_size <= 200 * 1024**2):
            raise ValueError("Offline wheel is not a regular artifact from the pinned lock")
        total += metadata.st_size
        if total > 512 * 1024**2:
            raise ValueError("Offline wheelhouse exceeds the build input size bound")
        target = destination / path.name
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as incoming, target.open("xb") as outgoing:
            opened = os.fstat(incoming.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size != metadata.st_size:
                raise ValueError("Offline wheel changed before its bounded copy")
            copied = 0
            while copied <= metadata.st_size:
                chunk = incoming.read(min(4 * 1024**2, metadata.st_size + 1 - copied))
                if not chunk:
                    break
                outgoing.write(chunk)
                copied += len(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if copied != metadata.st_size or sha(target) != expected["sha256"]:
            raise ValueError("Offline wheel content differs from the pinned source hash")
        target.chmod(0o444)
        records.append({"filename": path.name, "sha256": expected["sha256"], "bytes": copied})
    return {"mode": "offline-frozen-export-pip-sync", "wheel_count": len(records),
            "wheel_bytes": total, "source_lock_sha256": sha(source / "uv.lock"), "wheels": records}


def extract_source(archive, destination):
    prefix = "hermes-agent-" + COMMIT
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if len(members) > 40000 or sum(m.size for m in members) > 1536 * 1024**2:
            raise ValueError("Source archive exceeds its extraction bound")
        seen = set()
        for member in members:
            relative = PurePosixPath(member.name)
            if (relative.is_absolute() or ".." in relative.parts or member.name in seen
                    or relative.parts[0] != prefix or not (member.isfile() or member.isdir())):
                raise ValueError("Source archive contains an unsafe entry")
            seen.add(member.name)
        # Every name is validated, no links/devices are accepted, destination is fresh.
        source.extractall(destination)
    return destination / prefix


def extract_uv_artifact(archive, destination, artifact_format):
    """Extract one pinned executable, never install or execute a wheel package."""
    if artifact_format == "tar.gz":
        with tarfile.open(archive) as packed:
            binary = packed.getmember("uv-x86_64-unknown-linux-gnu/uv")
            if not binary.isfile() or binary.size > 150 * 1024**2:
                raise ValueError("Unexpected uv binary artifact")
            with packed.extractfile(binary) as incoming, destination.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
    elif artifact_format == "wheel":
        with zipfile.ZipFile(archive) as packed:
            entries = packed.infolist()
            if len(entries) > 100 or sum(e.file_size for e in entries) > 300 * 1024**2:
                raise ValueError("uv wheel exceeds its extraction bound")
            names = set()
            for entry in entries:
                name = PurePosixPath(entry.filename)
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if (name.is_absolute() or ".." in name.parts or "\\" in entry.filename
                        or entry.filename in names or entry.flag_bits & 1
                        or kind not in {0, stat.S_IFREG, stat.S_IFDIR}):
                    raise ValueError("uv wheel contains an unsafe entry")
                names.add(entry.filename)
            metadata_name = "uv-0.13.5.dist-info/METADATA"
            wheel_name = "uv-0.13.5.dist-info/WHEEL"
            binary_name = "uv-0.13.5.data/scripts/uv"
            for name in (metadata_name, wheel_name):
                if packed.getinfo(name).file_size > 128 * 1024:
                    raise ValueError("uv wheel metadata is unexpectedly large")
            metadata = Parser().parsestr(packed.read(metadata_name).decode("utf-8"))
            wheel = Parser().parsestr(packed.read(wheel_name).decode("utf-8"))
            if (metadata.get_all("Name") != ["uv"] or metadata.get_all("Version") != ["0.13.5"]
                    or wheel.get("Root-Is-Purelib") != "false"
                    or "py3-none-manylinux_2_17_x86_64" not in wheel.get_all("Tag", [])):
                raise ValueError("uv wheel metadata does not match the reviewed version/platform")
            binary = packed.getinfo(binary_name)
            if binary.is_dir() or not 0 < binary.file_size <= 150 * 1024**2:
                raise ValueError("Unexpected executable in uv wheel")
            with packed.open(binary) as incoming, destination.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
    else:
        raise ValueError("Unsupported uv artifact format")
    destination.chmod(0o755)
    return sha(destination)


def resolve_python_base():
    token_url = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull"
    token = json.loads(read_url(token_url, 128 * 1024)[0])["token"]
    accept = ("application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,"
              "application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json")
    headers = {"Authorization": "Bearer " + token, "Accept": accept}

    def manifest(reference):
        body, received = read_url("https://registry-1.docker.io/v2/library/python/manifests/" + reference,
                                  4 * 1024**2, headers)
        actual = "sha256:" + hashlib.sha256(body).hexdigest()
        if received.get("Docker-Content-Digest") != actual:
            raise ValueError("Docker Official Python manifest digest mismatch")
        return json.loads(body), actual

    index, index_digest = manifest(PYTHON_TAG)
    matches = [m for m in index.get("manifests", [])
               if m.get("platform", {}).get("os") == "linux"
               and m.get("platform", {}).get("architecture") == "amd64"
               and not m.get("platform", {}).get("variant")]
    if len(matches) != 1 or not DIGEST.fullmatch(matches[0].get("digest", "")):
        raise ValueError("Python tag does not identify exactly one linux/amd64 manifest")
    leaf, digest = manifest(matches[0]["digest"])
    if digest != matches[0]["digest"] or not DIGEST.fullmatch(leaf.get("config", {}).get("digest", "")):
        raise ValueError("Python platform manifest does not match the index")
    return {"tag": "docker.io/library/python:" + PYTHON_TAG, "index_digest": index_digest,
            "platform_digest": digest, "reference": "docker.io/library/python@" + digest,
            "platform": "linux/amd64", "resolved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def bounded_output(value):
    if value is None:
        return {"tail": "", "bytes": 0, "truncated": False}
    raw = value if isinstance(value, bytes) else str(value).encode("utf-8", errors="replace")
    return {"tail": raw[-8192:].decode("utf-8", errors="replace"), "bytes": len(raw), "truncated": len(raw) > 8192}


class BuildAttempt:
    """Durable private evidence, independent of automatically removed contexts."""

    def __init__(self, root, recipe):
        parent = root
        for component in (".private", "qualification", "hermes-build"):
            parent = parent / component
            parent.mkdir(mode=0o700, exist_ok=True)
            metadata = parent.lstat()
            if (not stat.S_ISDIR(metadata.st_mode) or parent.is_symlink()
                    or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077):
                raise ValueError("Build evidence directory must be private and owned by this user")
        self.directory = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%dT%H%M%SZ-", time.gmtime()), dir=parent))
        self.path = self.directory / "attempt.json"
        self.value = {"schema_version": 1, "kind": "hermes_qualification_build_attempt", "status": "in_progress",
                      "stage": "created", "history": [], "image_id": None,
                      "inputs": {"source_url": SOURCE_URL, "source_commit": COMMIT,
                                 "source_archive_sha256": SOURCE_SHA256,
                                 "recipe_sha256": {n: sha(recipe / n) for n in ("Dockerfile", "entrypoint.py")}},
                      "qualification_passed": False, "saas_isolation_passed": False}
        self.update("created")

    def update(self, stage, **fields):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.value.update(fields)
        self.value.update({"stage": stage, "updated_at": now})
        self.value["history"].append({"stage": stage, "at": now})
        fd, name = tempfile.mkstemp(prefix=".attempt-", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(self.value, output, indent=2, ensure_ascii=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def failed(self, error, process=None):
        if self.value.get("status") == "failed":
            return
        evidence = process if process is not None else error
        failure = {"stage": self.value["stage"], "type": type(error).__name__,
                   "returncode": getattr(evidence, "returncode", None),
                   "stdout": bounded_output(getattr(evidence, "stdout", None)),
                   "stderr": bounded_output(getattr(evidence, "stderr", None))}
        # No raw exception/URL/header text: registry redirects may contain an
        # anonymous signed download token even though no user key is ever read.
        self.update(self.value["stage"], status="failed", failure=failure)


def command(argv, attempt=None, **kwargs):
    try:
        return subprocess.run(argv, check=True, stdin=subprocess.DEVNULL, **kwargs)
    except BaseException as error:
        if attempt is not None:
            attempt.failed(error)
        raise


def observed_image_identity(inspect):
    """Project bounded identity fields; never serialize image Config/Env."""
    if (not isinstance(inspect, dict) or not isinstance(inspect.get("Id"), str)
            or not DIGEST.fullmatch(inspect["Id"])):
        raise ValueError("Docker inspect returned an invalid image ID")
    descriptor = inspect.get("Descriptor")
    observed_descriptor = None
    if descriptor is not None:
        if (not isinstance(descriptor, dict)
                or not isinstance(descriptor.get("mediaType"), str)
                or len(descriptor["mediaType"]) > 200
                or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]{1,180}", descriptor["mediaType"])
                or not isinstance(descriptor.get("digest"), str)
                or not DIGEST.fullmatch(descriptor["digest"])
                or type(descriptor.get("size")) is not int or not 0 <= descriptor["size"] <= 64 * 1024**2):
            raise ValueError("Docker image target descriptor has invalid fields")
        platform_value = descriptor.get("platform")
        observed_platform = None
        if platform_value is not None:
            if not isinstance(platform_value, dict) or not {"os", "architecture"} <= set(platform_value):
                raise ValueError("Docker image descriptor platform is invalid")
            observed_platform = {}
            for key in ("os", "architecture", "variant", "os.version"):
                if key in platform_value:
                    value = platform_value[key]
                    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", value):
                        raise ValueError("Docker image descriptor platform field is invalid")
                    observed_platform[key] = value
            if "os.features" in platform_value:
                features = platform_value["os.features"]
                if (not isinstance(features, list) or len(features) > 16
                        or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", v) for v in features)):
                    raise ValueError("Docker image descriptor platform features are invalid")
                observed_platform["os.features"] = list(features)
        observed_descriptor = {"mediaType": descriptor["mediaType"], "digest": descriptor["digest"],
                               "size": descriptor["size"], "platform": observed_platform}
    repo_digests = inspect.get("RepoDigests")
    if repo_digests is not None:
        if (not isinstance(repo_digests, list) or len(repo_digests) > 64
                or any(not isinstance(v, str) or len(v) > 512
                       or not re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}", v) for v in repo_digests)):
            raise ValueError("Docker repository digest observations are invalid")
        repo_digests = list(repo_digests)
    return {"inspect_image_id": inspect["Id"], "image_descriptor": observed_descriptor,
            "repo_digests": repo_digests}


def docker_storage_free(path):
    """Read the actual containing mount, even when DockerRootDir is root-only."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Docker storage path must be absolute")
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4]))
        try:
            path.relative_to(mount)
        except ValueError:
            continue
        mounts.append(mount)
    if not mounts:
        raise ValueError("Cannot identify Docker storage's actual filesystem")
    return shutil.disk_usage(max(mounts, key=lambda p: len(p.parts))).free


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--prepare-only", action="store_true", help="verify/download inputs and resolve base; do not build")
    parser.add_argument("--wheelhouse", type=Path,
                        help="explicit offline dependency input; every wheel must match the original source lock")
    parser.add_argument("--uv-source", choices=("release-tar", "pypi-wheel"), default="release-tar",
                        help="explicit uv 0.13.5 artifact choice; no automatic format/source fallback")
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    recipe = root / "deploy/qualification/hermes"
    if not (root / "images/README.md").is_file() or not (recipe / "entrypoint.py").is_file():
        parser.error("--root must identify this Harniqo workspace")
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "AMD64"}:
        parser.error("This qualification target is native Linux x86_64; do not substitute emulation")
    attempt = BuildAttempt(root, recipe)
    print(json.dumps({"build_attempt": str(attempt.path.relative_to(root))}), flush=True)
    try:
        run_build(args, root, recipe, attempt)
    except BaseException as error:
        attempt.failed(error)
        raise


def run_build(args, root, recipe, attempt):
    attempt.update("preflight", uv_source=args.uv_source)
    # Retain 50 GiB plus a bounded allowance for source, layers and archive.
    if shutil.disk_usage(root).free < 60 * 1024**3:
        raise RuntimeError("Need at least 60 GiB free before preparing/building this image")
    if not args.prepare_only:
        if shutil.which("docker") is None:
            raise RuntimeError("Docker CLI/daemon must already be available")
        endpoint = os.environ.get("DOCKER_HOST") or json.loads(command(
            ["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"],
            capture_output=True, text=True, timeout=30, attempt=attempt).stdout)
        if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
            raise RuntimeError("Use the local authorized Docker daemon; remote contexts cannot verify its disk here")
        info = json.loads(command(["docker", "info", "--format", "{{json .}}"], capture_output=True, text=True,
                                  timeout=30, attempt=attempt).stdout)
        if info.get("OSType") != "linux" or info.get("Architecture") not in {"x86_64", "amd64"}:
            raise RuntimeError("The Docker daemon must be native Linux amd64")
        if docker_storage_free(info["DockerRootDir"]) < 60 * 1024**3:
            raise RuntimeError("Docker's actual storage filesystem also needs at least 60 GiB free")
    state = root / ".state/qualification/hermes"
    state.mkdir(parents=True, exist_ok=True)
    archive = state / (COMMIT + ".tar.gz")
    attempt.update("source_download")
    cached_download(SOURCE_URL, archive, SOURCE_SHA256, 512 * 1024**2)
    with tempfile.TemporaryDirectory(prefix="context-", dir=state) as temporary:
        attempt.update("source_extract")
        context = Path(temporary)
        source = extract_source(archive, context)
        source.rename(context / "source")
        source = context / "source"
        locks = {name: sha(source / name) for name in ("pyproject.toml", "uv.lock", "pm/lock.json")}
        attempt.update("wheelhouse_verify")
        dependencies = prepare_wheelhouse(source, args.wheelhouse, context)
        manager = json.loads((source / "pm/lock.json").read_text())["packages"]["uv"]
        artifact = manager["artifacts"]["linux-x64"]
        if manager["version"] != "0.13.5" or artifact["sha256"] != "f68d291c75783122dad58ab77def0e13f3233f358731db5d6d7e8715603b928f":
            raise ValueError("Pinned source no longer has the reviewed uv bootstrap")
        upstream_uv = {"format": "tar.gz", "version": manager["version"], **artifact}
        uv_input = dict(UV_WHEEL) if args.uv_source == "pypi-wheel" else dict(upstream_uv)
        uv_archive = state / (UV_WHEEL_FILENAME if args.uv_source == "pypi-wheel"
                              else "uv-" + manager["version"] + "-linux-x64.tar.gz")
        uv_bootstrap = {**uv_input, "version": manager["version"], "selection": args.uv_source,
                        "upstream_pin": upstream_uv}
        attempt.update("uv_download", locks_sha256=locks, uv_bootstrap=uv_bootstrap)
        cached_download(uv_input["url"], uv_archive, uv_input["sha256"], 64 * 1024**2)
        if args.uv_source == "pypi-wheel" and uv_archive.stat().st_size != UV_WHEEL["size"]:
            raise ValueError("Pinned uv wheel size mismatch")
        attempt.update("uv_extract")
        uv_bootstrap["extracted_binary_sha256"] = extract_uv_artifact(uv_archive, context / "uv", uv_input["format"])
        sqlite_archive = state / "sqlite-autoconf-3530400.tar.gz"
        attempt.update("sqlite_download")
        cached_download(SQLITE_URL, sqlite_archive, SQLITE_SHA256, 32 * 1024**2)
        shutil.copyfile(sqlite_archive, context / "sqlite.tar.gz")
        for name in ("Dockerfile", "entrypoint.py", "transport_audit.py"):
            shutil.copyfile(recipe / name, context / name)
        # Context is generated exclusively from reviewed public inputs, never root/.private.
        base_path = state / "python-base-resolved.json"
        attempt.update("python_base_resolve")
        if base_path.exists():
            base = json.loads(base_path.read_text())
            if (base.get("tag") != "docker.io/library/python:" + PYTHON_TAG or base.get("platform") != "linux/amd64"
                    or not DIGEST.fullmatch(base.get("platform_digest", ""))
                    or base.get("reference") != "docker.io/library/python@" + base["platform_digest"]):
                raise ValueError("Cached Python base resolution is invalid")
        else:
            base = resolve_python_base()
            write_json(base_path, base)
        inputs = {"source_url": SOURCE_URL, "source_commit": COMMIT, "source_version": VERSION,
                  "requested_release": REQUESTED_RELEASE, "requested_release_commit": RELEASE_COMMIT,
                  "release_matches_commit": False,
                  "source_archive_sha256": SOURCE_SHA256, "locks_sha256": locks, "python_base": base,
                  "uv_bootstrap": uv_bootstrap,
                  "dependency_installation": dependencies,
                  "sqlite": {"version": "3.53.4", "url": SQLITE_URL, "sha256": SQLITE_SHA256},
                  "recipe_sha256": {n: sha(context / n) for n in ("Dockerfile", "entrypoint.py", "transport_audit.py")},
                  "dependency_selection": "upstream uv.lock core + sms (aiohttp only); no default groups; frozen; wheels only"}
        attempt.update("inputs_prepared", inputs=inputs)
        if args.prepare_only:
            attempt.update("prepared", status="prepared")
            print(json.dumps({"prepared": True, "built": False, "base_digest": base["platform_digest"],
                              "source_archive_sha256": SOURCE_SHA256}))
            return
        iid_file = context / "image-id"
        attempt.update("building")
        try:
            command(["docker", "build", "--platform", "linux/amd64", "--build-arg", "PYTHON_BASE=" + base["reference"],
                     "--build-arg", "HARNIQO_DEPENDENCY_MODE=" + ("offline" if args.wheelhouse else "online"),
                     "--iidfile", str(iid_file), "--tag", "harniqo/hermes-qualification:" + VERSION,
                     str(context)], timeout=1800, attempt=attempt)
        finally:
            # Preserve an image ID even if a later command/build teardown fails.
            if iid_file.is_file() and iid_file.stat().st_size < 256:
                recorded_id = iid_file.read_text().strip()
                if DIGEST.fullmatch(recorded_id):
                    attempt.update(attempt.value["stage"], build_result_id=recorded_id)
        image_id = iid_file.read_text().strip()
        if not DIGEST.fullmatch(image_id):
            raise ValueError("Docker build did not return an immutable image ID")
        build_result_id = image_id
        attempt.update("image_inspect", build_result_id=build_result_id)
        inspect = json.loads(command(["docker", "image", "inspect", image_id], capture_output=True,
                                     text=True, timeout=30, attempt=attempt).stdout)[0]
        if inspect.get("Architecture") != "amd64" or inspect.get("Os") != "linux":
            raise ValueError("Built image platform mismatch")
        image_identity = observed_image_identity(inspect)
        image_id = image_identity["inspect_image_id"]
        attempt.update("runtime_inventory", image_id=image_id, image_identity=image_identity)
        inventory_result = command(
            ["docker", "run", "--rm", "--read-only", "--network", "none", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges", "--memory", "256m", "--pids-limit", "32",
             "--entrypoint", "/opt/hermes/.venv/bin/python", image_id, "-c",
             "from pathlib import Path; print(Path('/opt/hermes-runtime-inventory.json').read_text())"],
            capture_output=True, text=True, timeout=30, attempt=attempt)
        try:
            inventory = json.loads(inventory_result.stdout)
        except ValueError as error:
            attempt.failed(error, process=inventory_result)
            raise
        attempt.update("archive_prepare", runtime_inventory=inventory)
        output_dir = root / "images/containers/hermes" / VERSION
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / (image_id.replace(":", "-") + ".docker.tar")
        provenance = output.with_suffix(output.suffix + ".provenance.json")
        if output.exists() or provenance.exists():
            raise FileExistsError("Image archive/provenance already exists; refuse to overwrite evidence")
        partial = output.with_suffix(output.suffix + ".partial")
        attempt.update("archive_save", archive=str(output.relative_to(root)))
        try:
            with partial.open("xb") as stream:
                command(["docker", "image", "save", image_id], stdout=stream, stderr=subprocess.PIPE,
                        text=True, timeout=600, attempt=attempt)
                stream.flush()
                os.fsync(stream.fileno())
            archive_sha = sha(partial)
            os.link(partial, output)
            output.chmod(0o444)
        finally:
            partial.unlink(missing_ok=True)
        attempt.update("archive_provenance", archive_sha256=archive_sha)
        write_json(provenance, {"schema_version": 1, "kind": "container_archive", "component": "hermes",
                   "version": VERSION, "platform": "linux/amd64", "format": "docker-archive",
                   "relative_path": str(output.relative_to(root / "images")), "sha256": archive_sha,
                   "image_id": image_id, "image_digest": image_id, "digest_kind": "docker-image-store-id",
                   "build_result_id": build_result_id,
                   "image_config_digest": None, "registry_manifest_digest": None, **image_identity,
                   "inputs": inputs, "runtime_inventory": inventory,
                   "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "qualification_passed": False, "saas_isolation_passed": False})
        attempt.update("completed", status="completed", provenance=str(provenance.relative_to(root)))
        print(json.dumps({"built": True, "image_id": image_id, "archive": str(output.relative_to(root)),
                          "archive_sha256": archive_sha, "qualification_passed": False}))


if __name__ == "__main__":
    main()
