# Offline maintenance over the trusted company runtime.
ARG BASE_IMAGE=docker.io/library/maimi-web-sales-batch20@sha256:76e5f07fb381e1b8523ba13817965988bf87348fd6470b6d30c198fba4ba7f01
ARG WORD_OFFICE_IMAGE=docker.io/library/maimi-office-api@sha256:60c4458f816c964ed1fd9b761bb7c27cdfc4a6dcfd2a2e126ed3d607acb2be69
ARG CHAT_RUNTIME_IMAGE=docker.io/library/maimi-task-files@sha256:a984952c5865ce16152fb45f52a7f8cef7b278eefc84b4abf3dca6ab512faeb3
ARG EXECUTION_RUNTIME_IMAGE=docker.io/library/maimi-task-files@sha256:5167751957c12efad81386d5fe068838c860bd9b3b576ff4bdd4a0a0ee2ca734
FROM ${WORD_OFFICE_IMAGE} AS word-libraries
FROM ${BASE_IMAGE} AS company-ui
USER 0
COPY dsh/plugins/company-ui/client-assets.json dsh/plugins/company-ui/client.template.js dsh/plugins/company-ui/render_client.py /tmp/order-ui/
COPY patch-native-chat.py /tmp/order-ui/
RUN python3 -IB /tmp/order-ui/render_client.py --installed-client /opt/dsh/plugins/company-ui/client.js --verify-shipped --output /tmp/company-client.js
RUN python3 -IB /tmp/order-ui/patch-native-chat.py --input /opt/dsh/node_modules/@deepseek-ai/dsh-client-ui-chat/lib/client.js --output /tmp/native-chat.js

FROM ${BASE_IMAGE} AS source-build
USER 0
COPY --chown=0:0 personal-cloud/ /opt/personal-cloud/
COPY --chown=0:0 dsh/ /opt/dsh/
COPY --from=company-ui --chown=0:0 /tmp/company-client.js /opt/dsh/plugins/company-ui/client.js
COPY --from=company-ui --chown=0:0 /tmp/native-chat.js /opt/dsh/node_modules/@deepseek-ai/dsh-client-ui-chat/lib/client.js
RUN rm -rf /opt/dsh/node_modules/dsh-any-background /opt/dsh/plugins/managed/r73/background /opt/dsh/plugins/managed/r73/themes
RUN --mount=type=bind,source=harden-runtime.py,target=/tmp/harden-runtime.py,readonly python3 -IB /tmp/harden-runtime.py
RUN --mount=type=bind,from=word-libraries,source=/opt/office/venv/lib/python3.13/site-packages,target=/opt/approved-word-python,readonly \
    --mount=type=bind,source=install-word-libraries.py,target=/tmp/install-word-libraries.py,readonly \
    python3 -IB /tmp/install-word-libraries.py --group word --group spreadsheet-image && python3 -IB -c 'import docx, lxml.etree, typing_extensions, openpyxl, defusedxml, PIL.Image'
USER 1000:1000

