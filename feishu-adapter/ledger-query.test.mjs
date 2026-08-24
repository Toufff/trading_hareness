import assert from 'node:assert/strict';
import test from 'node:test';
import { completedAssetLookupSql } from './ledger.mjs';

test('only treats an asset as reusable after its parent remote batch completed', () => {
	const sql = completedAssetLookupSql();
	assert.match(sql, /JOIN ingestion_jobs j ON j\.job_id = a\.job_id/);
	assert.match(sql, /a\.state = 'completed'/);
	assert.match(sql, /j\.status = 'completed'/);
	assert.match(sql, /j\.remote_batch_id IS NOT NULL/);
});
