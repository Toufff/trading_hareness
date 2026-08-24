import assert from 'node:assert/strict';
import test from 'node:test';
import { completedAssetLookupSql, deliveryRetryDelaySeconds } from './ledger.mjs';

test('only treats an asset as reusable after its parent remote batch completed', () => {
	const sql = completedAssetLookupSql();
	assert.match(sql, /JOIN ingestion_jobs j ON j\.job_id = a\.job_id/);
	assert.match(sql, /a\.state = 'completed'/);
	assert.match(sql, /j\.status = 'completed'/);
	assert.match(sql, /j\.remote_batch_id IS NOT NULL/);
});

test('delivery outbox retry delay is bounded exponential backoff', () => {
	assert.equal(deliveryRetryDelaySeconds(1), 10);
	assert.equal(deliveryRetryDelaySeconds(2), 20);
	assert.equal(deliveryRetryDelaySeconds(6), 300);
	assert.equal(deliveryRetryDelaySeconds(99), 300);
});
