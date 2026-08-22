import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

test('xiaojie analyst route is registered for tagged summary ingestion', () => {
	const registry = JSON.parse(readFileSync(new URL('../config/source-registry.json', import.meta.url), 'utf8'));
	const route = registry.routes.find((item) => item.tag === 'xiaojie');
	assert.deepEqual(route, {
		tag: 'xiaojie', label: '小杰', topic_key: 'market', publisher_key: 'xiaojie', remote_analyst_id: 'xiaojie', enabled: true,
	});
});
