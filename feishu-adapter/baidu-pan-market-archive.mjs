import { Readable } from 'node:stream';

const DEFAULT_ROOT = '/apps/股票paper存储/market-realtime';
const MAX_SNAPSHOT_BYTES = 12 * 1024 * 1024;

function latestTimestamp(values) {
	return values
		.map((value) => text(value))
		.filter(Boolean)
		.sort()
		.at(-1) ?? null;
}

const SNAPSHOT_SPECS = [
	{
		bucket: 'market-events',
		path: '/api/v1/events/market?limit=500',
		source: 'quant.market.events',
		identity: (body) => {
			const items = body?.items ?? [];
			const latest = items[0];
			return latest?.event_id && latest?.occurred_at ? `${latest.event_id}:${latest.occurred_at}:${body?.total ?? items.length}` : '';
		},
		observedAt: (body) => latestTimestamp((body?.items ?? []).map((item) => item?.occurred_at ?? item?.available_at)),
	},
	{
		bucket: 'all-a-level1',
		path: '/api/v1/market/level1/latest?limit=6000',
		source: 'quant.market.level1.latest',
		identity: (body) => text(body?.snapshot_at),
		observedAt: (body) => body?.snapshot_at,
	},
	{
		bucket: 'watchlist',
		path: '/api/v1/intraday/scans/latest?limit=200',
		source: 'quant.intraday.scans.latest',
		identity: (body) => text(body?.scan?.scan_id),
		observedAt: (body) => body?.scan?.observed_at,
	},
	{
		bucket: 'leader-rotation',
		path: '/api/v1/research/ten-day-leader-rotation/latest?limit=90',
		source: 'quant.ten_day_leader_rotation.latest',
		identity: (body) => {
			const runId = text(body?.run?.run_id, 'none');
			const batchId = text(body?.intraday?.latest_batch?.scan_id, text(body?.intraday?.latest_batch?.observed_at, 'none'));
			return body?.run || body?.intraday?.latest_batch ? `${runId}:${batchId}` : '';
		},
		observedAt: (body) => body?.intraday?.latest_batch?.observed_at ?? body?.run?.updated_at,
	},
	{
		bucket: 'board-rotation',
		path: '/api/v1/intraday/board-rotations/latest',
		source: 'quant.intraday.board_rotations.latest',
		identity: (body) => latestTimestamp((body?.items ?? []).map((item) => item?.updated_at ?? item?.snapshot_minute)),
		observedAt: (body) => latestTimestamp((body?.items ?? []).map((item) => item?.updated_at ?? item?.snapshot_minute)),
	},
	{
		bucket: 'board-mining',
		path: '/api/v1/intraday/board-stock-mining/latest',
		source: 'quant.intraday.board_stock_mining.latest',
		identity: (body) => text(body?.run?.mining_run_id),
		observedAt: (body) => body?.run?.observed_at,
	},
	{
		bucket: 'limit-linkage',
		path: '/api/v1/intraday/limit-linkage/latest',
		source: 'quant.intraday.limit_linkage.latest',
		identity: (body) => text(body?.run?.linkage_run_id),
		observedAt: (body) => body?.run?.observed_at,
	},
	{
		bucket: 'sector-curves',
		path: '/api/v1/market/sectors/intraday/curves',
		source: 'quant.market.sectors.intraday.curves',
		identity: (body) => {
			const points = (body?.items ?? []).flatMap((item) => item?.points ?? []);
			const latest = latestTimestamp(points.map((point) => point?.observed_at));
			return body?.trade_date && latest ? `${body.trade_date}:${latest}` : '';
		},
		observedAt: (body) => latestTimestamp((body?.items ?? []).flatMap((item) => (item?.points ?? []).map((point) => point?.observed_at))),
	},
	{
		bucket: 'strategy-decisions',
		path: '/api/v1/strategy/decisions/latest',
		source: 'quant.strategy.decisions.latest',
		identity: (body) => text(body?.run?.run_id),
		observedAt: (body) => body?.run?.observed_at ?? body?.run?.created_at,
	},
	{
		bucket: 'strategy-health',
		path: '/api/v1/strategy/health',
		source: 'quant.strategy.health',
		identity: (body) => text(body?.observed_at),
		observedAt: (body) => body?.observed_at,
	},
	{
		bucket: 'post-close',
		path: '/api/v1/strategy/post-close/latest',
		source: 'quant.strategy.post_close.latest',
		identity: (body) => text(body?.run?.run_id ?? body?.candidate_run?.run_id),
		observedAt: (body) => body?.run?.updated_at ?? body?.candidate_run?.updated_at,
	},
	{
		bucket: 'pattern-mining',
		path: '/api/v1/strategy/pattern-mining/latest',
		source: 'quant.strategy.pattern_mining.latest',
		identity: (body) => text(body?.run?.run_id),
		observedAt: (body) => body?.run?.updated_at ?? body?.run?.created_at,
	},
	{
		bucket: 'strategy-reviews',
		path: '/api/v1/strategy/reviews/latest',
		source: 'quant.strategy.reviews.latest',
		identity: (body) => text(body?.review?.review_id),
		observedAt: (body) => body?.review?.observed_at ?? body?.review?.created_at,
	},
];