FROM ${EXECUTION_RUNTIME_IMAGE} AS execution
COPY --chown=0:0 ["dsh/community.patch.json", "dsh/enterprise-native.mjs", "dsh/office-response-client.mjs", "dsh/package-lock.json", "dsh/package.json", "dsh/workspace-pinned-overlay.mjs", "/opt/dsh/"]
COPY --chown=0:0 ["dsh/company-ppt-template/index.mjs", "/opt/dsh/company-ppt-template/"]
COPY --chown=0:0 ["dsh/company-ppt-template/skills/company-ppt-template/SKILL.md", "dsh/company-ppt-template/skills/company-ppt-template/template-manifest.json", "/opt/dsh/company-ppt-template/skills/company-ppt-template/"]
COPY --chown=0:0 ["dsh/company-ppt-template/skills/company-ppt-template/references/template-guide.md", "/opt/dsh/company-ppt-template/skills/company-ppt-template/references/"]
COPY --chown=0:0 ["dsh/company-ppt-template/skills/company-ppt-template/scripts/company_ppt.py", "dsh/company-ppt-template/skills/company-ppt-template/scripts/preview_pdf.py", "/opt/dsh/company-ppt-template/skills/company-ppt-template/scripts/"]
COPY --chown=0:0 ["dsh/office-adapter/adapter.mjs", "dsh/office-adapter/index.mjs", "dsh/office-adapter/preview-converter.mjs", "dsh/office-adapter/probe-native-host.mjs", "dsh/office-adapter/publish-output.py", "dsh/office-adapter/transport.mjs", "/opt/dsh/office-adapter/"]
COPY --chown=0:0 ["dsh/plugins/company-ui/client-assets.json", "dsh/plugins/company-ui/index.mjs", "dsh/plugins/company-ui/package.json", "/opt/dsh/plugins/company-ui/"]
COPY --chown=0:0 ["dsh/plugins/managed/README.md", "dsh/plugins/managed/build-managed.mjs", "dsh/plugins/managed/index.mjs", "dsh/plugins/managed/package.json", "dsh/plugins/managed/policy.mjs", "dsh/plugins/managed/profile.mjs", "dsh/plugins/managed/source-lock.json", "/opt/dsh/plugins/managed/"]
COPY --chown=0:0 ["dsh/plugins/managed/appearance/index.mjs", "/opt/dsh/plugins/managed/appearance/"]
COPY --chown=0:0 ["dsh/plugins/managed/browser/index.mjs", "/opt/dsh/plugins/managed/browser/"]
COPY --chown=0:0 ["dsh/plugins/managed/context/index.mjs", "/opt/dsh/plugins/managed/context/"]
COPY --chown=0:0 ["dsh/plugins/managed/dependencies/package-lock.json", "dsh/plugins/managed/dependencies/package.json", "/opt/dsh/plugins/managed/dependencies/"]
COPY --chown=0:0 ["dsh/plugins/managed/files/index.mjs", "/opt/dsh/plugins/managed/files/"]
COPY --chown=0:0 ["dsh/plugins/managed/genui/index.mjs", "/opt/dsh/plugins/managed/genui/"]
COPY --chown=0:0 ["dsh/plugins/managed/r73/package.json", "dsh/plugins/managed/r73/policy.mjs", "/opt/dsh/plugins/managed/r73/"]
COPY --chown=0:0 ["dsh/plugins/managed/r73/appearance/package.json", "/opt/dsh/plugins/managed/r73/appearance/"]
COPY --chown=0:0 ["dsh/plugins/managed/r73/browser/package.json", "/opt/dsh/plugins/managed/r73/browser/"]
COPY --chown=0:0 ["dsh/plugins/managed/r73/context/package.json", "/opt/dsh/plugins/managed/r73/context/"]
COPY --chown=0:0 ["dsh/plugins/managed/r73/files/package.json", "/opt/dsh/plugins/managed/r73/files/"]
COPY --chown=0:0 ["dsh/plugins/managed/r73/genui/package.json", "/opt/dsh/plugins/managed/r73/genui/"]
COPY --chown=0:0 ["dsh/plugins/managed/r73/sidebar/package.json", "/opt/dsh/plugins/managed/r73/sidebar/"]
COPY --chown=0:0 ["dsh/plugins/managed/sidebar/index.mjs", "/opt/dsh/plugins/managed/sidebar/"]
COPY --chown=0:0 ["dsh/review-skills/index.mjs", "dsh/review-skills/package.json", "/opt/dsh/review-skills/"]
COPY --chown=0:0 ["dsh/review-skills/skills/onboarding-health-review/SKILL.md", "/opt/dsh/review-skills/skills/onboarding-health-review/"]
COPY --chown=0:0 ["dsh/review-skills/skills/onboarding-health-review/references/official-sources.json", "dsh/review-skills/skills/onboarding-health-review/references/review-contract.md", "/opt/dsh/review-skills/skills/onboarding-health-review/references/"]
COPY --chown=0:0 ["dsh/review-skills/skills/patent-review/SKILL.md", "/opt/dsh/review-skills/skills/patent-review/"]
COPY --chown=0:0 ["dsh/review-skills/skills/patent-review/references/report-contract.md", "dsh/review-skills/skills/patent-review/references/review-workflows.md", "dsh/review-skills/skills/patent-review/references/rule-catalog.json", "/opt/dsh/review-skills/skills/patent-review/references/"]
COPY --chown=0:0 ["dsh/toolkit/connector-failure.mjs", "dsh/toolkit/drawing-extraction.mjs", "dsh/toolkit/enterprise-task-skills.mjs", "dsh/toolkit/http.mjs", "dsh/toolkit/project-failure.mjs", "dsh/toolkit/project-routing.mjs", "execution-overrides/dsh/toolkit/project-bridge.mjs", "execution-overrides/dsh/toolkit/session-tools.mjs", "/opt/dsh/toolkit/"]
COPY --chown=0:0 ["dsh/toolkit/adapter/project-tool-request.mjs", "execution-overrides/dsh/toolkit/adapter/index.mjs", "/opt/dsh/toolkit/adapter/"]
COPY --chown=0:0 ["personal-cloud/attachment-bridge.mjs", "personal-cloud/attachment_cache_retention.py", "personal-cloud/cache_empty_shell.py", "personal-cloud/card-store-client.mjs", "personal-cloud/card_retention_server.py", "personal-cloud/card_store.py", "personal-cloud/community-profile.mjs", "personal-cloud/company-system-prompt.mjs", "personal-cloud/company-workspace-controller.mjs", "personal-cloud/distributed-skills.mjs", "personal-cloud/enterprise-model-policy.mjs", "personal-cloud/enterprise-profile.mjs", "personal-cloud/execution-agent.mjs", "personal-cloud/execution-relay.mjs", "personal-cloud/execution-rpc-core.mjs", "personal-cloud/history-admission-rc2.mjs", "personal-cloud/history-migration.mjs", "personal-cloud/history-native.mjs", "personal-cloud/history-runtime-snapshot.mjs", "personal-cloud/history-server.mjs", "personal-cloud/idle-control.mjs", "personal-cloud/image-description.mjs", "personal-cloud/live-task-root.mjs", "personal-cloud/managed-outputs-plugin.mjs", "personal-cloud/native-attachment-plugin.mjs", "personal-cloud/native-cancel-fence.mjs", "personal-cloud/native-model-scope.mjs", "personal-cloud/native-scoped-credentials.mjs", "personal-cloud/native-search-connection.mjs", "personal-cloud/native-search-install.mjs", "personal-cloud/native-task-context.mjs", "personal-cloud/office-bridge.mjs", "personal-cloud/office-kit-bridge.mjs", "personal-cloud/office-kit-executor.mjs", "personal-cloud/official-search-plugin.mjs", "personal-cloud/official-search-provider.mjs", "personal-cloud/outputs-bridge.mjs", "personal-cloud/plugin-static-policy.mjs", "personal-cloud/project-file-transfer-gate.mjs", "personal-cloud/project-files-plugin.mjs", "personal-cloud/provider-readiness.mjs", "personal-cloud/remote-execution-client.mjs", "personal-cloud/remote-execution-plugin.mjs", "personal-cloud/search-host.mjs", "personal-cloud/search-permits.mjs", "personal-cloud/source_retention_contract.py", "personal-cloud/source_retention_server.py", "personal-cloud/source_retention_store.py", "personal-cloud/task-authority.mjs", "personal-cloud/task-control-plugin.mjs", "personal-cloud/task-files.mjs", "personal-cloud/task-tickets.mjs", "personal-cloud/transfer-scope.mjs", "personal-cloud/vision-control-plugin.mjs", "personal-cloud/web-bridge.mjs", "personal-cloud/web-control-plugin.mjs", "personal-cloud/worker-identity.mjs", "personal-cloud/worker.mjs", "personal-cloud/write-shared-cache.mjs", "/opt/personal-cloud/"]
COPY --chown=0:0 ["personal-cloud/workspace-client/client.js", "personal-cloud/workspace-client/index.mjs", "personal-cloud/workspace-client/package.json", "/opt/personal-cloud/workspace-client/"]

