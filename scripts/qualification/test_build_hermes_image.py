#!/usr/bin/env python3
"""Wheelhouse admission component tests using synthetic locks and real files.

Run with Python 3.11+ on POSIX:
  PYTHONDONTWRITEBYTECODE=1 python3 scripts/qualification/test_build_hermes_image.py -v

No network, Docker, model, package installation or native Harness is exercised.
"""
from __future__ import annotations

import errno
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import build_hermes_image as builder


class PrepareWheelhouseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="harniqo-wheelhouse-", dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.provided = self.root / "provided"
        self.source.mkdir()
        self.provided.mkdir()
        self.name = "fixture_alpha-1.0.0-py3-none-any.whl"
        self.payload = self.wheel_bytes("fixture_alpha")
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.write_lock([(self.name, self.payload, True)])
        # Any accidental extension of these file-only tests into I/O with an
        # external service or command must fail before performing that action.
        for attribute in ("read_url", "command"):
            guard = patch.object(builder, attribute, side_effect=AssertionError("External I/O is outside this test scope"))
            guard.start()
            self.addCleanup(guard.stop)

    @staticmethod
    def wheel_bytes(name):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as wheel:
            wheel.writestr(name + "/__init__.py", '"""Synthetic qualification test fixture."""\n')
            wheel.writestr(name + "-1.0.0.dist-info/METADATA",
                           "Metadata-Version: 2.1\nName: " + name + "\nVersion: 1.0.0\n")
            wheel.writestr(name + "-1.0.0.dist-info/WHEEL",
                           "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            wheel.writestr(name + "-1.0.0.dist-info/RECORD", "")
        return archive.getvalue()

    def write_lock(self, wheels):
        lines = ['version = 1', '[[package]]', 'name = "fixture-alpha"', 'version = "1.0.0"', 'wheels = [']
        for filename, payload, include_size in wheels:
            fields = ["url = " + json.dumps("https://files.pythonhosted.org/fixture/" + filename),
                      "hash = " + json.dumps("sha256:" + hashlib.sha256(payload).hexdigest())]
            if include_size:
                fields.append("size = " + str(len(payload)))
            lines.append("  { " + ", ".join(fields) + " },")
        lines.append("]")
        (self.source / "uv.lock").write_text("\n".join(lines) + "\n")

    def context(self, name="context"):
        path = self.root / name
        path.mkdir()
        return path

    def prepare(self, context_name="context"):
        return builder.prepare_wheelhouse(self.source, self.provided, self.context(context_name))

    def test_known_wheels_copy_exact_bytes_readonly_and_leave_lock_unchanged(self):
        other_name = "fixture_beta-1.0.0-py3-none-any.whl"
        other = self.wheel_bytes("fixture_beta")
        self.write_lock([(self.name, self.payload, True), (other_name, other, True)])
        original_lock = (self.source / "uv.lock").read_bytes()
        expected = {self.name: self.payload, other_name: other}
        for filename, payload in expected.items():
            (self.provided / filename).write_bytes(payload)
        (self.provided / "notes.txt").write_text("Do not copy non-wheel inputs")
        context = self.context()
        result = builder.prepare_wheelhouse(self.source, self.provided, context)
        destination = context / "wheelhouse"
        self.assertEqual(set(path.name for path in destination.iterdir()), set(expected))
        for filename, payload in expected.items():
            target = destination / filename
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o444)
            self.assertEqual((self.provided / filename).read_bytes(), payload)
        self.assertEqual((self.source / "uv.lock").read_bytes(), original_lock)
        self.assertEqual(result["mode"], "offline-frozen-export-pip-sync")
        self.assertEqual(result["wheel_count"], 2)
        self.assertEqual(result["wheel_bytes"], len(self.payload) + len(other))
        self.assertEqual(result["source_lock_sha256"], hashlib.sha256(original_lock).hexdigest())
        records = {record["filename"]: record for record in result["wheels"]}
        self.assertEqual(set(records), set(expected))
        for filename, payload in expected.items():
            self.assertEqual(records[filename]["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(records[filename]["bytes"], len(payload))

    def test_same_size_content_change_is_rejected(self):
        corrupted = bytes([self.payload[0] ^ 1]) + self.payload[1:]
        self.assertEqual(len(corrupted), len(self.payload))
        (self.provided / self.name).write_bytes(corrupted)
        with self.assertRaises(ValueError):
            self.prepare()

    def test_known_size_mismatch_is_rejected(self):
        for label, payload in [("short", self.payload[:-1]), ("long", self.payload + b"x")]:
            with self.subTest(size=label):
                (self.provided / self.name).write_bytes(payload)
                with self.assertRaises(ValueError):
                    self.prepare(label)

    def test_unknown_wheel_name_is_rejected_even_with_known_bytes(self):
        (self.provided / "unknown-1.0.0-py3-none-any.whl").write_bytes(self.payload)
        with self.assertRaises(ValueError):
            self.prepare()

    def test_empty_offline_directory_is_rejected(self):
        with self.assertRaises(ValueError):
            self.prepare()

    def test_wheel_file_symlink_is_rejected(self):
        outside = self.root / "outside.whl"
        outside.write_bytes(self.payload)
        (self.provided / self.name).symlink_to(outside)
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(outside.read_bytes(), self.payload)

    def test_wheelhouse_directory_symlink_is_rejected(self):
        (self.provided / self.name).write_bytes(self.payload)
        alias = self.root / "alias"
        alias.symlink_to(self.provided, target_is_directory=True)
        with self.assertRaises(ValueError):
            builder.prepare_wheelhouse(self.source, alias, self.context())

    def test_wheelhouse_symlink_parent_is_rejected(self):
        real_parent = self.root / "real-parent"
        actual = real_parent / "wheels"
        actual.mkdir(parents=True)
        (actual / self.name).write_bytes(self.payload)
        alias = self.root / "alias-parent"
        alias.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            builder.prepare_wheelhouse(self.source, alias / "wheels", self.context())

    def test_directory_named_as_wheel_is_rejected(self):
        (self.provided / self.name).mkdir()
        with self.assertRaises(ValueError):
            self.prepare()

    def test_online_mode_creates_empty_input_without_requiring_a_lock(self):
        (self.source / "uv.lock").unlink()
        context = self.context()
        result = builder.prepare_wheelhouse(self.source, None, context)
        self.assertEqual(result, {"mode": "online-frozen-sync", "wheel_count": 0})
        self.assertTrue((context / "wheelhouse").is_dir())
        self.assertEqual(list((context / "wheelhouse").iterdir()), [])

    def test_unselected_platform_wheel_without_size_does_not_block_target(self):
        other_name = "fixture_alpha-1.0.0-py3-none-win_amd64.whl"
        self.write_lock([(self.name, self.payload, True), (other_name, b"unselected platform", False)])
        (self.provided / self.name).write_bytes(self.payload)
        result = self.prepare()
        self.assertEqual(result["wheel_count"], 1)
        self.assertEqual(result["wheels"][0]["filename"], self.name)

    def test_selected_wheel_without_size_records_actual_copied_bytes(self):
        self.write_lock([(self.name, self.payload, False)])
        (self.provided / self.name).write_bytes(self.payload)
        result = self.prepare()
        self.assertEqual(result["wheel_bytes"], len(self.payload))
        self.assertEqual(result["wheels"], [{"filename": self.name, "sha256": self.digest, "bytes": len(self.payload)}])

    def test_selected_wheel_without_size_still_rejects_hash_mismatch(self):
        self.write_lock([(self.name, self.payload, False)])
        (self.provided / self.name).write_bytes(bytes([self.payload[0] ^ 1]) + self.payload[1:])
        with self.assertRaises(ValueError):
            self.prepare()

    def test_symlink_swap_between_stat_and_open_is_rejected(self):
        candidate = self.provided / self.name
        candidate.write_bytes(self.payload)
        outside = self.root / "outside.whl"
        # Matching bytes make this specifically a no-follow check, rather than
        # a hash mismatch that would hide a path-following regression.
        outside.write_bytes(self.payload)
        real_open = os.open
        swapped = False

        def swap_before_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if Path(path) == candidate and not swapped:
                candidate.unlink()
                candidate.symlink_to(outside)
                swapped = True
            return real_open(path, flags, *args, **kwargs)

        with patch.object(builder.os, "open", side_effect=swap_before_open):
            with self.assertRaises((OSError, ValueError)) as raised:
                self.prepare()
        if isinstance(raised.exception, OSError):
            self.assertEqual(raised.exception.errno, errno.ELOOP)
        self.assertTrue(swapped)
        self.assertEqual(outside.read_bytes(), self.payload)


if __name__ == "__main__":
    unittest.main()
