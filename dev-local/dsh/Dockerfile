FROM node@sha256:b21fe589dfbe5cc39365d0544b9be3f1f33f55f3c86c87a76ff65a02f8f5848e
WORKDIR /opt/dsh
COPY dev-local/dsh/package.json dev-local/dsh/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY dev-local/dsh /opt/megmeet
RUN node /opt/megmeet/brand-overlay-r2.mjs \
    && node /opt/megmeet/prepare-runtime.mjs \
    && mkdir -p /state/work /state/dsh /state/home /state/cache \
    && chown -R node:node /state
ENV PATH=/opt/dsh/node_modules/.bin:/usr/local/bin:/usr/bin:/bin \
    HOME=/state/home DSH_HOME=/state/dsh DSH_TELEMETRY_DISABLED=1 \
    XDG_CACHE_HOME=/state/cache NODE_OPTIONS=--max-old-space-size=768
USER node
WORKDIR /state/work
CMD ["node", "/opt/megmeet/launcher.mjs"]
