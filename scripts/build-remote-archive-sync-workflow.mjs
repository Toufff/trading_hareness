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
function syncTrigger({ name, stream, y }) {
  return {
  id: randomUUID(),
  name,
  type: 'n8n-nodes-base.httpRequest',
  typeVersion: 4.4,
  position: [220, y],
  parameters: {
    authentication: 'genericCredentialType',
    genericAuthType: 'httpBearerAuth',
    url: 'http://quant-research:8000/api/v1/remote-archive/sync',
    method: 'POST',
    sendBody: true,
    contentType: 'json',
    specifyBody: 'json',
    jsonBody: `=JSON.stringify({ streams: ["${stream}"], max_items: 100 })`,
    options: { timeout: 120000, response: { includeInputData: true } },
  },
  credentials,
  retryOnFail: true,
  maxTries: 3,
  waitBetweenTries: 5000,
  };
}

function schedule({ name, intervals, y }) {
  return {
  id: randomUUID(),
  name,
  type: 'n8n-nodes-base.scheduleTrigger',
  typeVersion: 1.2,
  position: [0, y],
  parameters: { rule: { interval: intervals.map((expression) => ({ field: 'cronExpression', expression })) } },
  };
}

function workflow({ id, name, triggerName, stream, intervals, y }) {
  const trigger = syncTrigger({ name: triggerName, stream, y });
  const clock = schedule({ name: `${name} 定时`, intervals, y });
  return {
    id, name, active: true, nodes: [clock, trigger],
    connections: { [clock.name]: { main: [[{ node: trigger.name, type: 'main', index: 0 }]] } },
    settings: { executionOrder: 'v1', timezone: 'Asia/Shanghai' },
  };
}

// Messages are deliberately offset from reports.  A 429 or slow catalog
// request can never make the fresh text stream miss its own scheduled run.
const workflows = [
  workflow({
    id: 'remoteArchiveReports123', name: '市场研究：同步远端分析师报告',
    triggerName: '同步远端分析师报告文字', stream: 'reports',
    intervals: ['*/15 9-11,13-14 * * 1-5', '20 18 * * 1-5'], y: 0,
  }),
  workflow({
    id: 'remoteArchiveMessages123', name: '市场研究：同步远端分析师消息',
    triggerName: '同步远端分析师消息文字', stream: 'messages',
    intervals: ['2-59/15 9-11,13-14 * * 1-5', '22 18 * * 1-5'], y: 160,
  }),
];
writeFileSync(outputPath, JSON.stringify(workflows, null, 2) + '\n');
