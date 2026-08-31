import test from 'node:test';
import assert from 'node:assert/strict';
import { archiveDirectory, buildPageQuery, exportDataset, parseArgs } from './baidu-pan-history-export.mjs';

test('history export creates deterministic partition paths and manifest metadata', async () => {
	const queries = [];
	const uploads = [];
	const pool = { async query(sql, params) { queries.push({ sql, params }); return queries.length === 1 ? { rows: [{ row: { symbol: '600000.SH', trading_date: '2026-08-28', close: 10.2 } }] } : { rows: [] }; } };
	let directoryChecks = 0;
	const baiduPan = {
		async list() { directoryChecks += 1; if (directoryChecks <= 5) throw new Error('missing directory'); return { list: [] }; },
		async mkdir(path) { return { path }; },
		async uploadReadable(input) { uploads.push(input); return { path: input.remotePath, fsId: String(uploads.length) }; },
	};
	const result = await exportDataset({ pool, baiduPan, dataset: 'canonical_bars_daily', from: '2026-08-28', to: '2026-08-29' });
	assert.equal(result.total_rows, 1);
	assert.equal(result.parts.length, 1);
	assert.match(result.parts[0].path, /market-realtime\/history\/canonical_bars_daily\/2026-08-28_2026-08-29\/part-000001\.jsonl\.gz$/);
	assert.equal(uploads.length, 2);
	assert.equal(uploads[1].fileName, 'manifest.json');
	assert.equal(archiveDirectory('/archive', 'canonical_bars_daily', '2026-08-28', '2026-08-29'), '/archive/canonical_bars_daily/2026-08-28_2026-08-29');
});

test('history export argument parser is bounded and rejects reversed dates', () => {
	const args = parseArgs(['--dataset', 'intraday_quote_observations', '--from', '2026-08-01', '--to', '2026-08-02', '--part-rows', '200000']);
	assert.equal(args.partRows, 100000);
	assert.throws(() => parseArgs(['--dataset', 'canonical_bars_daily', '--from', '2026-08-02', '--to', '2026-08-01']), /from < to/);
});

test('history export uses keyset pagination instead of OFFSET for large tables', () => {
	const spec = { table: 'quant.example', dateColumn: 'observed_at', order: ['observed_at', 'row_id'] };
	const first = buildPageQuery(spec, '2026-01-01', '2026-02-01', null, 5000);
	assert.doesNotMatch(first.sql, /OFFSET/i);
	assert.match(first.sql, /ORDER BY t\.observed_at, t\.row_id LIMIT \$3$/);
	assert.deepEqual(first.params, ['2026-01-01', '2026-02-01', 5000]);
	const next = buildPageQuery(spec, '2026-01-01', '2026-02-01', { observed_at: '2026-01-15T09:30:00Z', row_id: 123 }, 5000);
	assert.match(next.sql, /\(t\.observed_at, t\.row_id\) > \(\$3, \$4\)/);
	assert.match(next.sql, /LIMIT \$5$/);
	assert.deepEqual(next.params, ['2026-01-01', '2026-02-01', '2026-01-15T09:30:00Z', 123, 5000]);
});
