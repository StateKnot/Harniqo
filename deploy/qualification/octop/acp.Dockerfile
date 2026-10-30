ARG OPENCODE_IMAGE
ARG HARNESS_IMAGE
FROM ${OPENCODE_IMAGE} AS opencode_source
FROM ${HARNESS_IMAGE}
USER 0:0
LABEL io.harniqo.qualification="octop-acp" \
      io.harniqo.scope="trusted-octop-harness-acp-delegated" \
      io.harniqo.outer.revision="76cd38d7b8ffc66c28ba24ad60bbab206a95d3ac" \
      io.harniqo.inner.revision="810c0eba1a7348036f8bdf61e6632d0c36c6876c"
COPY --from=opencode_source /usr/local/bin/opencode /usr/local/bin/opencode
COPY --from=opencode_source /usr/local/bin/rg /usr/local/bin/rg
COPY --from=opencode_source /opt/harniqo/config /opt/harniqo/config
COPY --from=opencode_source /opt/harniqo/upstream/LICENSE /opt/acp-qualification/opencode-LICENSE
COPY --from=opencode_source /opt/harniqo/inputs.json /opt/acp-qualification/opencode-inputs.json
COPY wheels /opt/acp-qualification/wheels
COPY requirements.lock /opt/acp-qualification/requirements.lock
RUN /opt/runtime/bin/python -m pip install --no-cache-dir --no-index --find-links=/opt/acp-qualification/wheels --require-hashes -r /opt/acp-qualification/requirements.lock && \
    /opt/runtime/bin/python -m pip check && rm -rf /opt/acp-qualification/wheels
RUN /opt/runtime/bin/python - <<'PY'
import hashlib, importlib.metadata, json, subprocess
from pathlib import Path
import acp, psutil
assert importlib.metadata.version('octop-harness') == '1.0.5'
assert importlib.metadata.version('agent-client-protocol') == '0.10.1'
assert subprocess.check_output(['opencode','--version'],text=True).strip() == '1.19.5'
result = subprocess.run(['ldd','/usr/local/bin/opencode'],capture_output=True,text=True)
assert result.returncode == 0 and 'not found' not in result.stdout
Path('/opt/acp-qualification/inventory.json').write_text(json.dumps({
 'harness':'1.0.5','acp_python':'0.10.1','psutil':psutil.__version__,
 'opencode':'1.19.5','opencode_sha256':hashlib.sha256(Path('/usr/local/bin/opencode').read_bytes()).hexdigest(),
 'protocol_version':acp.PROTOCOL_VERSION,'native_qualified':False},indent=2)+'\n')
PY
USER 65532:65532
WORKDIR /data
CMD ["python", "-c", "print('Octop Harness ACP qualification image; no model task started')"]
