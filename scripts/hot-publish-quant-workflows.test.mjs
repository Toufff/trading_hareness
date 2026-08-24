import assert from 'node:assert/strict';
import test from 'node:test';
import { buildUpdatePayload, gatewayUrlExpression, rewriteQuantUrls } from './hot-publish-quant-workflows.mjs';

test('rewrites only direct quant HTTP nodes to the gateway fallback expression', () => {
  const workflow = {
    name: 'test', connections: {}, settings: {}, nodes: [
      { name: 'direct', type: 'n8n-nodes-base.httpRequest', parameters: { url: 'http://quant-research:8000/api/v1/pipeline/daily' } },
      { name: 'other', type: 'n8n-nodes-base.httpRequest', parameters: { url: 'https://example.invalid/keep' } },
      { name: 'code', type: 'n8n-nodes-base.code', parameters: {} },
    ],
  };
  const result = rewriteQuantUrls(workflow);
  assert.equal(result.changes.length, 1);
  assert.equal(result.workflow.nodes[0].parameters.url, `${gatewayUrlExpression}/api/v1/pipeline/daily`);
  assert.equal(result.workflow.nodes[1].parameters.url, 'https://example.invalid/keep');
});

test('update payload retains only documented mutable workflow fields', () => {
  const payload = buildUpdatePayload({ name: 'test', description: 'desc', nodes: [], connections: {}, settings: {}, nodeGroups: [], active: true, id: 'ignored' });
  assert.deepEqual(Object.keys(payload).sort(), ['connections', 'description', 'name', 'nodeGroups', 'nodes', 'settings']);
});
