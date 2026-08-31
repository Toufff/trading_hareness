import test from 'node:test';
import assert from 'node:assert/strict';
import { createBaiduPanMarketArchive } from './baidu-pan-market-archive.mjs';

test('archives latest watchlist and leader evidence asynchronously with idempotent keys', async () => {
	const jobs = [];
	const uploaded = [];
	const completed = [];
	const ledger = {
		async enqueueBaiduPanArchive(input) {
			if (jobs.some((item) => item.archive_key === input.archiveKey)) return null;
			const row = { archive_id: `id-${jobs.length + 1}`, archive_key: input.archiveKey, bucket: input.bucket, observed_at: input.observedAt, payload: input.payload };
			jobs.push(row);
			return row;
		},
		async claimBaiduPanArchives() { return jobs.filter((item) => !completed.includes(item.archive_id)); },
		async completeBaiduPanArchive(id, value) { completed.push(id); return { archive_id: id, ...value }; },
		async failBaiduPanArchive() { throw new Error('unexpected failure'); },
		async baiduPanArchiveStatus() { return { queue_depth: jobs.length - completed.length, completed: completed.length }; },
	};
	const baiduPan = {
		async list() { return { list: [] }; },
		async mkdir() { return { errno: 0 }; },
		async uploadReadable(input) { uploaded.push(input.remotePath); return { path: input.remotePath, fsId: uploaded.length }; },
	};
	const fetchImpl = async (url) => {
		if (url.endsWith('/api/v1/intraday/scans/latest?limit=200')) return new Response(JSON.stringify({ scan: { scan_id: 'scan-1', observed_at: '2026-08-30T08:00:00.000Z' }, signals: [] }), { status: 200 });
		if (url.endsWith('/api/v1/research/ten-day-leader-rotation/latest?limit=90')) return new Response(JSON.stringify({ run: { run_id: 'run-1', updated_at: '2026-08-30T08:00:00.000Z' }, candidates: [], intraday: { latest_batch: { scan_id: 'scan-1', observed_at: '2026-08-30T08:00:00.000Z' } } }), { status: 200 });
		return new Response(JSON.stringify({}), { status: 200 });
	};
	const archive = createBaiduPanMarketArchive({ baiduPan, ledger, quantServiceUrl: 'http://quant', enabled: true, fetchImpl, rootPath: '/archive' });
	await archive.poll();
	assert.equal(completed.length, 2);
	assert.equal(uploaded.length, 2);
	assert.ok(uploaded.some((path) => path.includes('/watchlist/')));
	assert.ok(uploaded.some((path) => path.includes('/leader-rotation/')));
	const status = await archive.status();
	assert.equal(status.queue.completed, 2);
});
