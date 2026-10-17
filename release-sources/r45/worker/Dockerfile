ARG CORE_BASE_IMAGE
FROM ${CORE_BASE_IMAGE}
USER root
COPY --chown=0:0 rootfs/ /
RUN node /opt/personal-cloud/verify-runtime020.mjs
LABEL com.megmeet.dsh.runtime="0.2.0-rc.2"
USER 1000:1000
