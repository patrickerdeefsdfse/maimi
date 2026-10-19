# The maintenance controller must supply its reviewed immutable Linux/amd64 base.
# No mutable image tag or company configuration is inferred by this file.
ARG CORE_BASE_IMAGE
FROM ${CORE_BASE_IMAGE}
USER root
RUN rm -rf /opt/dsh /opt/personal-cloud
ADD runtime-native021.tar.gz /
COPY verify-runtime.mjs /opt/megmeet-verify-native021.mjs
ARG RUNTIME_MANIFEST_SHA256
RUN test -n "$RUNTIME_MANIFEST_SHA256" && node /opt/megmeet-verify-native021.mjs / "$RUNTIME_MANIFEST_SHA256"
LABEL com.megmeet.dsh.runtime="0.2.1-rc.2"
USER 1000:1000
WORKDIR /workspace
ENTRYPOINT ["node", "/opt/personal-cloud/worker.mjs"]
