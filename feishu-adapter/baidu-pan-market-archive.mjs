import { Readable } from 'node:stream';

const DEFAULT_ROOT = '/apps/股票paper存储/market-realtime';
const MAX_SNAPSHOT_BYTES = 12 * 1024 * 1024;

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
			const jobs = await ledger.claimBaiduPanArchives({ workerId: 'baidu-pan-market-archive', limit: 4, leaseSeconds: 300 });
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
			const [scan, leader] = await Promise.all([
				fetchJson('/api/v1/intraday/scans/latest?limit=200'),
				fetchJson('/api/v1/research/ten-day-leader-rotation/latest?limit=90'),
			]);
			const scanId = text(scan?.scan?.scan_id);
			if (scanId) await enqueueSnapshot('watchlist', scanId, scan.scan.observed_at, { source: 'quant.intraday.scans.latest', scan });
			const runId = text(leader?.run?.run_id, 'none');
			const batchId = text(leader?.intraday?.latest_batch?.scan_id, text(leader?.intraday?.latest_batch?.observed_at, 'none'));
			if (leader?.run || leader?.intraday?.latest_batch) await enqueueSnapshot('leader-rotation', `${runId}:${batchId}`, leader?.intraday?.latest_batch?.observed_at ?? leader?.run?.updated_at, { source: 'quant.ten_day_leader_rotation.latest', leader });
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
