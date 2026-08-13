import { readFileSync, writeFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';

const [sourcePath, outputPath] = process.argv.slice(2);
if (!sourcePath || !outputPath) {
  throw new Error('usage: build-remote-archive-sync-workflow.mjs SOURCE_TEXT_WORKFLOW OUTPUT');
}

const source = JSON.parse(readFileSync(sourcePath, 'utf8'));
const sourceWorkflow = Array.isArray(source) ? source[0] : source;
const credentials = sourceWorkflow.nodes?.find((node) => node.credentials?.httpBearerAuth)?.credentials;
if (!credentials?.httpBearerAuth) {
  throw new Error('source workflow has no httpBearerAuth credential');
}

// The bearer value and remote URL are deliberately not versioned. n8n keeps
// the bearer in its encrypted credential store; the node only calls the local
// quant service, which forwards the header in memory to its fixed local URL.
const syncTrigger = {
  id: randomUUID(),
  name: '同步远端分析师文字',
  type: 'n8n-nodes-base.httpRequest',
  typeVersion: 4.4,
  position: [220, 0],
  parameters: {
    authentication: 'genericCredentialType',
    genericAuthType: 'httpBearerAuth',
    url: 'http://quant-research:8000/api/v1/remote-archive/sync',
    method: 'POST',
    sendBody: true,
    contentType: 'json',
    specifyBody: 'json',
    jsonBody: '=JSON.stringify({ streams: ["reports", "messages"], max_items: 100 })',
    options: { timeout: 120000, response: { includeInputData: true } },
  },
  credentials,
  retryOnFail: true,
  maxTries: 3,
  waitBetweenTries: 5000,
};

const schedule = {
  id: randomUUID(),
  name: '交易时段与盘后同步远端报告',
  type: 'n8n-nodes-base.scheduleTrigger',
  typeVersion: 1.2,
  position: [0, 0],
  parameters: { rule: { interval: [
    { field: 'cronExpression', expression: '*/15 9-11,13-14 * * 1-5' },
    { field: 'cronExpression', expression: '20 18 * * 1-5' },
  ] } },
};

const workflow = {
  id: 'remoteArchiveSync123',
  name: '市场研究：同步远端分析师文字',
  active: true,
  nodes: [schedule, syncTrigger],
  connections: {
    [schedule.name]: { main: [[{ node: syncTrigger.name, type: 'main', index: 0 }]] },
  },
  settings: { executionOrder: 'v1', timezone: 'Asia/Shanghai' },
};
writeFileSync(outputPath, JSON.stringify([workflow], null, 2) + '\n');
