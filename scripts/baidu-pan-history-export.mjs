#!/usr/bin/env node

import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { Readable } from 'node:stream';
import { gzipSync } from 'node:zlib';
import pg from '../feishu-adapter/node_modules/pg/lib/index.js';
import { createLedger } from '../feishu-adapter/ledger.mjs';
import { createBaiduPanStorage, normalizeBaiduPanPath } from '../feishu-adapter/baidu-pan-storage.mjs';

const DEFAULT_ROOT = '/apps/股票paper存储/market-realtime/history';
const DEFAULT_PART_ROWS = 5000;
const DEFAULT_MAX_PART_BYTES = 256 * 1024 * 1024;

const DATASETS = Object.freeze({
	canonical_bars_daily: { table: 'quant.canonical_bars_daily', dateColumn: 'trading_date', order: ['trading_date', 'symbol'] },
	daily_fundamentals: { table: 'quant.daily_fundamentals', dateColumn: 'trading_date', order: ['trading_date', 'symbol'] },
	daily_trade_limits: { table: 'quant.daily_trade_limits', dateColumn: 'trading_date', order: ['trading_date', 'symbol'] },
	daily_adjustment_factors: { table: 'quant.daily_adjustment_factors', dateColumn: 'trading_date', order: ['trading_date', 'symbol'] },
	stock_money_flow_daily: { table: 'quant.stock_money_flow_daily', dateColumn: 'trading_date', order: ['trading_date', 'symbol'] },
	raw_market_observations: { table: 'quant.raw_market_observations', dateColumn: 'effective_at', order: ['effective_at', 'observation_id'] },
	tushare_raw_records: { table: 'quant.tushare_raw_records', dateColumn: 'available_at', order: ['available_at', 'record_id'] },
	intraday_quote_observations: { table: 'quant.intraday_quote_observations', dateColumn: 'observed_at', order: ['observed_at', 'quote_observation_id'] },
	intraday_rule_input_snapshots: { table: 'quant.intraday_rule_input_snapshots', dateColumn: 'observed_at', order: ['observed_at', 'rule_input_snapshot_id'] },
	intraday_signal_events: { table: 'quant.intraday_signal_events', dateColumn: 'observed_at', order: ['observed_at', 'signal_event_id'] },
	intraday_board_flow_snapshots: { table: 'quant.intraday_board_flow_snapshots', dateColumn: 'observed_at', order: ['observed_at', 'flow_snapshot_id'] },
	ten_day_leader_rotation_intraday_observations: { table: 'quant.ten_day_leader_rotation_intraday_observations', dateColumn: 'observed_at', order: ['observed_at', 'observation_id'] },
	xiaojie_leader_flow_observations: { table: 'quant.xiaojie_leader_flow_observations', dateColumn: 'first_seen_at', order: ['first_seen_at', 'symbol'] },
	analyst_observations: { table: 'quant.analyst_observations', dateColumn: 'stated_at', order: ['stated_at', 'observation_id'] },
	analyst_evidence: { table: 'quant.analyst_evidence', dateColumn: 'available_at', order: ['available_at', 'evidence_id'] },
});

function safeSegment(value) {
	const normalized = String(value ?? '').trim().replace(/[^a-zA-Z0-9._-]+/g, '_').slice(0, 120);
	if (!normalized) throw new Error('archive path segment cannot be empty');
	return normalized;
}

function parseArgs(argv) {
	const args = { dataset: '', from: '', to: '', root: process.env.BAIDU_PAN_HISTORY_ROOT_PATH || DEFAULT_ROOT, partRows: DEFAULT_PART_ROWS, maxPartBytes: DEFAULT_MAX_PART_BYTES, dryRun: false };
	for (let i = 0; i < argv.length; i += 1) {
		const arg = argv[i];
		if (arg === '--dry-run') { args.dryRun = true; continue; }
		if (!arg.startsWith('--')) throw new Error(`unknown argument: ${arg}`);
		const key = arg.slice(2).replaceAll('-', '');
		const value = argv[++i];
		if (value === undefined) throw new Error(`${arg} requires a value`);
		if (key === 'dataset') args.dataset = value;
		else if (key === 'from') args.from = value;
		else if (key === 'to') args.to = value;
		else if (key === 'root') args.root = value;
		else if (key === 'partrows') args.partRows = Math.max(1, Math.min(100_000, Number(value) || DEFAULT_PART_ROWS));
		else if (key === 'maxpartbytes') args.maxPartBytes = Math.max(1_048_576, Math.min(450 * 1024 * 1024, Number(value) || DEFAULT_MAX_PART_BYTES));
		else throw new Error(`unknown argument: ${arg}`);
	}
	if (!DATASETS[args.dataset]) throw new Error(`dataset must be one of: ${Object.keys(DATASETS).join(', ')}`);
	if (!/^\d{4}-\d{2}-\d{2}$/.test(args.from) || !/^\d{4}-\d{2}-\d{2}$/.test(args.to) || args.from >= args.to) throw new Error('--from/--to must be YYYY-MM-DD with from < to');
	return args;
}

function archiveDirectory(root, dataset, from, to) {
	return normalizeBaiduPanPath(`${root}/${safeSegment(dataset)}/${safeSegment(from)}_${safeSegment(to)}`);
}

function manifestPath(directory) {
	return `${directory}/manifest.json`;
}

