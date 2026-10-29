ARG PYTHON_BASE
FROM ${PYTHON_BASE}
LABEL io.harniqo.qualification="octop" io.harniqo.scope="trusted-native-interface-qualification"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/data/home
COPY requirements.lock /opt/qualification/requirements.lock
COPY wheels /opt/qualification/wheels
RUN python -m venv /opt/runtime && /opt/runtime/bin/python -m pip install --no-cache-dir --no-index --find-links=/opt/qualification/wheels --require-hashes -r /opt/qualification/requirements.lock && /opt/runtime/bin/python -m pip check && rm -rf /opt/qualification/wheels
ENV PATH="/opt/runtime/bin:${PATH}"
USER 65532:65532
WORKDIR /data
CMD ["python", "-c", "import importlib.metadata as m; print(m.version('octop-harness'))"]
