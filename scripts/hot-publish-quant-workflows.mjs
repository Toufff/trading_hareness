import { pathToFileURL } from 'node:url';

export const gatewayUrlExpression = "={{ $env.QUANT_SERVICE_URL || 'http://quant-research-gateway:8000' }}";
export const workflowIds = [
  'quantDailyResearch123',
  'quantFactorResearch123',
  'quantMarketSnapshots123',
  'remoteArchiveMessages123',
  'remoteArchiveReports123',
];

export function rewriteQuantUrls(workflow) {
  const nodes = Array.isArray(workflow?.nodes) ? workflow.nodes : [];
  const changes = [];
  const rewrittenNodes = nodes.map((node) => {
    if (node?.type !== 'n8n-nodes-base.httpRequest') return node;
    const url = String(node.parameters?.url ?? '');
    if (!url.startsWith('http://quant-research:8000/')) return node;
    const nextUrl = `${gatewayUrlExpression}${url.slice('http://quant-research:8000'.length)}`;
    changes.push({ node: String(node.name ?? '<unnamed>'), from: url, to: nextUrl });
    return { ...node, parameters: { ...node.parameters, url: nextUrl } };
  });
  return { workflow: { ...workflow, nodes: rewrittenNodes }, changes };
}

export function buildUpdatePayload(workflow) {
  if (!workflow?.name || !Array.isArray(workflow.nodes) || !workflow.connections || !workflow.settings) {
    throw new Error('n8n workflow response lacks required update fields');
  }
  const payload = {
    name: workflow.name,
    nodes: workflow.nodes,
    connections: workflow.connections,
    settings: workflow.settings,
  };
  if (typeof workflow.description === 'string') payload.description = workflow.description;
  if (Array.isArray(workflow.nodeGroups)) payload.nodeGroups = workflow.nodeGroups;
  return payload;
}

function option(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

async function request(baseUrl, apiKey, path, init = {}) {
  const response = await fetch(`${baseUrl.replace(/\/$/, '')}${path}`, {
    ...init,
    headers: {
      accept: 'application/json',
      'X-N8N-API-KEY': apiKey,
      ...init.headers,
    },
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) throw new Error(`n8n API ${init.method ?? 'GET'} ${path} returned HTTP ${response.status}`);
  return await response.json();
}

export async function main() {
  if (process.argv.includes('--help')) {
    console.log('Usage: N8N_API_KEY=... node scripts/hot-publish-quant-workflows.mjs [--apply] [--base-url http://127.0.0.1:5678/api/v1]');
    console.log('Default is a read-only dry run. --apply updates only direct quant-research URLs and lets n8n auto-republish active workflows.');
    return;
  }
  const apiKey = String(process.env.N8N_API_KEY ?? '');
  if (!apiKey) throw new Error('N8N_API_KEY is required; create a scoped n8n API key with workflow:read, workflow:update, and workflow:activate');
  const baseUrl = option('--base-url') ?? 'http://127.0.0.1:5678/api/v1';
  const apply = process.argv.includes('--apply');
  const results = [];
  for (const id of workflowIds) {
    const current = await request(baseUrl, apiKey, `/workflows/${encodeURIComponent(id)}?excludePinnedData=true`);
    const { workflow, changes } = rewriteQuantUrls(current);
    results.push({ id, changes: changes.length });
    if (!apply || !changes.length) continue;
    const updated = await request(baseUrl, apiKey, `/workflows/${encodeURIComponent(id)}`, {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(buildUpdatePayload(workflow)),
    });
    const remaining = rewriteQuantUrls(updated).changes;
    if (remaining.length) throw new Error(`${id} still contains ${remaining.length} direct quant URLs after update`);
    if (!updated.active) throw new Error(`${id} was no longer published after update`);
  }
  console.log(`${apply ? 'hot publish' : 'dry run'} complete: ${results.map((item) => `${item.id}=${item.changes}`).join(', ')}`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}
