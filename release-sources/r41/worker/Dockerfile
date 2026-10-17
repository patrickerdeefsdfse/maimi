# Exact verified company Linux/amd64 worker config ID supplied by controller.
ARG CORE_BASE_IMAGE
FROM ${CORE_BASE_IMAGE}
USER root
# Image paths only. Replace the complete runtime and reviewed enterprise code.
RUN rm -rf /opt/dsh /opt/personal-cloud
ADD runtime-linux-x64-020-rc2.tar.gz /
RUN node /opt/personal-cloud/verify-runtime020.mjs
LABEL com.megmeet.dsh.runtime="0.2.0-rc.2"
USER 1000:1000
