import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import prepare_openclaw as target


class OfficialOciBoundary(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)/'input.tar'
        self.addCleanup(self.directory.cleanup)
        self.blob = b'fixed compressed layer fixture'
        self.config = b'{"architecture":"amd64","os":"linux"}'
        def descriptor(data):
            return {'digest': 'sha256:'+hashlib.sha256(data).hexdigest(), 'size': len(data)}
        self.layer = descriptor(self.blob)
        self.configuration = descriptor(self.config)
        self.manifest = json.dumps({'schemaVersion': 2, 'config': self.configuration, 'layers': [self.layer]}).encode()
        self.digest = descriptor(self.manifest)
        self.entries = {
            'oci-layout': b'{"imageLayoutVersion":"1.0.0"}',
            'index.json': json.dumps({'manifests': [{**self.digest, 'annotations': {
                'org.opencontainers.image.ref.name': target.OCI_TAG}}]}).encode(),
            'blobs/sha256/'+self.digest['digest'][7:]: self.manifest,
            'blobs/sha256/'+self.configuration['digest'][7:]: self.config,
            'blobs/sha256/'+self.layer['digest'][7:]: self.blob,
        }
        self.addCleanup(patch.stopall)
        patch.object(target, 'MANIFEST', self.digest['digest']).start()
        patch.object(target, 'CONFIG', self.configuration['digest']).start()

    def archive(self, entries=None, extra=None):
        with tarfile.open(self.path, 'w', format=tarfile.USTAR_FORMAT) as out:
            for name, data in (entries or self.entries).items():
                info = tarfile.TarInfo(name); info.size = len(data)
                out.addfile(info, io.BytesIO(data))
            if extra:
                out.addfile(extra)

    def test_complete_pinned_archive_and_full_archive_hash(self):
        self.archive()
        value = target.verify_oci(self.path)
        self.assertEqual(value['verified_blobs'], 2)
        self.assertEqual(value['sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_same_size_blob_tampering_is_rejected(self):
        changed = dict(self.entries)
        changed['blobs/sha256/'+self.layer['digest'][7:]] = b'x'*len(self.blob)
        self.archive(changed)
        with self.assertRaisesRegex(ValueError, 'blob size or hash'):
            target.verify_oci(self.path)

    def test_symlink_and_parent_path_never_extract(self):
        for name, kind in [('outside', tarfile.SYMTYPE), ('../outside', tarfile.REGTYPE)]:
            with self.subTest(name=name):
                extra = tarfile.TarInfo(name); extra.type = kind; extra.linkname = '/etc/passwd'
                self.archive(extra=extra)
                with self.assertRaisesRegex(ValueError, 'physical member'):
                    target.verify_oci(self.path)
        self.assertFalse((self.path.parent/'outside').exists())

    def test_pax_expansion_is_rejected_before_payload_read(self):
        info = tarfile.TarInfo('pax'); info.type = tarfile.XHDTYPE; info.size = 1024**3
        self.path.write_bytes(info.tobuf(format=tarfile.USTAR_FORMAT))
        with self.assertRaisesRegex(ValueError, 'physical member'):
            target.verify_oci(self.path)

    def test_truncated_member_cannot_verify(self):
        self.archive()
        self.path.write_bytes(self.path.read_bytes()[:530])
        with self.assertRaises(ValueError):
            target.verify_oci(self.path)


if __name__ == '__main__':
    unittest.main()
