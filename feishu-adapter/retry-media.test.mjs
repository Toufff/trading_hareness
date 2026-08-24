import assert from 'node:assert/strict';
import test from 'node:test';
import { shouldRedownloadRetryMedia } from './retry-media.mjs';

const event = { message: { message_id: 'om_source' } };

test('always re-downloads a Feishu resource before retrying it', () => {
	assert.equal(shouldRedownloadRetryMedia({ expectedResourceCount: 1, event }), true);
});

test('does not try to redownload manual or text-only retries', () => {
	assert.equal(shouldRedownloadRetryMedia({ expectedResourceCount: 1, event: null }), false);
	assert.equal(shouldRedownloadRetryMedia({ expectedResourceCount: 0, event }), false);
});