function text(value, fallback = '') {
	return String(value ?? fallback).trim();
}

function exchangeDate(value) {
	const date = new Date(value || Date.now());
	if (Number.isNaN(date.getTime())) return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date());
	return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(date);
}

function hourPart(value) {
	const date = new Date(value || Date.now());
	if (Number.isNaN(date.getTime())) return '00';
	return new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Shanghai', hour: '2-digit', hourCycle: 'h23' }).format(date);
}

function safeSegment(value, fallback = 'unknown') {
	const normalized = text(value, fallback).replace(/[^a-zA-Z0-9._-]+/g, '_').slice(0, 120);
	return normalized || fallback;
}

function archivePath(root, bucket, observedAt, filename) {
	return `${root.replace(/\/$/, '')}/${safeSegment(bucket)}/${exchangeDate(observedAt)}/${hourPart(observedAt)}/${filename}`;
}

export function createBaiduPanMarketArchive({ baiduPan, ledger, quantServiceUrl, enabled = false, intervalSeconds = 30, rootPath = DEFAULT_ROOT, fetchImpl = fetch, logger = console }) {
	const baseUrl = text(quantServiceUrl).replace(/\/$/, '');
	const intervalMs = Math.max(10, Math.min(300, Number(intervalSeconds) || 30)) * 1000;
	const archiveRoot = text(rootPath, DEFAULT_ROOT).replace(/\/$/, '') || DEFAULT_ROOT;
	const enabledFlag = Boolean(enabled && baiduPan && ledger?.enqueueBaiduPanArchive && baseUrl);
	let running = false;
	let lastPollAt = null;
	let lastSuccessAt = null;
	let lastError = null;
	let archivedInProcess = 0;
	const directoryCache = new Set();

	async function fetchJson(path) {
		const response = await fetchImpl(`${baseUrl}${path}`, { headers: { accept: 'application/json' }, signal: AbortSignal.timeout(10_000) });
		let body;
		try { body = await response.json(); } catch { throw new Error(`量化服务归档读取返回无效 JSON（HTTP ${response.status}）`); }
		if (!response.ok) throw new Error(`量化服务归档读取失败（HTTP ${response.status}）`);
		return body;
	}

	async function ensureDirectory(path) {
		const normalized = text(path).replace(/\/+$/, '') || '/';
		if (directoryCache.has(normalized)) return;
		const parts = normalized.split('/').filter(Boolean);
		let current = '';
		for (const part of parts) {
			current += `/${part}`;
			if (directoryCache.has(current)) continue;
			const parent = current.slice(0, current.lastIndexOf('/')) || '/';
			let exists = false;
			try {
				const listed = await baiduPan.list({ dir: parent, limit: 1000 });
				exists = (listed.list ?? []).some((item) => item?.path === current && Number(item?.isdir) === 1);
			} catch { /* mkdir below remains the recovery path for protected app dirs */ }
			if (!exists) {
				try { await baiduPan.mkdir(current); }
				catch (error) {
					if (!/31066|already|exist|冲突/i.test(String(error?.message ?? error))) throw error;
				}
			}
			directoryCache.add(current);
		}
	}

	async function enqueueSnapshot(bucket, identity, observedAt, payload) {
		const serialized = JSON.stringify({ schema: 'market-realtime-archive-v1', bucket, observed_at: observedAt, exchange_date: exchangeDate(observedAt), ...payload });
		const bytes = Buffer.byteLength(serialized);
		if (bytes > MAX_SNAPSHOT_BYTES) throw new Error(`实时研究快照超过 ${Math.floor(MAX_SNAPSHOT_BYTES / 1024 / 1024)} MiB 上限`);
		const archiveKey = `market:${bucket}:${identity}`;
		const row = await ledger.enqueueBaiduPanArchive({ archiveKey, bucket, observedAt, exchangeDate: exchangeDate(observedAt), payload: JSON.parse(serialized) });
		return row ? { archiveId: row.archive_id, bucket, bytes, observedAt, payload: serialized } : null;
	}

	async function uploadJob(job) {
		const observedAt = job.observed_at ?? new Date().toISOString();
		const bucket = safeSegment(job.bucket, 'unknown');
		const directory = `${archiveRoot}/${bucket}/${exchangeDate(observedAt)}/${hourPart(observedAt)}`;
		await ensureDirectory(directory);
		const filename = `${safeSegment(job.archive_key, 'snapshot')}.json`;
		const remotePath = `${directory}/${filename}`;
		const content = Buffer.from(JSON.stringify(job.payload ?? {}));
		const result = await baiduPan.uploadReadable({ readable: Readable.from([content]), fileName: filename, size: content.length, remotePath });
		await ledger.completeBaiduPanArchive(job.archive_id, { remotePath: result.path ?? remotePath, remoteFsId: result.fsId ?? null });
		archivedInProcess += 1;
	}

	async function drain() {
		if (!enabledFlag || running) return;
		running = true;
		try {
			const jobs = await ledger.claimBaiduPanArchives({ workerId: 'baidu-pan-market-archive', limit: 16, leaseSeconds: 300 });
			for (const job of jobs) {
				try { await uploadJob(job); }
				catch (error) { await ledger.failBaiduPanArchive(job.archive_id, { errorMessage: String(error?.message ?? error), retryable: true }); logger.warn(`百度网盘研究快照归档失败：${error?.message ?? error}`); }
			}
		} finally { running = false; }
	}

	async function poll() {
		if (!enabledFlag || running) return;
		lastPollAt = new Date().toISOString();
		try {
			const results = await Promise.all(SNAPSHOT_SPECS.map(async (spec) => ({ spec, body: await fetchJson(spec.path) })));
			for (const { spec, body } of results) {
				const identity = text(spec.identity(body));
				if (!identity) continue;
				await enqueueSnapshot(spec.bucket, identity, spec.observedAt(body), { source: spec.source, data: body });
			}
			lastSuccessAt = new Date().toISOString();
			lastError = null;
		} catch (error) {
			lastError = String(error?.message ?? error).slice(0, 420);
			logger.warn(`百度网盘研究快照轮询失败：${lastError}`);
		} finally { await drain(); }
	}

	return {
		enabled: enabledFlag,
		intervalMs,
		poll,
		drain,
		status: async () => ({ enabled: enabledFlag, running, interval_seconds: intervalMs / 1000, last_poll_at: lastPollAt, last_success_at: lastSuccessAt, last_error: lastError, archived_in_process: archivedInProcess, queue: ledger.baiduPanArchiveStatus ? await ledger.baiduPanArchiveStatus() : null, root_path: archiveRoot }),
	};
}

export { archivePath };
