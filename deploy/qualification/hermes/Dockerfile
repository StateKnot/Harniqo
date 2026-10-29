# The builder resolves the Docker Official Python tag to its linux/amd64 digest
# before this file is invoked. No credentials are build arguments or context files.
ARG PYTHON_BASE
FROM ${PYTHON_BASE} AS sqlite_build
# The full official Python base contains the compiler toolchain. Reuse the
# fixed source and compile options from the reviewed Hermes Dockerfile; no apt.
COPY sqlite.tar.gz /tmp/sqlite.tar.gz
RUN printf '%s  %s\n' '0e9483900e92cd5de8fd48d16bf9200145a61f7fd5be542a5ac81d8a9516eb9c' /tmp/sqlite.tar.gz | sha256sum -c - && \
    tar -xzf /tmp/sqlite.tar.gz -C /tmp && cd /tmp/sqlite-autoconf-3530400 && \
    CFLAGS='-O2 -DSQLITE_ENABLE_FTS3 -DSQLITE_ENABLE_FTS3_PARENTHESIS -DSQLITE_ENABLE_FTS4 -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_RTREE -DSQLITE_ENABLE_GEOPOLY -DSQLITE_ENABLE_COLUMN_METADATA -DSQLITE_ENABLE_UNLOCK_NOTIFY -DSQLITE_ENABLE_DBSTAT_VTAB -DSQLITE_ENABLE_DBPAGE_VTAB -DSQLITE_ENABLE_MATH_FUNCTIONS -DSQLITE_ENABLE_PREUPDATE_HOOK -DSQLITE_ENABLE_SESSION -DSQLITE_SECURE_DELETE -DSQLITE_THREADSAFE=1 -DSQLITE_MAX_VARIABLE_NUMBER=250000' \
      ./configure --prefix=/opt/sqlite-fixed --disable-static && make -j2 && make install
FROM ${PYTHON_BASE}
LABEL org.opencontainers.image.source="https://github.com/NousResearch/hermes-agent" \
      org.opencontainers.image.revision="a382af23f4c067d23463b5781958684c2cda82bc" \
      org.opencontainers.image.version="git-a382af23f4c0-harniqo-qualification" \
      io.harniqo.scope="trusted-native-interface-qualification"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never UV_NO_MANAGED_PYTHON=1 UV_CACHE_DIR=/tmp/uv-cache UV_LINK_MODE=copy
COPY uv /usr/local/bin/uv
COPY --from=sqlite_build /opt/sqlite-fixed/lib/libsqlite3.so.3.53.4 /usr/local/lib/
RUN ln -sf libsqlite3.so.3.53.4 /usr/local/lib/libsqlite3.so.0 && \
    ln -sf libsqlite3.so.3.53.4 /usr/local/lib/libsqlite3.so && ldconfig && \
    python3 -c "import sqlite3; assert sqlite3.sqlite_version_info == (3,53,4); c=sqlite3.connect(':memory:'); c.execute(\"CREATE VIRTUAL TABLE docs USING fts5(content, tokenize='trigram')\"); c.execute(\"INSERT INTO docs VALUES ('hermes')\"); assert c.execute(\"SELECT count(*) FROM docs WHERE docs MATCH 'erm'\").fetchone()[0] == 1"
COPY source/ /opt/hermes/
WORKDIR /opt/hermes
ARG HARNIQO_DEPENDENCY_MODE=online
# sms is the upstream minimal extra containing only aiohttp, required by the
# native API adapter. This does not enable SMS or install messaging/desktop extras.
# No lock refresh, resolver fallback, sdist build, dev group or runtime installer.
# This task-scoped BuildKit mount survives failed syncs but is not an image layer.
# Offline installs the full frozen export, including upstream version overrides.
RUN --mount=type=cache,id=harniqo-hermes-a382af23f4c067d23463b5781958684c2cda82bc-linuxamd64-uv0.13.5,target=/tmp/uv-cache,sharing=locked \
    --mount=type=bind,source=wheelhouse,target=/opt/harniqo-wheelhouse,readonly \
    python3 -c 'import sys; assert sys.version_info[:2] == (3, 14)' && \
    case "$HARNIQO_DEPENDENCY_MODE" in \
      online) \
        uv sync --frozen --all-packages --python /usr/local/bin/python3 \
          --no-default-groups --no-install-project --no-install-package hermes-agent \
          --no-editable --no-build --extra sms ;; \
      offline) \
        uv export --frozen --offline --all-packages --no-default-groups --extra sms \
          --no-emit-project --no-emit-package hermes-agent --format requirements-txt \
          --output-file /tmp/harniqo-frozen-requirements.txt && \
        uv venv --python /usr/local/bin/python3 .venv && \
        UV_CONCURRENT_DOWNLOADS=4 UV_CONCURRENT_INSTALLS=4 \
        uv pip sync --offline --require-hashes --no-index \
          --find-links=/opt/harniqo-wheelhouse --only-binary=:all: \
          --python=.venv/bin/python /tmp/harniqo-frozen-requirements.txt && \
        rm /tmp/harniqo-frozen-requirements.txt ;; \
      *) printf '%s\n' 'HARNIQO_DEPENDENCY_MODE must be online or offline' >&2; exit 2 ;; \
    esac && \
    .venv/bin/python scripts/write_install_stamp.py --output install-stamp.json \
      --commit a382af23f4c067d23463b5781958684c2cda82bc --source commit-build \
      --display-version git.a382af23f4c0.harniqo-qualification --dirty \
      --distribution docker --update-mechanism external && \
    .venv/bin/python -c 'import aiohttp, openai, ruamel.yaml; import gateway.platforms.api_server' && \
    .venv/bin/python -c 'import importlib.metadata as m,json,sqlite3,sys; from pathlib import Path; Path("/opt/hermes-runtime-inventory.json").write_text(json.dumps({"python":sys.version,"sqlite":sqlite3.sqlite_version,"distributions":sorted([{"name":d.metadata["Name"],"version":d.version} for d in m.distributions()],key=lambda d:d["name"].lower())},sort_keys=True,indent=2)+"\n")' && \
    mkdir -p /data/hermes /data/workspace /opt/harniqo && \
    chown -R 10001:10001 /data && chmod 0700 /data/hermes /data/workspace && chmod -R a-w /opt/hermes && \
    rm /usr/local/bin/uv
COPY --chmod=0444 entrypoint.py /opt/harniqo/entrypoint.py
# Each native managed worker discovers this reviewed builtin through the
# upstream registry; parent-process registration would not survive worker exec.
COPY --chmod=0444 entrypoint.py /opt/hermes/tools/harniqo_qualification.py
ENV PATH=/opt/hermes/.venv/bin:$PATH PYTHONPATH=/opt/hermes \
    HOME=/data/hermes HERMES_HOME=/data/hermes \
    HERMES_WRITE_SAFE_ROOT=/data/workspace HERMES_SAFE_MODE=1 \
    HERMES_DISABLE_LAZY_INSTALLS=1
USER 10001:10001
WORKDIR /data/workspace
EXPOSE 8642
ENTRYPOINT ["/opt/hermes/.venv/bin/python", "/opt/harniqo/entrypoint.py"]
