import assert from 'node:assert/strict';
import test from 'node:test';
import { manualEventId, manualMessageId } from './manual-ids.mjs';

test('manual event and message ids always carry the manual: prefix', () => {
	assert.match(manualEventId(), /^manual:[0-9a-f-]{36}$/);
	assert.match(manualMessageId(), /^manual:[0-9a-f-]{36}$/);
});

test('manual ids are unique per call so they can never be pre-registered by a caller', () => {
	const ids = new Set([manualEventId(), manualEventId(), manualMessageId(), manualMessageId()]);
	assert.equal(ids.size, 4);
});
