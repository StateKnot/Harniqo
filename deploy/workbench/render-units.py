#!/usr/bin/env python3
"""Render private user units with explicit source and toolchain paths."""

import argparse
import os
from pathlib import Path


def unit_value(path):
    value = str(path)
    if not path.is_absolute() or any(character in value for character in "\r\n\x00"):
        raise ValueError("unit paths must be absolute single-line values")
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    if not args.node.is_file() or not (args.repository / "apps/control-plane/main-worker.ts").is_file():
        parser.error("source and pinned Node executable must exist")
    if not args.directory.is_dir() or args.directory.resolve() != args.directory:
        parser.error("private deployment directory must already exist at its physical path")
    os.umask(0o077)
    output = args.directory / "systemd"
    output.mkdir(mode=0o700)
    values = {"@REPOSITORY@": unit_value(args.repository), "@NODE@": unit_value(args.node), "@PRIVATE@": unit_value(args.directory)}
    for template in sorted((Path(__file__).parent / "systemd").glob("*.service")):
        text = template.read_text()
        for marker, value in values.items():
            text = text.replace(marker, value)
        with (output / template.name).open("x") as file:
            file.write(text)
        print(template.name)


if __name__ == "__main__":
    main()
