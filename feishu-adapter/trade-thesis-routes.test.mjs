import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

test('trade thesis GET and POST routes stay separately mapped', async () => {
	const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8');
	assert.match(source, /\['\/api\/research\/theses', '\/api\/v1\/research\/theses'\]/);
	assert.match(source, /\['\/api\/research\/theses\/evaluate', '\/api\/v1\/research\/theses\/evaluate'\]/);
	assert.match(source, /thesisTimeline && request\.method === 'GET'/);
	assert.match(source, /thesisMutation && request\.method === 'POST'/);
});