function sha256(buffer) {
	return createHash('sha256').update(buffer).digest('hex');
}

function rowJson(row) {
	return `${JSON.stringify(row)}\n`;
}

async function ensureDirectory(baiduPan, path) {
	const parts = normalizeBaiduPanPath(path).split('/').filter(Boolean);
	let current = '';
	for (const part of parts) {
		current += `/${part}`;
		try { await baiduPan.list({ dir: current, limit: 1 }); }
		catch { await baiduPan.mkdir(current); }
	}
}

async function remoteExists(baiduPan, directory, filename) {
	const listed = await baiduPan.list({ dir: directory, limit: 1000 });
	return (listed.list ?? []).some((item) => item?.server_filename === filename && Number(item?.isdir) !== 1);
}

async function uploadBuffer(baiduPan, directory, filename, buffer) {
	const remotePath = `${directory}/${filename}`;
	if (await remoteExists(baiduPan, directory, filename)) return { path: remotePath, skipped: true, bytes: buffer.length, sha256: sha256(buffer) };
	const result = await baiduPan.uploadReadable({ readable: Readable.from([buffer]), fileName: filename, size: buffer.length, remotePath });
	return { path: result.path ?? remotePath, skipped: false, bytes: buffer.length, sha256: sha256(buffer) };
}

async function exportDataset({ pool, baiduPan, dataset, from, to, root = DEFAULT_ROOT, partRows = DEFAULT_PART_ROWS, maxPartBytes = DEFAULT_MAX_PART_BYTES, dryRun = false, logger = console }) {
	const spec = DATASETS[dataset];
	const directory = archiveDirectory(root, dataset, from, to);
	if (!dryRun) await ensureDirectory(baiduPan, directory);
	const orderBy = spec.order.map((column) => `t.${column}`).join(', ');
	const query = `SELECT to_jsonb(t) AS row FROM ${spec.table} t WHERE t.${spec.dateColumn} >= $1::date AND t.${spec.dateColumn} < $2::date ORDER BY ${orderBy} LIMIT $3 OFFSET $4`;
	const parts = [];
	let offset = 0;
	let partNumber = 0;
	let totalRows = 0;
	let totalBytes = 0;
	while (true) {
		const result = await pool.query(query, [from, to, partRows, offset]);
		if (!result.rows.length) break;
		const raw = Buffer.from(result.rows.map((item) => rowJson(item.row)).join(''), 'utf8');
		const compressed = gzipSync(raw, { level: 6 });
		if (compressed.length > maxPartBytes) throw new Error(`part ${partNumber + 1} compressed size exceeds configured limit; lower --part-rows`);
		partNumber += 1;
		const filename = `part-${String(partNumber).padStart(6, '0')}.jsonl.gz`;
		const uploaded = dryRun ? { path: `${directory}/${filename}`, skipped: false, bytes: compressed.length, sha256: sha256(compressed) } : await uploadBuffer(baiduPan, directory, filename, compressed);
		parts.push({ filename, rows: result.rows.length, ...uploaded });
		totalRows += result.rows.length;
		totalBytes += compressed.length;
		offset += result.rows.length;
		logger.info(`历史归档 ${dataset}: ${filename}, rows=${result.rows.length}, bytes=${compressed.length}${uploaded.skipped ? ', skipped=existing' : ''}`);
		if (result.rows.length < partRows) break;
	}
	const manifest = {
		schema: 'market-history-archive-v1', dataset, table: spec.table, date_column: spec.dateColumn,
		from, to, generated_at: new Date().toISOString(), total_rows: totalRows, compressed_bytes: totalBytes,
		parts, research_only: true, live_effect: 'none', restore_policy: 'staging_schema_only',
	};
	const manifestBuffer = Buffer.from(`${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
	if (!dryRun) await uploadBuffer(baiduPan, directory, 'manifest.json', manifestBuffer);
	return { ...manifest, manifest_path: manifestPath(directory), manifest_sha256: sha256(manifestBuffer), dry_run: dryRun };
}

async function main() {
	const args = parseArgs(process.argv.slice(2));
	const password = process.env.POSTGRES_PASSWORD;
	if (!password) throw new Error('POSTGRES_PASSWORD is required');
	const host = process.env.PGHOST || '127.0.0.1';
	const port = process.env.PGPORT || '5432';
	const database = process.env.PGDATABASE || 'n8n';
	const user = process.env.PGUSER || 'n8n';
	const connectionString = `postgresql://${encodeURIComponent(user)}:${encodeURIComponent(password)}@${host}:${port}/${encodeURIComponent(database)}`;
	const pool = new pg.Pool({ connectionString, max: 2 });
	const ledger = createLedger(connectionString);
	const baiduPan = createBaiduPanStorage({ appKey: process.env.BAIDU_PAN_APP_KEY, secretKey: process.env.BAIDU_PAN_SECRET_KEY, redirectUri: process.env.BAIDU_PAN_REDIRECT_URI || 'oob', ledger, rootPath: process.env.BAIDU_PAN_ROOT_PATH || '/' });
	try {
		const result = await exportDataset({ pool, baiduPan, ...args });
		console.log(JSON.stringify(result, null, 2));
	} finally {
		await pool.end();
	}
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) main().catch((error) => { console.error(error?.stack || error); process.exitCode = 1; });

export { DATASETS, archiveDirectory, exportDataset, manifestPath, parseArgs, sha256 };
