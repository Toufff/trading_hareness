import assert from 'node:assert/strict';
import test from 'node:test';
import { shouldSkipMessageForward } from './message-idempotency.mjs';

test('only an existing source message suppresses a normal delivery', () => {
	assert.equal(shouldSkipMessageForward({ existingJob: null }), false);
	assert.equal(shouldSkipMessageForward({ existingJob: { message_id: 'om_existing' } }), true);
	assert.equal(shouldSkipMessageForward({ existingJob: { message_id: 'om_existing' }, replayJobId: 'job-retry' }), false);
});