FROM ${CHAT_RUNTIME_IMAGE} AS chat
COPY --chown=0:0 ["dsh/community.patch.json", "dsh/enterprise-native.mjs", "dsh/office-response-client.mjs", "dsh/package-lock.json", "dsh/package.json", "dsh/workspace-pinned-overlay.mjs", "/opt/dsh/"]
COPY --chown=0:0 ["dsh/company-ppt-template/index.mjs", "/opt/dsh/company-ppt-template/"]
COPY --chown=0:0 ["dsh/company-ppt-template/skills/company-ppt-template/SKILL.md", "dsh/company-ppt-template/skills/company-ppt-template/template-manifest.json", "/opt/dsh/company-ppt-template/skills/company-ppt-template/"]
COPY --chown=0:0 ["dsh/company-ppt-template/skills/company-ppt-template/references/template-guide.md", "/opt/dsh/company-ppt-template/skills/company-ppt-template/references/"]
COPY --chown=0:0 ["dsh/company-ppt-template/skills/company-ppt-template/scripts/company_ppt.py", "dsh/company-ppt-template/skills/company-ppt-template/scripts/preview_pdf.py", "/opt/dsh/company-ppt-template/skills/company-ppt-template/scripts/"]
COPY --chown=0:0 ["dsh/office-adapter/adapter.mjs", "dsh/office-adapter/index.mjs", "dsh/office-adapter/preview-converter.mjs", "dsh/office-adapter/probe-native-host.mjs", "dsh/office-adapter/publish-output.py", "dsh/office-adapter/transport.mjs", "/opt/dsh/office-adapter/"]
COPY --chown=0:0 ["dsh/plugins/company-ui/client-assets.json", "dsh/plugins/company-ui/index.mjs", "dsh/plugins/company-ui/package.json", "/opt/dsh/plugins/company-ui/"]
COPY --chown=0:0 ["dsh/plugins/managed/README.md", "dsh/plugins/managed/build-managed.mjs", "dsh/plugins/managed/index.mjs", "dsh/plugins/managed/package.json", "dsh/plugins/managed/policy.mjs", "dsh/plugins/managed/profile.mjs", "dsh/plugins/managed/source-lock.json", "/opt/dsh/plugins/managed/"]
COPY --chown=0:0 ["dsh/plugins/managed/appearance/index.mjs", "/opt/dsh/plugins/managed/appearance/"]
COPY --chown=0:0 ["dsh/plugins/managed/browser/index.mjs", "/opt/dsh/plugins/managed/browser/"]
COPY --chown=0:0 ["dsh/plugins/managed/context/index.mjs", "/opt/dsh/plugins/managed/context/"]
COPY --chown=0:0 ["dsh/plugins/managed/dependencies/package-lock.json", "dsh/plugins/managed/dependencies/package.json", "/opt/dsh/plugins/managed/dependencies/"]
COPY --chown=0:0 ["dsh/plugins/managed/files/index.mjs", "/opt/dsh/plugins/managed/files/"]
COPY --chown=0:0 ["dsh/plugins/managed/genui/index.mjs", "/opt/dsh/plugins/managed/genui/"]
COPY --chown=0:0 ["dsh/plugins/managed/r73/package.json", "dsh/plugins/managed/r73/policy.mjs", "/opt/dsh/plugins/managed/r73/"]
COPY --chown=0:0 ["dsh/plugins/managed/r73/appearance/package.json", "/opt/dsh/plugins/managed/r73/appearance/"]
COPY --chown=0:0 ["dsh/plugins/managed/r73/browser/package.json", "/opt/dsh/plugins/managed/r73/browser/"]
COPY --chown=0:0 ["dsh/plugins/managed/r73/context/package.json", "/opt/dsh/plugins/managed/r73/context/"]
COPY --chown=0:0 ["dsh/plugins/managed/r73/files/package.json", "/opt/dsh/plugins/managed/r73/files/"]
COPY --chown=0:0 ["dsh/plugins/managed/r73/genui/package.json", "/opt/dsh/plugins/managed/r73/genui/"]
COPY --chown=0:0 ["dsh/plugins/managed/r73/sidebar/package.json", "/opt/dsh/plugins/managed/r73/sidebar/"]
COPY --chown=0:0 ["dsh/plugins/managed/sidebar/index.mjs", "/opt/dsh/plugins/managed/sidebar/"]
COPY --chown=0:0 ["dsh/review-skills/index.mjs", "dsh/review-skills/package.json", "/opt/dsh/review-skills/"]
COPY --chown=0:0 ["dsh/review-skills/skills/onboarding-health-review/SKILL.md", "/opt/dsh/review-skills/skills/onboarding-health-review/"]
COPY --chown=0:0 ["dsh/review-skills/skills/onboarding-health-review/references/official-sources.json", "dsh/review-skills/skills/onboarding-health-review/references/review-contract.md", "/opt/dsh/review-skills/skills/onboarding-health-review/references/"]
COPY --chown=0:0 ["dsh/review-skills/skills/patent-review/SKILL.md", "/opt/dsh/review-skills/skills/patent-review/"]
COPY --chown=0:0 ["dsh/review-skills/skills/patent-review/references/report-contract.md", "dsh/review-skills/skills/patent-review/references/review-workflows.md", "dsh/review-skills/skills/patent-review/references/rule-catalog.json", "/opt/dsh/review-skills/skills/patent-review/references/"]
COPY --chown=0:0 ["dsh/toolkit/badge-task.mjs", "dsh/toolkit/connector-failure.mjs", "dsh/toolkit/drawing-extraction.mjs", "dsh/toolkit/enterprise-task-skills.mjs", "dsh/toolkit/http.mjs", "dsh/toolkit/project-bridge.mjs", "dsh/toolkit/project-failure.mjs", "dsh/toolkit/project-routing.mjs", "dsh/toolkit/session-tools.mjs", "/opt/dsh/toolkit/"]
COPY --chown=0:0 ["dsh/toolkit/adapter/index.mjs", "dsh/toolkit/adapter/project-tool-request.mjs", "/opt/dsh/toolkit/adapter/"]
COPY --chown=0:0 ["personal-cloud/attachment-bridge.mjs", "personal-cloud/attachment_cache_retention.py", "personal-cloud/cache_empty_shell.py", "personal-cloud/card-store-client.mjs", "personal-cloud/card_retention_server.py", "personal-cloud/card_store.py", "personal-cloud/community-profile.mjs", "personal-cloud/company-system-prompt.mjs", "personal-cloud/company-workspace-controller.mjs", "personal-cloud/distributed-skills.mjs", "personal-cloud/enterprise-model-policy.mjs", "personal-cloud/enterprise-profile.mjs", "personal-cloud/execution-agent.mjs", "personal-cloud/execution-relay.mjs", "personal-cloud/execution-rpc-core.mjs", "personal-cloud/history-admission-rc2.mjs", "personal-cloud/history-migration.mjs", "personal-cloud/history-native.mjs", "personal-cloud/history-runtime-snapshot.mjs", "personal-cloud/history-server.mjs", "personal-cloud/idle-control.mjs", "personal-cloud/image-description.mjs", "personal-cloud/live-task-root.mjs", "personal-cloud/managed-outputs-plugin.mjs", "personal-cloud/native-attachment-plugin.mjs", "personal-cloud/native-cancel-fence.mjs", "personal-cloud/native-model-scope.mjs", "personal-cloud/native-scoped-credentials.mjs", "personal-cloud/native-search-connection.mjs", "personal-cloud/native-search-install.mjs", "personal-cloud/native-task-context.mjs", "personal-cloud/office-bridge.mjs", "personal-cloud/office-kit-bridge.mjs", "personal-cloud/office-kit-executor.mjs", "personal-cloud/official-search-plugin.mjs", "personal-cloud/official-search-provider.mjs", "personal-cloud/outputs-bridge.mjs", "personal-cloud/plugin-static-policy.mjs", "personal-cloud/project-file-transfer-gate.mjs", "personal-cloud/project-files-plugin.mjs", "personal-cloud/provider-readiness.mjs", "personal-cloud/remote-execution-client.mjs", "personal-cloud/remote-execution-plugin.mjs", "personal-cloud/search-host.mjs", "personal-cloud/search-permits.mjs", "personal-cloud/source_retention_contract.py", "personal-cloud/source_retention_server.py", "personal-cloud/source_retention_store.py", "personal-cloud/task-authority.mjs", "personal-cloud/task-control-plugin.mjs", "personal-cloud/task-files.mjs", "personal-cloud/task-tickets.mjs", "personal-cloud/transfer-scope.mjs", "personal-cloud/vision-control-plugin.mjs", "personal-cloud/web-bridge.mjs", "personal-cloud/web-control-plugin.mjs", "personal-cloud/worker-identity.mjs", "personal-cloud/worker.mjs", "personal-cloud/write-shared-cache.mjs", "/opt/personal-cloud/"]
COPY --chown=0:0 ["personal-cloud/workspace-client/client.js", "personal-cloud/workspace-client/index.mjs", "personal-cloud/workspace-client/package.json", "/opt/personal-cloud/workspace-client/"]
