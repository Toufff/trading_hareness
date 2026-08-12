FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production FRONTEND_MODE=spa FRONTEND_DIST=/app/frontend-dist
COPY feishu-adapter/package.json ./
RUN npm install --omit=dev --ignore-scripts --no-audit --no-fund
COPY feishu-adapter/index.mjs feishu-adapter/ledger.mjs ./
COPY config/source-registry.json /app/source-registry.json
COPY frontend/dist /app/frontend-dist
USER root
CMD ["node", "index.mjs"]
