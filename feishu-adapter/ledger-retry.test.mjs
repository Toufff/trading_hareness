import assert from 'node:assert/strict';
import test from 'node:test';
import { createLedger, getOrCreateJobInsertSql } from './ledger.mjs';

function fakePool(responses) {
	const calls = [];
	return {
		calls,
		async query(sql, params) {
			calls.push({ sql, params });
			const next = responses.shift();
			if (next instanceof Error) throw next;
			return next ?? { rows: [], rowCount: 0 };
		},
	};
}

test('getOrCreateJob insert SQL relies on a plain ON CONFLICT DO NOTHING to catch either unique key atomically', () => {
	const sql = getOrCreateJobInsertSql();
	assert.match(sql, /ON CONFLICT DO NOTHING RETURNING \*/);
	assert.doesNotMatch(sql, /ON CONFLICT\s*\(/);
});

test('getOrCreateJob returns the freshly inserted row without a second query when the insert wins the race', async () => {
	const pool = fakePool([{ rows: [{ job_id: 'job-1' }], rowCount: 1 }]);
	const ledger = createLedger(undefined, { pool });
	const result = await ledger.getOrCreateJob({ jobId: 'job-1', eventId: 'evt-1', messageId: 'msg-1', route: { tag: 't', topic_key: 'k', publisher_key: 'p', remote_analyst_id: 'a' }, payload: {}, contentSha256: null });
	assert.equal(result.duplicate, false);
	assert.equal(result.job.job_id, 'job-1');
	assert.equal(pool.calls.length, 1);
});

test('getOrCreateJob falls back to a lookup and reports a duplicate when the insert loses the race', async () => {
	const pool = fakePool([
		{ rows: [], rowCount: 0 },
		{ rows: [{ job_id: 'job-existing' }], rowCount: 1 },
	]);
	const ledger = createLedger(undefined, { pool });
	const result = await ledger.getOrCreateJob({ jobId: 'job-2', eventId: 'evt-1', messageId: 'msg-1', route: { tag: 't', topic_key: 'k', publisher_key: 'p', remote_analyst_id: 'a' }, payload: {}, contentSha256: null });
	assert.equal(result.duplicate, true);
	assert.equal(result.job.job_id, 'job-existing');
	assert.equal(pool.calls.length, 2);
	assert.match(pool.calls[1].sql, /SELECT \* FROM ingestion_jobs WHERE event_id=\$1 OR message_id=\$2/);
});

test('failDelivery keeps retrying under the attempt ceiling', async () => {
	const pool = fakePool([
		{ rows: [{ attempt_count: 3 }], rowCount: 1 },
		{ rows: [{ delivery_id: 'd1', status: 'retryable_failed' }], rowCount: 1 },
	]);
	const ledger = createLedger(undefined, { pool });
	await ledger.failDelivery('d1', { errorMessage: 'boom', retryable: true, maxAttempts: 20 });
	const updateCall = pool.calls[1];
	assert.equal(updateCall.params[1], true, 'still retryable under the ceiling');
});

test('failDelivery forces a terminal failure once max attempts is reached, even when the caller says retryable', async () => {
	const pool = fakePool([
		{ rows: [{ attempt_count: 20 }], rowCount: 1 },
		{ rows: [{ delivery_id: 'd1', status: 'failed' }], rowCount: 1 },
	]);
	const ledger = createLedger(undefined, { pool });
	await ledger.failDelivery('d1', { errorMessage: 'boom', retryable: true, maxAttempts: 20 });
	const updateCall = pool.calls[1];
	assert.equal(updateCall.params[1], false, 'exhausted retries must not stay retryable');
	assert.match(updateCall.params[2], /已达最大重试次数/);
});

test('claimRelayMessage bounds the failed-status reclaim window by attempt_count', async () => {
	const pool = fakePool([{ rows: [{ source_message_id: 'm1' }], rowCount: 1 }]);
	const ledger = createLedger(undefined, { pool });
	await ledger.claimRelayMessage({ sourceMessageId: 'm1', sourceKey: 'k', sourceChatId: 'c', sourceCreateTime: 1, targetChatId: 't', routeTag: 'tag', message: {} });
	const call = pool.calls[0];
	assert.match(call.sql, /attempt_count < \$9/);
	assert.equal(call.params[8], 20);
});
