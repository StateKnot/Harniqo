"""Record only public CI runtime facts before installing validation dependencies."""
import hashlib
import json
from pathlib import Path
import platform
import shutil
import sys

root = Path(__file__).resolve().parents[2]
free = shutil.disk_usage(root).free
report = {
    'scope': 'schema_openapi_fixture_validation_only',
    'python': platform.python_version(),
    'system': platform.system(),
    'machine': platform.machine(),
    'free_bytes_before_install': free,
    'requirements_sha256': hashlib.sha256((root/'scripts/contracts/requirements.lock').read_bytes()).hexdigest(),
    'desktop_support_verified': False,
    'native_harness_verified': False,
}
folder = root/'ci-evidence'
folder.mkdir(exist_ok=True)
(folder/'environment.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
print(json.dumps(report))
if sys.version_info[:2] != (3, 12) or free < 5*1024**3:
    raise SystemExit('CI needs Python 3.12 and 5 GiB free for this bounded validation job')
