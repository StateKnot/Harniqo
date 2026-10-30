ARG ORIGINAL_IMAGE
FROM ${ORIGINAL_IMAGE}
USER 0:0
LABEL io.harniqo.qualification="octop-acp" \
      io.harniqo.scope="trusted-octop-harness-acp-delegated-patched" \
      io.harniqo.patch.sha256="80b00725949cd106ba4b76639ded4614dca1347829a5aedb3c40a2e38296a147"
COPY apply_octop_acp_patch.py acp-final-text.patch acp-patch-profile.json acp-upstream-LICENSE /opt/acp-qualification/patch/
RUN /opt/runtime/bin/python - <<'PY'
import hashlib, importlib, json, sys
from pathlib import Path
sys.path.insert(0,'/opt/acp-qualification/patch')
from apply_octop_acp_patch import patched_source
import octop_harness.builtin.tools.acp_runner as module
root=Path('/opt/acp-qualification/patch'); source=Path(module.__file__)
for artifact in root.iterdir():artifact.chmod(0o644)
profile=json.loads((root/'acp-patch-profile.json').read_text())
source.write_bytes(patched_source(source.read_bytes(),(root/'acp-final-text.patch').read_bytes(),profile))
# Remove the one modified module's bytecode before loading it again.
for cached in source.parent.glob('__pycache__/acp_runner.*.pyc'):cached.unlink()
importlib.invalidate_caches();importlib.reload(module)
assert hashlib.sha256(source.read_bytes()).hexdigest()==profile['patched_sha256']
assert hashlib.sha256((root/'acp-upstream-LICENSE').read_bytes()).hexdigest()==profile['upstream_license_sha256']
(root/'loaded-identity.json').write_text(json.dumps({'scope':profile['scope'],'source_sha256':profile['patched_sha256'],'patch_sha256':profile['patch_sha256'],'native_qualified':False},indent=2)+'\n')
PY
USER 65532:65532
