FROM node:22-alpine
ARG APP_GIT_SHA=unknown
ARG APP_RELEASE=unknown
ARG APP_BUILD_CREATED_AT=unknown
WORKDIR /app
ENV NODE_ENV=production FRONTEND_MODE=spa FRONTEND_DIST=/app/frontend-dist
COPY feishu-adapter/package.json ./
RUN npm install --omit=dev --ignore-scripts --no-audit --no-fund
COPY feishu-adapter/index.mjs feishu-adapter/ledger.mjs feishu-adapter/initialize-ledger.mjs feishu-adapter/group-relay.mjs feishu-adapter/wechat-group-relay.mjs feishu-adapter/summary-listener.mjs feishu-adapter/message-filter.mjs feishu-adapter/message-time.mjs feishu-adapter/summary-ingestion-filter.mjs feishu-adapter/ingestion-health.mjs feishu-adapter/message-idempotency.mjs feishu-adapter/retry-media.mjs feishu-adapter/feishu-user-oauth.mjs feishu-adapter/feishu-workbench.mjs feishu-adapter/release-metadata.mjs feishu-adapter/stream-write.mjs feishu-adapter/single-flight.mjs feishu-adapter/paper-ingest-command.mjs feishu-adapter/paper-feedback-command.mjs feishu-adapter/personal-decision-routes.mjs feishu-adapter/baidu-pan-storage.mjs feishu-adapter/baidu-pan-market-archive.mjs feishu-adapter/dashboard-auth.mjs feishu-adapter/oauth-state.mjs feishu-adapter/manual-ids.mjs feishu-adapter/error-response.mjs feishu-adapter/filename-utils.mjs feishu-adapter/secretbox.mjs ./
COPY config/source-registry.json /app/source-registry.json
COPY frontend/dist /app/frontend-dist
COPY scripts/baidu-pan-history-export.mjs scripts/baidu-pan-upload-file.mjs /app/scripts/
ENV APP_GIT_SHA=${APP_GIT_SHA} \
    APP_RELEASE=${APP_RELEASE} \
    APP_BUILD_CREATED_AT=${APP_BUILD_CREATED_AT}
LABEL org.opencontainers.image.revision=${APP_GIT_SHA} \
      org.opencontainers.image.version=${APP_RELEASE} \
      org.opencontainers.image.created=${APP_BUILD_CREATED_AT}
USER root
CMD ["node", "index.mjs"]
