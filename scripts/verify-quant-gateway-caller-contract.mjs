import { readFileSync } from 'node:fs';

const workflowFiles = [
  'workflows/quant-intraday-alerts.json',
  'workflows/quant-market-snapshots.json',
];
const gatewayExpression = "={{ $env.QUANT_SERVICE_URL || 'http://quant-research-gateway:8000' }}";

for (const path of workflowFiles) {
  const workflow = JSON.parse(readFileSync(path, 'utf8'));
  for (const node of workflow.nodes ?? []) {
    if (node.type !== 'n8n-nodes-base.httpRequest') continue;
    const url = String(node.parameters?.url ?? '');
    if (!url.startsWith(`${gatewayExpression}/api/`)) {
      throw new Error(`${path}:${node.name} must use QUANT_SERVICE_URL, received ${url || '<empty>'}`);
    }
  }
}

const compose = readFileSync('compose.yaml', 'utf8');
for (const expected of [
  'QUANT_SERVICE_URL: http://quant-research-gateway:8000',
  'quant-research-gateway:\n        condition: service_healthy',
]) {
  if (!compose.includes(expected)) throw new Error(`compose gateway contract missing: ${expected}`);
}
if (compose.includes('QUANT_SERVICE_URL: http://quant-research:8000')) {
  throw new Error('compose still routes a configured caller directly to quant-research');
}

const serverCompose = readFileSync('deploy/compose.server.yaml', 'utf8');
for (const expected of [
  'quant-research-gateway:',
  'quant-research-preflight:',
  'QUANT_SERVICE_URL: http://quant-research-gateway:8000',
]) {
  if (!serverCompose.includes(expected)) throw new Error(`server compose gateway contract missing: ${expected}`);
}
if (serverCompose.includes('QUANT_SERVICE_URL: http://quant-research:8000')) {
  throw new Error('server compose still routes a configured caller directly to quant-research');
}

for (const path of [
  'scripts/build-remote-archive-sync-workflow.mjs',
  'scripts/build-factor-research-workflow.mjs',
  'scripts/build-quant-daily-workflow.mjs',
  'scripts/converge-remote-archive-sync-workflow.sh',
  'scripts/converge-n8n-quant-daily-workflow.sh',
]) {
  const source = readFileSync(path, 'utf8');
  if (!source.includes(gatewayExpression)) throw new Error(`${path} must verify/build the gateway expression`);
  if (source.includes('http://quant-research:8000')) throw new Error(`${path} still contains a direct quant address`);
}

const adapter = readFileSync('feishu-adapter/index.mjs', 'utf8');
for (const expected of [
  "['/api/research/ten-day-leader-rotation/latest', '/api/v1/research/ten-day-leader-rotation/latest']",
  "['/api/research/ten-day-leader-rotation/run', '/api/v1/research/ten-day-leader-rotation/run']",
  "path.includes('/ten-day-leader-rotation/run')",
]) {
  if (!adapter.includes(expected)) throw new Error(`adapter shadow-strategy proxy contract missing: ${expected}`);
}

console.log(`quant gateway caller contract verified: ${workflowFiles.length} checked-in workflow artifacts and 5 workflow builders/convergers`);
