#!/usr/bin/env python3
"""Fetch an authenticated Ubuntu cloud image into this workspace's images/.

Only Canonical HTTPS URLs, the installed Ubuntu cloud-image keyring and pinned
signer fingerprints are accepted. No global keyring or system setting is changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.request

SIGNER = "D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81"
ORIGIN = "https://cloud-images.ubuntu.com"
MAX_IMAGE = 8 * 1024**3


def fetch(url: str, target: Path, maximum: int) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Harniqo-M0/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        if not response.url.startswith(ORIGIN + "/"):
            raise ValueError("Image download redirected outside the approved origin")
        total = 0
        next_progress = 64 * 1024**2
        with target.open("xb") as output:
            while chunk := response.read(1024**2):
                total += len(chunk)
                if total > maximum:
                    raise ValueError("Download exceeded its size limit")
                output.write(chunk)
                if total >= next_progress:
                    print(json.dumps({"downloaded_mib": total // 1024**2}), flush=True)
                    next_progress += 64 * 1024**2
            output.flush()
            os.fsync(output.fileno())


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024**2):
            result.update(chunk)
    return result.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--series", choices=["noble"], default="noble")
    parser.add_argument("--keyring", type=Path, default=Path("/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg"))
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    if not (root / "images/README.md").is_file():
        parser.error("--root must be a Harniqo workspace with images/README.md")
    if not args.keyring.is_file():
        parser.error("A trusted Ubuntu cloud-image keyring is required")
    free = shutil.disk_usage(root).free
    if free < 50 * 1024**3 + MAX_IMAGE:
        raise RuntimeError("Insufficient free space; retain at least 50 GiB plus the download allowance")
    destination = root / "images/vm/base/ubuntu" / args.series / "amd64"
    destination.mkdir(parents=True, exist_ok=True)
    staging_parent = root / ".state/qualification/image-downloads"
    staging_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ubuntu-", dir=staging_parent) as temporary:
        staging = Path(temporary)
        base_url = f"{ORIGIN}/{args.series}/current"
        sums, signature = staging / "SHA256SUMS", staging / "SHA256SUMS.gpg"
        fetch(base_url + "/SHA256SUMS", sums, 2 * 1024**2)
        fetch(base_url + "/SHA256SUMS.gpg", signature, 1024**2)
        verified = subprocess.run(
            ["gpgv", "--status-fd", "1", "--keyring", str(args.keyring.resolve()), str(signature), str(sums)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        if not any(line.startswith("[GNUPG:] VALIDSIG " + SIGNER + " ") for line in verified.stdout.splitlines()):
            raise RuntimeError("Checksum signature does not match the approved Canonical signer")
        filename = f"{args.series}-server-cloudimg-amd64.img"
        matches = []
        for line in sums.read_text().splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) == 2 and parts[1].lstrip(" *./") == filename:
                matches.append(parts[0])
        if len(matches) != 1 or len(matches[0]) != 64:
            raise RuntimeError("Could not identify exactly one signed cloud-image checksum")
        expected = matches[0]
        target = destination / f"{expected}.qcow2"
        if target.exists():
            if not target.is_file() or target.is_symlink() or digest(target) != expected:
                raise RuntimeError("Existing image is not the authenticated expected file")
        else:
            partial = staging / filename
            fetch(base_url + "/" + filename, partial, MAX_IMAGE)
            if digest(partial) != expected:
                raise RuntimeError("Cloud image hash mismatch; the current publication may have changed, retry safely")
            info = json.loads(subprocess.check_output(["qemu-img", "info", "--output=json", str(partial)], text=True, timeout=30))
            if info["format"] != "qcow2" or info.get("backing-filename"):
                raise RuntimeError("Expected a standalone qcow2 image without a backing file")
            partial.chmod(0o444)
            os.replace(partial, target)
        provenance = {
            "schema_version": 1, "kind": "vm_base", "component": "ubuntu-cloud-image",
            "series": args.series, "platform": "linux/amd64", "format": "qcow2",
            "source": base_url + "/" + filename, "sha256": expected,
            "relative_path": str(target.relative_to(root / "images")),
            "signer_fingerprint": SIGNER, "bytes": target.stat().st_size,
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (destination / f"{expected}.provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
        shutil.copy2(sums, destination / f"{expected}.SHA256SUMS")
        shutil.copy2(signature, destination / f"{expected}.SHA256SUMS.gpg")
        manifest = root / "images/manifest.local.json"
        entries = json.loads(manifest.read_text()) if manifest.exists() else {"schema_version": 1, "images": []}
        entries["images"] = [x for x in entries["images"] if x.get("relative_path") != provenance["relative_path"]] + [provenance]
        tmp = manifest.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=2) + "\n")
        tmp.chmod(0o600)
        os.replace(tmp, manifest)
        print(json.dumps({"verified": True, **provenance}), flush=True)


if __name__ == "__main__":
    main()
