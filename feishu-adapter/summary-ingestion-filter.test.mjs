import assert from 'node:assert/strict';
import test from 'node:test';
import { hasImportableTaggedPayload } from './summary-ingestion-filter.mjs';

test('filters a tag-only summary message before it reaches n8n', () => {
	assert.equal(hasImportableTaggedPayload('#anqiang'), false);
	assert.equal(hasImportableTaggedPayload('#anqiang\n   '), false);
});

test('keeps text and native-media tagged summary messages importable', () => {
	assert.equal(hasImportableTaggedPayload('#liwei\n突破内容'), true);
	assert.equal(hasImportableTaggedPayload('#quanneng', { hasMedia: true }), true);
});
