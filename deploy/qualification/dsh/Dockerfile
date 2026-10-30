FROM harniqo-dsh-input-node:eba17fa04b4ac5616496c2ce374a95afb6db6ff52d8bf73bf87dc8c7b8656cad
LABEL io.harniqo.qualification.dsh.source="f3803c96974ad4a94195fb97e5c637e9c2a3afe3"
COPY --chown=1000:1000 node_modules /opt/dsh/node_modules
COPY package.json package-lock.json /opt/dsh/
ENV HOME=/data/home DSH_HOME=/data/home DSH_TELEMETRY_MODE=OFF DSH_PRIMARY_RUNTIME="" NODE_OPTIONS=""
HEALTHCHECK NONE
USER 1000:1000
WORKDIR /data/workspace
ENTRYPOINT ["/usr/local/bin/node", "/opt/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js"]
CMD ["--profile", "sdk"]
