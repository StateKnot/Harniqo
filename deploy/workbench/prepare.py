#!/usr/bin/env python3
"""Create private first-workbench deployment inputs without starting services."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
from uuid import uuid4


def write_private(directory, name, value):
    with (directory / name).open("x", encoding="utf-8") as output:
        os.chmod(output.name, 0o600)
        output.write(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--database-image", required=True)
    parser.add_argument("--database-port", type=int, default=10052)
    args = parser.parse_args()
    if not re.fullmatch(r"postgres@sha256:[0-9a-f]{64}", args.database_image):
        parser.error("database image must be an immutable official postgres reference")
    if not 10000 <= args.database_port <= 10100 or args.database_port == 10023:
        parser.error("database port must be in the approved application range")
    directory = args.directory
    if not directory.is_absolute() or directory.exists():
        parser.error("use a new absolute private directory")
    os.umask(0o077)
    directory.mkdir(parents=True, mode=0o700)
    if directory.resolve() != directory:
        parser.error("private directory must have an exact physical path")
    names = (
        "organizationId", "projectId", "principalId", "sessionId", "workspaceId",
        "deploymentId", "executionTargetId", "runtimeInstanceId", "harnessReleaseId",
        "platformEpoch", "deploymentRevisionId", "configurationRevisionId",
        "inputSnapshotId", "budgetPolicyId",
    )
    profile = {name: str(uuid4()) for name in names}
    profile["name"] = "Hermes"
    write_private(directory, "profile.json", json.dumps(profile, indent=2) + "\n")
    for label in ("admin", "migrator", "control", "worker"):
        password = secrets.token_hex(32)
        role = "harniqo_" + label
        write_private(directory, label + ".password", password + "\n")
        write_private(directory, label + ".database-url", (
            f"postgresql://{role}:{password}@127.0.0.1:{args.database_port}/harniqo\n"
        ))
    write_private(directory, "native-api-token", secrets.token_urlsafe(48) + "\n")
    write_private(directory, "database.env", (
        f"HARNIQO_WORKBENCH_POSTGRES_IMAGE={args.database_image}\n"
        f"HARNIQO_WORKBENCH_DATABASE_PORT={args.database_port}\n"
        f"HARNIQO_WORKBENCH_ADMIN_PASSWORD_FILE={directory / 'admin.password'}\n"
    ))
    for name in ("hermes-home", "workspace", "connector-state"):
        (directory / name).mkdir(mode=0o700)
    print(json.dumps({"prepared": True, "profile": "profile.json", "database_env": "database.env"}))


if __name__ == "__main__":
    main()
