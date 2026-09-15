import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

test('stock workbench has a read-only proxy and keeps the compatibility action', async () => {
	const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8');
	assert.match(source, /stockWorkbench && request\.method === 'GET'/);
	assert.match(source, /proxyResearch\(`\/api\/v1\/stocks\/\$\{encodeURIComponent\(symbol\)\}\/workbench`, url\.search, response\)/);
	assert.match(source, /stockWorkbench && request\.method === 'POST'/);
});

test('agent presentation control is exposed as an authenticated write and SSE state', async () => {
	const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8');
	assert.match(source, /url\.pathname === '\/api\/research\/workbench-control' && request\.method === 'GET'/);
	assert.match(source, /url\.pathname === '\/api\/research\/workbench-control' && request\.method === 'POST'/);
	assert.match(source, /sendSse\(response, 'workbench-control', stockWorkbenchControl\.snapshot\(\)\)/);
});
