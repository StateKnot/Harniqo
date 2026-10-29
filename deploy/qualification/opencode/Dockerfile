# Public inputs are verified by prepare_opencode.py before this offline build.
ARG PYTHON_BASE=python@sha256:faf7953db05ee8e060aa9fda8a389e5779cdba26a22e1363379861023e910830
FROM ${PYTHON_BASE}
LABEL org.opencontainers.image.source="https://github.com/anomalyco/opencode" \
      org.opencontainers.image.revision="810c0eba1a7348036f8bdf61e6632d0c36c6876c" \
      org.opencontainers.image.version="1.19.5-harniqo-qualification" \
      io.harniqo.scope="trusted-native-interface-qualification"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/data/opencode XDG_DATA_HOME=/data/opencode/data \
    XDG_STATE_HOME=/data/opencode/state XDG_CACHE_HOME=/data/opencode/cache \
    XDG_CONFIG_HOME=/opt/harniqo/config \
    OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_MODELS_FETCH=1 \
    OPENCODE_DISABLE_DEFAULT_PLUGINS=true OPENCODE_DISABLE_EXTERNAL_SKILLS=true \
    OPENCODE_DISABLE_LSP_DOWNLOAD=true OPENCODE_DISABLE_CLAUDE_CODE=true \
    OPENCODE_DISABLE_PROJECT_CONFIG=1 OPENCODE_DISABLE_FFF=1 \
    OPENCODE_DISABLE_EMBEDDED_WEB_UI=true \
    OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=1024
COPY --chmod=0555 opencode rg /usr/local/bin/
COPY --chmod=0444 upstream/ /opt/harniqo/upstream/
COPY --chmod=0444 inputs.json /opt/harniqo/inputs.json
COPY --chmod=0444 entrypoint.py /opt/harniqo/entrypoint.py
# A real immutable config directory makes native Npm.install return before
# any reify attempt. No fabricated node_modules or package lock is needed.
RUN mkdir -p /opt/harniqo/config/opencode /data/opencode /data/workspace && \
    printf '%s\n' 'node_modules' 'package.json' 'package-lock.json' 'bun.lock' '.gitignore' > /opt/harniqo/config/opencode/.gitignore && \
    chmod -R a-w /opt/harniqo/config && \
    chmod 0555 /opt/harniqo /opt/harniqo/upstream && \
    chown 10001:10001 /data/opencode /data/workspace && \
    chmod 0700 /data/opencode /data/workspace
# --network=none is enforced by the builder. This invokes only version/info
# commands, with no server, model, plugin, tool or port listener.
RUN python3 - <<'PY'
import json, subprocess, sys
from pathlib import Path
version = subprocess.check_output(['opencode', '--version'], text=True, timeout=30).strip()
assert version == '1.19.5', version
rg = subprocess.check_output(['rg', '--version'], text=True, timeout=10).strip()
assert rg.splitlines()[0].split()[:2] == ['ripgrep', '15.1.0'], rg
linked = subprocess.run(['ldd', '/usr/local/bin/opencode'], capture_output=True, text=True, timeout=10)
assert linked.returncode == 0 and 'not found' not in linked.stdout, linked.stdout
Path('/opt/harniqo/runtime-inventory.json').write_text(json.dumps({
    'python': sys.version, 'opencode': version, 'ripgrep': rg,
    'opencode_dynamic_libraries': linked.stdout.splitlines(),
    'scope': 'offline-build-version-and-linkage-only', 'qualification_passed': False,
}, sort_keys=True, indent=2) + '\n')
PY
USER 10001:10001
RUN python3 - <<'PY'
import os
from pathlib import Path
for name in ('entrypoint.py', 'inputs.json', 'upstream/LICENSE', 'runtime-inventory.json'):
    assert (Path('/opt/harniqo') / name).read_bytes()
assert not os.access('/opt/harniqo/config/opencode', os.W_OK)
assert os.access('/data/opencode', os.W_OK)
assert os.access('/data/workspace', os.W_OK)
PY
WORKDIR /data/workspace
EXPOSE 4096
ENTRYPOINT ["python3", "/opt/harniqo/entrypoint.py"]
