import * as Lark from '@larksuiteoapi/node-sdk';
import { createHash, randomUUID } from 'node:crypto';
import { readFileSync, mkdirSync, createWriteStream, readdirSync, statSync, existsSync } from 'node:fs';
import { open, unlink, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createServer } from 'node:http';
import { createLedger } from './ledger.mjs';
import { createGroupRelay } from './group-relay.mjs';
import { createFeishuUserOauth } from './feishu-user-oauth.mjs';
import { createFeishuWorkbench } from './feishu-workbench.mjs';
import Busboy from 'busboy';

const required = ['FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'N8N_TEXT_WEBHOOK_URL', 'N8N_MEDIA_PART_WEBHOOK_URL', 'N8N_MEDIA_FINALIZE_WEBHOOK_URL'];
for (const name of required) {
	if (!process.env[name]) throw new Error(`${name} must be configured`);
}

const { FEISHU_APP_ID: appId, FEISHU_APP_SECRET: appSecret } = process.env;
const textWebhookUrl = process.env.N8N_TEXT_WEBHOOK_URL;
const mediaWebhookUrl = process.env.N8N_MEDIA_WEBHOOK_URL;
const mediaPartWebhookUrl = process.env.N8N_MEDIA_PART_WEBHOOK_URL;
const mediaFinalizeWebhookUrl = process.env.N8N_MEDIA_FINALIZE_WEBHOOK_URL;
const mediaStateWebhookUrl = process.env.N8N_MEDIA_STATE_WEBHOOK_URL;
const quantServiceUrl = String(process.env.QUANT_SERVICE_URL ?? '').replace(/\/$/, '');
const quantWriteApiKey = String(process.env.QUANT_WRITE_API_KEY ?? '');
const quantAlertWebhookToken = String(process.env.QUANT_ALERT_WEBHOOK_TOKEN ?? '');
const feishuAlertReceiveId = String(process.env.FEISHU_ALERT_RECEIVE_ID ?? '').trim();
const feishuAlertReceiveIdType = String(process.env.FEISHU_ALERT_RECEIVE_ID_TYPE ?? 'chat_id').trim();
const supportedAlertReceiveIdTypes = new Set(['chat_id', 'open_id', 'user_id', 'union_id']);
if (!supportedAlertReceiveIdTypes.has(feishuAlertReceiveIdType)) {
	throw new Error('FEISHU_ALERT_RECEIVE_ID_TYPE must be chat_id, open_id, user_id, or union_id');
}
const dashboardPort = Number(process.env.DASHBOARD_PORT ?? 3000);
const frontendDist = process.env.FRONTEND_DIST ?? '/app/frontend-dist';
const frontendMode = process.env.FRONTEND_MODE ?? (existsSync(frontendDist) ? 'spa' : 'legacy');
const importTimeZone = process.env.IMPORT_TIME_ZONE ?? 'Asia/Shanghai';
const remoteUploadPartBytes = 8 * 1024 * 1024;
const uploadPartBytes = Number(process.env.UPLOAD_PART_BYTES ?? remoteUploadPartBytes);
if (uploadPartBytes !== remoteUploadPartBytes) {
	throw new Error(`UPLOAD_PART_BYTES must match the remote import chunk limit: ${remoteUploadPartBytes}`);
}
// The SDK's default error logger includes an Axios request object. Suppress it
// so a failed resource download cannot place authorization headers in logs.
const larkLogger = {
	error: () => console.error('Feishu SDK request failed'),
	warn: () => {},
	info: () => {},
	debug: () => {},
	trace: () => {},
};
const larkClient = new Lark.Client({ appId, appSecret, domain: Lark.Domain.Feishu, logger: larkLogger });
const recentEvents = [];
const eventStreams = new Set();
const maxRecentEvents = 200;
const relayDrafts = new Map();
const feishuDedupeTtlMs = Number(process.env.FEISHU_DEDUPE_TTL_MS ?? 10 * 60 * 1000);
if (!Number.isFinite(feishuDedupeTtlMs) || feishuDedupeTtlMs < 0) {
	throw new Error('FEISHU_DEDUPE_TTL_MS must be a non-negative number');
}
const feishuEventPromises = new Map();
const sourceRegistry = JSON.parse(readFileSync('/app/source-registry.json', 'utf8'));
const ingestionStorageDir = process.env.INGESTION_STORAGE_DIR ?? '/var/lib/adapter-ingestion';
mkdirSync(ingestionStorageDir, { recursive: true });
const ledger = createLedger(process.env.INGESTION_DATABASE_URL || undefined);
await ledger.init(sourceRegistry);
const feishuUserOauth = createFeishuUserOauth({
	appId, appSecret, ledger, redirectUri: String(process.env.FEISHU_USER_OAUTH_REDIRECT_URI ?? 'http://localhost:8080/callback').trim(),
});
const groupRelayIntervalSeconds = Number(process.env.FEISHU_GROUP_RELAY_INTERVAL_SECONDS ?? 10);
if (!Number.isFinite(groupRelayIntervalSeconds) || groupRelayIntervalSeconds < 10 || groupRelayIntervalSeconds > 30) {
	throw new Error('FEISHU_GROUP_RELAY_INTERVAL_SECONDS must be between 10 and 30');
}
const groupRelayHistoryLookbackSeconds = Number(process.env.FEISHU_GROUP_RELAY_HISTORY_LOOKBACK_SECONDS ?? 300);
if (!Number.isFinite(groupRelayHistoryLookbackSeconds) || groupRelayHistoryLookbackSeconds < 60 || groupRelayHistoryLookbackSeconds > 3600) {
	throw new Error('FEISHU_GROUP_RELAY_HISTORY_LOOKBACK_SECONDS must be between 60 and 3600');
}
const groupRelayBootstrapMode = String(process.env.FEISHU_GROUP_RELAY_BOOTSTRAP_MODE ?? 'skip_existing').trim();
if (!['skip_existing', 'forward_existing'].includes(groupRelayBootstrapMode)) {
	throw new Error('FEISHU_GROUP_RELAY_BOOTSTRAP_MODE must be skip_existing or forward_existing');
}
const groupRelayConfig = {
	enabled: String(process.env.FEISHU_GROUP_RELAY_ENABLED ?? 'true').toLowerCase() !== 'false',
	targetChatId: String(process.env.FEISHU_GROUP_RELAY_TARGET_CHAT_ID ?? '').trim(),
	intervalSeconds: groupRelayIntervalSeconds,
	historyLookbackSeconds: groupRelayHistoryLookbackSeconds,
	overlapSeconds: Math.min(120, Math.max(30, Math.floor(groupRelayIntervalSeconds * 3))),
	reconcileEverySeconds: Math.max(3600, Number(process.env.FEISHU_GROUP_RELAY_RECONCILE_SECONDS ?? 21_600)),
	reconcileLookbackSeconds: Math.max(300, Number(process.env.FEISHU_GROUP_RELAY_RECONCILE_LOOKBACK_SECONDS ?? 86_400)),
	bootstrapMode: groupRelayBootstrapMode,
	sources: [
		{ key: 'anqiang', tag: 'anqiang', chatId: String(process.env.FEISHU_GROUP_RELAY_ANQIANG_CHAT_ID ?? 'oc_1de6464db12c43bf60985f7131e6334a').trim(), chatName: String(process.env.FEISHU_GROUP_RELAY_ANQIANG_CHAT_NAME ?? '马安强 (1)').trim() },
		{ key: 'liwei', tag: 'liwei', chatId: String(process.env.FEISHU_GROUP_RELAY_LIWEI_CHAT_ID ?? 'oc_4ed9fdb72a152bc921b2d34bd4a1df14').trim(), chatName: String(process.env.FEISHU_GROUP_RELAY_LIWEI_CHAT_NAME ?? '消息更新群').trim() },
		{ key: 'quanneng', tag: 'quanneng', chatId: String(process.env.FEISHU_GROUP_RELAY_QUANNENG_CHAT_ID ?? 'oc_d6a89890c3a62517116a2c63f015e0a0').trim(), chatName: String(process.env.FEISHU_GROUP_RELAY_QUANNENG_CHAT_NAME ?? '新野人哥会员群【禁言】').trim() },
	],
};
await ledger.initializeRelayRoutes(groupRelayConfig.sources);
const feishuWorkbench = createFeishuWorkbench({
	appId, appSecret, larkClient, ledger, userRequest: feishuUserOauth.userRequest,
	config: {
		targetChatId: groupRelayConfig.targetChatId,
		publicBaseUrl: String(process.env.FEISHU_WORKBENCH_PUBLIC_BASE_URL ?? '').trim(),
		driveFolderToken: String(process.env.FEISHU_WORKBENCH_DRIVE_FOLDER_TOKEN ?? '').trim(),
		wikiSpaceId: String(process.env.FEISHU_WORKBENCH_WIKI_SPACE_ID ?? '').trim(),
		tasklistGuid: String(process.env.FEISHU_WORKBENCH_TASKLIST_GUID ?? '').trim(),
		baseAppToken: String(process.env.FEISHU_WORKBENCH_BASE_APP_TOKEN ?? '').trim(),
		baseTableId: String(process.env.FEISHU_WORKBENCH_BASE_TABLE_ID ?? '').trim(),
		calendarId: String(process.env.FEISHU_WORKBENCH_CALENDAR_ID ?? '').trim(),
		approvalCode: String(process.env.FEISHU_WORKBENCH_APPROVAL_CODE ?? '').trim(),
		ailyAppId: String(process.env.FEISHU_WORKBENCH_AILY_APP_ID ?? '').trim(),
	},
});
const groupRelay = createGroupRelay({
	larkClient, sourceApi: feishuUserOauth.sourceApi, ledger, workbench: feishuWorkbench,
	config: { ...groupRelayConfig, sourcesProvider: () => ledger.relayRoutes() },
});
setInterval(() => { void groupRelay.tick(); }, groupRelayIntervalSeconds * 1000).unref();
void groupRelay.tick();
const reconcileSeconds = Math.max(30, Number(process.env.INGESTION_RECONCILE_SECONDS ?? 300));
const ledgerRetentionDays = Math.max(7, Number(process.env.INGESTION_LEDGER_RETENTION_DAYS ?? 90));
let lastLedgerPruneAt = 0;
async function runLocalAnalysisQueue() {
	try {
		for (const task of await ledger.pendingAnalysis()) {
			// The archive at 47 is the sole analyst-opinion source.  Local Feishu
			// media/text ingestion remains durable transport only: it must not run
			// a second OCR/ASR/text-opinion pipeline or create competing claims.
			await ledger.completeAnalysis(task.analysis_id, {
				kind: 'remote-archive-source', remote_batch_id: task.remote_batch_id,
				message: '等待远端市场复盘档案完成解析；量化观点仅由远端报告同步工作流写入。',
				generated_at: new Date().toISOString(), quant_service_configured: Boolean(quantServiceUrl),
			});
		}
	} catch (error) { console.error(`本地分析队列失败：${error instanceof Error ? error.message : String(error)}`); }
}
async function runRetryQueue() {
	for (const queued of await ledger.retryQueue()) {
		if (!await ledger.markRetryRunning(queued.job_id)) continue;
		try {
			const payload = queued.payload ?? {};
			const originalResources = Array.isArray(payload.resources) ? payload.resources : [];
			const replayResources = (queued.resources ?? []).map(({ asset, parts }) => ({
				asset_id: asset.asset_id, property: `replay_${asset.ordinal}`, filename: filenameForMediaType(asset.filename, asset.media_type), media_type: asset.media_type,
				declared_bytes: Number(asset.declared_bytes), content_sha256: asset.content_sha256,
				path: asset.storage_path, remote_upload_id: asset.remote_upload_id,
				last_modified: Number(originalResources[Number(asset.ordinal)]?.last_modified ?? Date.now()), part_size: uploadPartBytes,
				part_count: parts.length, parts: parts.map((part) => ({ part_index: Number(part.part_index), property: `replay_${asset.ordinal}_part_${part.part_index}`, bytes: Number(part.bytes), sha256: part.sha256, uploaded: Boolean(part.uploaded), remote_status: part.remote_status })),
			}));
			if (replayResources.some((resource) => resource.path && !existsSync(resource.path))) throw new Error('本地重试文件已被清理，无法恢复上传');
			await hydrateRemotePartState(replayResources);
			await forwardToN8n(payload.event, {
				resources: replayResources, messageText: payload.message_text, sourceLabel: payload.source_label,
				replayJobId: queued.job_id, source: payload.source, remoteBatchId: queued.remote_batch_id,
				receivedAt: payload.receivedAt,
				importContent: { content: payload.import_content ?? '', content_date: payload.content_date, content_time: payload.content_time },
			});
			} catch (error) {
				// A workflow can persist the remote status before its webhook returns 500.
				// Keep that diagnostic instead of obscuring it as a local retry failure.
				const current = await ledger.getJob(queued.job_id);
				if (!current || !['failed', 'retryable_failed'].includes(current.status)) {
					await ledger.updateJob(queued.job_id, { status: 'retryable_failed', stage: 'retry_failed', error_class: 'local_retry', error_message: error instanceof Error ? error.message : String(error) });
				}
			}
	}
}
async function cleanupUnreferencedMedia() {
	try {
		const referenced = await ledger.referencedStoragePaths();
		for (const name of readdirSync(ingestionStorageDir)) {
			const path = join(ingestionStorageDir, name);
			if (!referenced.has(path)) { try { const stat = statSync(path); if (Date.now() - stat.mtimeMs > reconcileSeconds * 1000) await unlink(path); } catch {} }
		}
	} catch (error) { console.error(`本地媒体对账失败：${error instanceof Error ? error.message : String(error)}`); }
}
async function reconcileNow() {
	await runRetryQueue();
	await runLocalAnalysisQueue();
	await cleanupUnreferencedMedia();
	if (Date.now() - lastLedgerPruneAt >= 60 * 60 * 1000) {
		await ledger.pruneHistory(ledgerRetentionDays);
		lastLedgerPruneAt = Date.now();
	}
	const [jobs, analysis] = await Promise.all([ledger.pendingJobs(), ledger.pendingAnalysis()]);
	return { pending_jobs: jobs.length, pending_analysis: analysis.length, reconciled_at: new Date().toISOString() };
}
setInterval(() => {
	void reconcileNow().catch((error) => console.error(`统一对账失败：${error instanceof Error ? error.message : String(error)}`));
}, reconcileSeconds * 1000).unref();
setInterval(() => { void cleanupUnreferencedMedia(); }, reconcileSeconds * 1000).unref();
setInterval(runLocalAnalysisQueue, Math.max(30, Number(process.env.ANALYSIS_POLL_SECONDS ?? 60) * 1000)).unref();
setInterval(() => { void runRetryQueue(); }, 30_000).unref();
void reconcileNow().catch((error) => console.error(`启动对账失败：${error instanceof Error ? error.message : String(error)}`));
const sourceRoutes = new Map((sourceRegistry.routes ?? []).map((route) => [String(route.tag).toLowerCase(), route]));
const supportedMediaTypes = new Set([
	'image/jpeg', 'image/png', 'image/webp', 'audio/mp4', 'audio/x-m4a',
	'audio/mpeg', 'audio/wav', 'audio/x-wav', 'video/mp4', 'video/quicktime',
]);

function parseContent(content) {
	try {
		return JSON.parse(content ?? '{}');
	} catch {
		return { raw: content };
	}
}

function resolveRoute(tag) {
	const normalized = String(tag ?? '').trim().toLowerCase();
	const route = sourceRoutes.get(normalized);
	if (!route || route.enabled === false) throw new Error(`不支持的来源标签：#${normalized || '未填写'}`);
	return route;
}

function routeFromMessageText(messageText) {
	const match = String(messageText ?? '').match(/^#([a-z0-9-]+)/i);
	return resolveRoute(match?.[1]);
}

const relayRouteOptions = [...sourceRoutes.values()]
	.filter((route) => route.enabled !== false)
	.map((route) => `<option value="${route.tag}">#${route.tag} · ${route.label}</option>`)
	.join('');

function extractPostPayload(content) {
	const blocks = Array.isArray(content?.content_v2) ? content.content_v2 : content?.content;
	if (!Array.isArray(blocks)) return { text: '', resources: [] };
	const text = [];
	const resources = [];
	for (const line of blocks) {
		for (const element of Array.isArray(line) ? line : []) {
			if (element?.tag === 'text' && typeof element.text === 'string') text.push(element.text);
			if (element?.tag === 'img' && element.image_key) {
				resources.push({ key: element.image_key, resource_type: 'image' });
			}
			if ((element?.tag === 'media' || element?.tag === 'audio') && element.file_key) {
				resources.push({ key: element.file_key, resource_type: 'file' });
			}
		}
	}
	return { text: text.join(''), resources };
}

function extractMessagePayload(message) {
	const content = parseContent(message?.content);
	if (message?.message_type === 'post') return extractPostPayload(content);
	const resources = [];
	if (content.image_key) resources.push({ key: content.image_key, resource_type: 'image' });
	if (content.file_key) resources.push({ key: content.file_key, resource_type: 'file' });
	return { text: content.text ?? content.raw ?? null, resources };
}

function summarizeEvent(data) {
	const message = data.message ?? {};
	const payload = extractMessagePayload(message);
	return {
		event_id: data.event_id ?? null,
		event_type: data.event_type ?? 'im.message.receive_v1',
		received_at: new Date().toISOString(),
		message_id: message.message_id ?? null,
		chat_id: message.chat_id ?? null,
		chat_type: message.chat_type ?? null,
		message_type: message.message_type ?? null,
		source: data.source ?? (data.event_type === 'manual.relay' ? 'manual-relay' : 'feishu'),
		source_label: data.source_label ?? null,
		text: payload.text,
		image_key: payload.resources.find((resource) => resource.resource_type === 'image')?.key ?? null,
		file_key: payload.resources.find((resource) => resource.resource_type === 'file')?.key ?? null,
		file_name: null,
		duration: null,
		sender_open_id: data.sender?.sender_id?.open_id ?? null,
		sender_type: data.sender?.sender_type ?? null,
		ingress_status: '已接收',
		n8n_status: '等待转发',
		target_status: null,
		target_batch_id: null,
		n8n_error: null,
		raw: data,
	};
}

function contentTypeFromBytes(bytes, fallback) {
	if (fallback && fallback !== 'application/octet-stream') return fallback;
	if (bytes.subarray(0, 3).equals(Buffer.from([0xff, 0xd8, 0xff]))) return 'image/jpeg';
	if (bytes.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return 'image/png';
	if (bytes.subarray(0, 4).toString('ascii') === 'RIFF' && bytes.subarray(8, 12).toString('ascii') === 'WEBP') return 'image/webp';
	if (bytes.subarray(0, 3).toString('ascii') === 'ID3') return 'audio/mpeg';
	if (bytes.subarray(0, 4).toString('ascii') === 'RIFF' && bytes.subarray(8, 12).toString('ascii') === 'WAVE') return 'audio/wav';
	if (bytes.subarray(4, 8).toString('ascii') === 'ftyp') {
		if (bytes.subarray(8, 12).toString('ascii') === 'qt  ') return 'video/quicktime';
		const fallbackName = String(fallback ?? '').toLowerCase();
		if (fallbackName.includes('audio')) return 'audio/mp4';
		return 'video/mp4';
	}
	return fallback ?? 'application/octet-stream';
}

function extensionFor(mediaType) {
	return ({
		'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp',
		'audio/mp4': 'm4a', 'audio/x-m4a': 'm4a', 'audio/mpeg': 'mp3',
		'audio/wav': 'wav', 'audio/x-wav': 'wav', 'video/mp4': 'mp4', 'video/quicktime': 'mov',
	})[mediaType] ?? 'bin';
}

function filenameForMediaType(filename, mediaType) {
	const extension = extensionFor(mediaType);
	const safeName = String(filename ?? '').replace(/[^\w.\-()\u4e00-\u9fff]+/g, '_').slice(0, 255);
	const stem = safeName.replace(/\.[^.]*$/, '') || 'attachment';
	return `${stem}.${extension}`;
}

function partManifest(bytes, property) {
	const parts = [];
	for (let offset = 0; offset < bytes.length; offset += uploadPartBytes) {
		const part = bytes.subarray(offset, Math.min(offset + uploadPartBytes, bytes.length));
		const index = parts.length;
		parts.push({
			property: `${property}_part_${index}`,
			bytes: part.length,
			sha256: createHash('sha256').update(part).digest('hex'),
		});
	}
	return { part_size: uploadPartBytes, part_count: parts.length, parts };
}

async function persistReadableAsset(readable, property, fallbackName, fallbackType, lastModified) {
	const path = join(ingestionStorageDir, `${randomUUID()}-${property}.bin`);
	const writer = createWriteStream(path, { flags: 'wx' });
	const fullHash = createHash('sha256');
	const parts = [];
	let pending = Buffer.alloc(0);
	let total = 0;
	let firstBytes = Buffer.alloc(0);
	try {
		for await (const chunk of readable) {
			const bytes = Buffer.from(chunk);
			if (firstBytes.length < 16) firstBytes = Buffer.concat([firstBytes, bytes.subarray(0, 16 - firstBytes.length)]);
			fullHash.update(bytes); total += bytes.length;
			if (total > Number(process.env.INGESTION_MAX_FILE_BYTES ?? 524_288_000)) throw new Error('媒体超过 500 MB 上限');
			pending = pending.length ? Buffer.concat([pending, bytes]) : bytes;
			while (pending.length >= uploadPartBytes) {
				const part = pending.subarray(0, uploadPartBytes); pending = pending.subarray(uploadPartBytes);
				parts.push({ property: `${property}_part_${parts.length}`, bytes: part.length, sha256: createHash('sha256').update(part).digest('hex') });
			}
			if (!writer.write(bytes)) await new Promise((resolve, reject) => { writer.once('drain', resolve); writer.once('error', reject); });
		}
		if (pending.length) parts.push({ property: `${property}_part_${parts.length}`, bytes: pending.length, sha256: createHash('sha256').update(pending).digest('hex') });
		await new Promise((resolve, reject) => writer.end((error) => error ? reject(error) : resolve()));
		if (!total) throw new Error('飞书资源下载为空');
		const mediaType = contentTypeFromBytes(firstBytes, fallbackType);
		if (!supportedMediaTypes.has(mediaType)) throw new Error(`目标导入 API 不支持媒体类型：${mediaType}`);
		return { property, filename: filenameForMediaType(fallbackName, mediaType), media_type: mediaType, declared_bytes: total, content_sha256: fullHash.digest('hex'), part_size: uploadPartBytes, part_count: parts.length, parts, last_modified: lastModified, path };
	} catch (error) { writer.destroy(); await unlink(path).catch(() => {}); throw error; }
}

async function readAssetPart(resource, offset, bytes) {
	const file = await open(resource.path, 'r');
	try { const output = Buffer.allocUnsafe(bytes); const { bytesRead } = await file.read(output, 0, bytes, offset); return output.subarray(0, bytesRead); }
	finally { await file.close(); }
}

async function fetchWithBackoff(url, options, { maxAttempts = 4, baseDelayMs = 500 } = {}) {
	let lastError; let lastResponse;
	for (let attempt = 1; attempt <= maxAttempts; attempt++) {
		try {
			const response = await fetch(url, options);
			if (response.ok || (response.status >= 400 && response.status < 500)) return response;
			lastResponse = response;
			lastError = new Error(`HTTP ${response.status}`);
		} catch (error) { lastError = error; }
		if (attempt < maxAttempts) await new Promise((resolve) => setTimeout(resolve, baseDelayMs * 2 ** (attempt - 1) + Math.floor(Math.random() * 200)));
	}
	if (lastResponse) return lastResponse;
	throw lastError ?? new Error('request failed');
}

async function hydrateRemotePartState(resources) {
	if (!mediaStateWebhookUrl) return;
	for (const resource of resources) {
		if (!resource.remote_upload_id) continue;
		try {
			const response = await fetchWithBackoff(mediaStateWebhookUrl, {
				method: 'POST', headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ upload_id: resource.remote_upload_id }), signal: AbortSignal.timeout(130_000),
			}, { maxAttempts: 1 });
			if (!response.ok) continue;
			const state = await response.json();
			const received = state?.upload?.received_parts ?? state?.received_parts ?? state?.parts_received ?? [];
			if (!Array.isArray(received)) continue;
			const indexes = received.map(Number).filter((index) => Number.isInteger(index) && index >= 0);
			if (!indexes.length) continue;
			for (const part of resource.parts) if (indexes.includes(Number(part.part_index))) part.uploaded = true;
			if (resource.asset_id) await ledger.recordRemoteParts(resource.asset_id, indexes);
		} catch (error) {
			console.warn(`无法读取远端上传会话 ${resource.remote_upload_id}：${error instanceof Error ? error.message : String(error)}`);
		}
	}
}

function filenameFromHeaders(headers, fallback) {
	const value = headers?.['content-disposition'] ?? headers?.['Content-Disposition'];
	const match = typeof value === 'string' && value.match(/filename\*?=(?:UTF-8''|\")?([^;\"]+)/i);
	return match ? decodeURIComponent(match[1].replace(/\"/g, '')).replace(/[^\w.-]+/g, '_') : fallback;
}

function formatImportDateTime(instant) {
	const parts = new Intl.DateTimeFormat('en-CA', {
		timeZone: importTimeZone,
		year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
	}).formatToParts(new Date(instant));
	const values = Object.fromEntries(parts.filter((part) => part.type !== 'literal').map((part) => [part.type, part.value]));
	return { content_date: `${values.year}-${values.month}-${values.day}`, content_time: `${values.hour}:${values.minute}` };
}

function isValidDateTime(date, time) {
	if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !/^\d{2}:\d{2}$/.test(time)) return false;
	const [year, month, day] = date.split('-').map(Number);
	const [hour, minute] = time.split(':').map(Number);
	const value = new Date(Date.UTC(year, month - 1, day, hour, minute));
	return value.getUTCFullYear() === year && value.getUTCMonth() === month - 1 && value.getUTCDate() === day &&
		value.getUTCHours() === hour && value.getUTCMinutes() === minute;
}

function extractImportContent(messageText, receivedAt) {
	const routeTag = messageText.match(/^#([a-z0-9-]+)\s*(?:\r?\n)?/i);
	const defaultDateTime = formatImportDateTime(receivedAt);
	if (!routeTag) return { content: '', ...defaultDateTime };
	let content = messageText.slice(routeTag[0].length).trim();
	const override = content.match(/^@(\d{4}-\d{2}-\d{2})[ \t]+(\d{2}:\d{2})(?:[ \t]*(?:\r?\n)?)/);
	if (!override) return { content, ...defaultDateTime };
	if (!isValidDateTime(override[1], override[2])) {
		throw new Error('指定时间无效，请使用 @YYYY-MM-DD HH:mm，例如 @2026-07-31 14:30');
	}
	return { content: content.slice(override[0].length).trim(), content_date: override[1], content_time: override[2] };
}

async function downloadMedia(data) {
	const message = data.message ?? {};
	const { resources } = extractMessagePayload(message);
	if (!resources.length) return [];

	return Promise.all(resources.map(async (resource, index) => {
		let response;
		try {
			// Official Feishu message-resource API. It authorizes against the app's
			// im:message:readonly (or broader) application scope.
			response = await larkClient.im.v1.messageResource.get({
				path: { message_id: message.message_id, file_key: resource.key },
				params: { type: resource.resource_type },
			});
		} catch (error) {
			if (error?.response?.status === 400) {
				throw new Error('飞书媒体下载被拒绝：请在开放平台申请并发布 im:message:readonly 权限');
			}
			throw new Error(`飞书媒体下载失败（HTTP ${error?.response?.status ?? '未知'}）`);
		}
		const headerType = String(response.headers?.['content-type'] ?? '').split(';')[0];
		const fallbackName = filenameFromHeaders(response.headers, `${resource.resource_type}-${resource.key}.bin`);
		return persistReadableAsset(response.getReadableStream(), `media_${index}`, fallbackName, headerType, Number(message.create_time ?? Date.now()));
	}));
}

function manualResource(file, index) {
	if (!file || typeof file !== 'object' || typeof file.data_base64 !== 'string') {
		throw new Error('手动投递的媒体格式无效');
	}
	const bytes = Buffer.from(file.data_base64, 'base64');
	if (!bytes.length || bytes.length > 12 * 1024 * 1024) throw new Error('单个媒体应介于 1 B 和 12 MB 之间');
	const mediaType = contentTypeFromBytes(bytes, String(file.media_type ?? '').split(';')[0]);
	if (!supportedMediaTypes.has(mediaType)) throw new Error(`不支持的手动投递媒体类型：${mediaType}`);
	const fallbackName = `manual-${index + 1}.${extensionFor(mediaType)}`;
	const filename = filenameForMediaType(String(file.filename ?? fallbackName), mediaType);
	return {
		property: `media_${index}`,
		filename,
		media_type: mediaType,
		declared_bytes: bytes.length,
		content_sha256: createHash('sha256').update(bytes).digest('hex'),
		...partManifest(bytes, `media_${index}`),
		last_modified: Date.now(),
		data: bytes,
	};
}

function sendSse(response, event, payload) {
	response.write(`event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`);
}

function broadcastSnapshot() {
	for (const response of eventStreams) sendSse(response, 'snapshot', recentEvents);
}

function addEvent(data) {
	const event = summarizeEvent(data);
	recentEvents.unshift(event);
	if (recentEvents.length > maxRecentEvents) recentEvents.pop();
	for (const response of eventStreams) sendSse(response, 'message', event);
	return event;
}

function updateEvent(eventId, patch) {
	const event = recentEvents.find((entry) => entry.event_id === eventId);
	if (!event) return;
	Object.assign(event, patch);
	broadcastSnapshot();
}

function summarizeN8nResult(payload) {
	const batch = payload?.batch ?? null;
	if (!batch) return { target_status: 'n8n 已完成' };
	const percent = Number.isFinite(batch.percentage) ? `（${batch.percentage}%）` : '';
	return {
		target_batch_id: batch.id ?? null,
		target_status: `已提交至目标服务：${batch.state ?? 'unknown'}${percent}`,
	};
}

const dashboardHtml = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Feishu Bot Monitor</title>
  <style>
    :root { color-scheme: dark; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0b1020; color: #edf2ff; }
    body { margin: 0; } main { max-width: 1080px; margin: 0 auto; padding: 36px 24px 64px; }
    header { display: flex; align-items: end; justify-content: space-between; gap: 20px; margin-bottom: 24px; }
    h1 { margin: 0; font-size: 28px; } p { color: #aab7d4; margin: 8px 0 0; }
    #status { color: #65e6a6; font-size: 14px; white-space: nowrap; } #count { color: #aab7d4; font-size: 14px; margin: 16px 0; }
    #events { display: grid; gap: 14px; } article { background: #131b31; border: 1px solid #263452; border-radius: 12px; padding: 18px; }
    .top { display: flex; justify-content: space-between; gap: 16px; } .kind { color: #79aaff; font-weight: 700; }
    .states { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 2px; } .state { border-radius: 999px; padding: 4px 9px; font-size: 13px; background: #203157; color: #c7d8ff; } .state.ok { background: #173f32; color: #82efb7; } .state.fail { background: #542738; color: #ffb6c5; }
    time { color: #8fa0c3; font-size: 13px; } .text { white-space: pre-wrap; line-height: 1.55; margin: 14px 0; }
    dl { display: grid; grid-template-columns: 140px 1fr; gap: 7px 14px; margin: 14px 0 0; font-size: 14px; }
    dt { color: #8fa0c3; } dd { margin: 0; overflow-wrap: anywhere; } details { margin-top: 14px; } pre { max-height: 360px; overflow: auto; background: #0b1020; border-radius: 8px; padding: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
    .empty { border: 1px dashed #455474; border-radius: 12px; padding: 32px; text-align: center; color: #8fa0c3; }
  </style>
</head>
<body><main>
  <header><div><h1>飞书机器人消息监控</h1><p>本机实时视图，保留当前适配器进程接收的最近 200 条事件。</p></div><div id="status">连接中…</div></header>
  <div id="count"></div><section id="events"><div class="empty">等待飞书消息…</div></section>
</main>
<script>
  const events = document.querySelector('#events');
  const count = document.querySelector('#count');
  const status = document.querySelector('#status');
  let total = 0;
  const metadata = [['event_type','事件'],['source','本机入口'],['source_label','来源备注'],['message_id','消息 ID'],['chat_id','群 ID'],['chat_type','会话类型'],['sender_open_id','发送者 Open ID'],['sender_type','发送者类型'],['target_batch_id','目标批次 ID'],['n8n_error','n8n 错误'],['image_key','图片 Key'],['file_key','文件 Key'],['file_name','文件名'],['duration','时长(ms)']];
  function field(parent, label, value) { if (value === null || value === undefined) return; const dt=document.createElement('dt'); dt.textContent=label; const dd=document.createElement('dd'); dd.textContent=value; parent.append(dt,dd); }
  function state(text, kind='') { const badge=document.createElement('span'); badge.className='state '+kind; badge.textContent=text; return badge; }
  function card(entry, prepend=false) {
    const article=document.createElement('article'); const top=document.createElement('div'); top.className='top';
    const kind=document.createElement('div'); kind.className='kind'; kind.textContent=entry.message_type || entry.event_type;
    const time=document.createElement('time'); time.textContent=new Date(entry.received_at).toLocaleString(); top.append(kind,time); article.append(top);
    const states=document.createElement('div'); states.className='states';
    states.append(state('飞书：'+(entry.ingress_status || '已接收'),'ok'));
    const n8nKind=entry.n8n_status === '已完成' ? 'ok' : entry.n8n_status === '失败' ? 'fail' : '';
    states.append(state('n8n：'+(entry.n8n_status || '未知'),n8nKind));
    if (entry.target_status) states.append(state('目标：'+entry.target_status, entry.n8n_status === '已完成' ? 'ok' : ''));
    article.append(states);
    if (entry.text) { const text=document.createElement('div'); text.className='text'; text.textContent=entry.text; article.append(text); }
    const dl=document.createElement('dl'); metadata.forEach(([key,label])=>field(dl,label,entry[key])); article.append(dl);
    const details=document.createElement('details'); const summary=document.createElement('summary'); summary.textContent='查看原始事件 JSON'; const pre=document.createElement('pre'); pre.textContent=JSON.stringify(entry.raw,null,2); details.append(summary,pre); article.append(details);
    const empty=events.querySelector('.empty'); if(empty) empty.remove(); if(prepend) events.prepend(article); else events.append(article);
  }
  function refreshCount(){ count.textContent='当前会话已接收 '+total+' 条事件'; }
  const source=new EventSource('/events');
  source.addEventListener('snapshot', e=>{ const data=JSON.parse(e.data); events.replaceChildren(); total=data.length; data.forEach(item=>card(item)); if (!data.length) events.innerHTML='<div class="empty">等待飞书消息…</div>'; refreshCount(); });
  source.addEventListener('message', e=>{ card(JSON.parse(e.data),true); total++; refreshCount(); });
  source.onopen=()=>status.textContent='实时连接已建立';
  source.onerror=()=>status.textContent='连接断开，正在重试…';
</script></body></html>`;

const relayHtml = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>本机消息投递台</title><style>
  :root{color-scheme:dark;font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0b1020;color:#edf2ff}body{margin:0}main{max-width:820px;margin:0 auto;padding:36px 24px 64px}header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:26px}h1{margin:0;font-size:29px}p{color:#aab7d4;line-height:1.6}a{color:#8eb3ff}form{background:#131b31;border:1px solid #263452;border-radius:14px;padding:22px;display:grid;gap:18px}label{display:grid;gap:8px;color:#cbd7f2;font-size:14px}select,input,textarea,button{font:inherit;border-radius:9px;border:1px solid #405276;background:#0b1020;color:#edf2ff;padding:10px 12px}textarea{min-height:190px;resize:vertical;line-height:1.55}.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}.drop{border:1px dashed #5272ad;border-radius:10px;padding:20px;text-align:center;color:#aab7d4}.drop.drag{background:#17274a;border-color:#8eb3ff}.files{margin:0;padding-left:20px;color:#cbd7f2;font-size:14px}.files:empty{display:none}button{cursor:pointer;background:#3268c6;border:0;font-weight:700}button:disabled{opacity:.65;cursor:wait}#result{min-height:22px;color:#aab7d4}.ok{color:#82efb7}.bad{color:#ffb6c5}.hint{font-size:13px;color:#8fa0c3;margin:0}.tag{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
</style></head><body><main>
<header><div><h1>本机消息投递台</h1><p>从飞书、微信或网页复制文字；拖入或粘贴图片/音频后直接进入市场复盘工作流。</p></div><a href="/">查看处理监控</a></header>
<form id="relay"><div class="row"><label>路由标签<select id="tag">${relayRouteOptions}</select></label><label>来源备注（可选）<input id="source" maxlength="120" placeholder="如：微信个人群 / 飞书群 A" /></label></div>
<div class="row"><label>指定日期（可选）<input id="date" type="date" /></label><label>指定时间（可选）<input id="time" type="time" /></label></div>
<label>正文<textarea id="text" placeholder="粘贴消息正文。若不指定日期和时间，按本机收到内容时的北京时间记录。"></textarea><button id="fillClipboard" type="button">填入当前剪贴板文字</button></label>
<div id="drop" class="drop" tabindex="0">拖入图片、音频或视频，或在此页面直接粘贴媒体。<br /><span class="hint">支持多文件；单个媒体受本地入口大小限制。</span><br /><input id="file" type="file" accept="image/jpeg,image/png,image/webp,audio/mp4,audio/x-m4a,audio/mpeg,audio/wav,audio/x-wav,video/mp4,video/quicktime" multiple /></div><ul id="files" class="files"></ul>
<button id="submit" type="submit">投递到市场复盘</button><div id="result" role="status"></div>
</form></main><script>
const staged=[]; const files=document.querySelector('#files'); const drop=document.querySelector('#drop'); const result=document.querySelector('#result'); const submit=document.querySelector('#submit');
async function fillClipboard(){try{const text=await navigator.clipboard.readText();if(!text.trim())throw new Error('剪贴板没有文字');document.querySelector('#text').value=text;result.className='ok';result.textContent='已填入剪贴板文字。';}catch(err){result.className='bad';result.textContent='无法读取剪贴板：'+(err.message||err);}}
document.querySelector('#fillClipboard').addEventListener('click',fillClipboard);
const draftId=new URLSearchParams(location.search).get('draft');if(draftId){fetch('/relay-draft/'+encodeURIComponent(draftId),{cache:'no-store'}).then(r=>r.json().then(b=>({r,b}))).then(({r,b})=>{if(!r.ok)throw new Error(b.message);document.querySelector('#text').value=b.text;result.className='ok';result.textContent='已从快捷键填入剪贴板文字。';}).catch(err=>{result.className='bad';result.textContent=err.message||String(err);});}
function render(){files.replaceChildren(...staged.map((f,i)=>{const li=document.createElement('li');li.textContent=(i+1)+'. '+f.name+' · '+Math.ceil(f.size/1024)+' KB';return li;}));}
function add(list){for(const f of list){if(!staged.some(x=>x.name===f.name&&x.size===f.size&&x.lastModified===f.lastModified)) staged.push(f);}render();}
document.querySelector('#file').addEventListener('change',e=>add(e.target.files));
for(const type of ['dragenter','dragover']) drop.addEventListener(type,e=>{e.preventDefault();drop.classList.add('drag')});
for(const type of ['dragleave','drop']) drop.addEventListener(type,e=>{e.preventDefault();drop.classList.remove('drag')});
drop.addEventListener('drop',e=>add(e.dataTransfer.files));
window.addEventListener('paste',e=>{const pasted=[...e.clipboardData.items].filter(x=>x.kind==='file').map(x=>x.getAsFile()).filter(Boolean);if(pasted.length){e.preventDefault();add(pasted);result.textContent='已加入 '+pasted.length+' 个剪贴板媒体。';}});
document.querySelector('#relay').addEventListener('submit',async e=>{e.preventDefault();result.className='';const tag=document.querySelector('#tag').value;const date=document.querySelector('#date').value;const time=document.querySelector('#time').value;if((date&&!time)||(!date&&time)){result.className='bad';result.textContent='指定时间时请同时填写日期和时间。';return;}const text=document.querySelector('#text').value.trim();if(!text&&!staged.length){result.className='bad';result.textContent='请至少填写正文或加入一个媒体。';return;}submit.disabled=true;submit.textContent='投递中…';try{const form=new FormData();form.append('tag',tag);form.append('text',text);form.append('source_label',document.querySelector('#source').value.trim());if(date)form.append('content_date',date);if(time)form.append('content_time',time);for(const file of staged)form.append('media',file,file.name);const response=await fetch('/manual-relay',{method:'POST',body:form});const body=await response.json();if(!response.ok)throw new Error(body.message||'投递失败');result.className='ok';result.textContent='已接收：'+body.message_id+'；n8n 正在处理。';document.querySelector('#text').value='';document.querySelector('#file').value='';staged.splice(0);render();}catch(err){result.className='bad';result.textContent=err.message||String(err);}finally{submit.disabled=false;submit.textContent='投递到市场复盘';}});
</script></body></html>`;

function readJsonBody(request, limit = 18 * 1024 * 1024) {
	return new Promise((resolve, reject) => {
		const chunks = [];
		let size = 0;
		request.on('data', (chunk) => {
			size += chunk.length;
			if (size > limit) {
				reject(new Error('投递内容过大，媒体总大小请控制在 12 MB 以内'));
				request.destroy();
				return;
			}
			chunks.push(chunk);
		});
		request.on('end', async () => {
			try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
			catch { reject(new Error('请求不是有效 JSON')); }
		});
		request.on('error', reject);
	});
}

async function handleQuantAlert(request, response) {
	if (!quantAlertWebhookToken || request.headers['x-quant-alert-token'] !== quantAlertWebhookToken) {
		response.writeHead(401, { 'content-type': 'application/json' });
		response.end(JSON.stringify({ status: 'unauthorized' }));
		return;
	}
	if (!feishuAlertReceiveId) {
		response.writeHead(503, { 'content-type': 'application/json' });
		response.end(JSON.stringify({ status: 'disabled', reason: 'FEISHU_ALERT_RECEIVE_ID is not configured' }));
		return;
	}
	try {
		const payload = await readJsonBody(request, 16 * 1024);
		const text = String(payload?.text ?? '').trim();
		if (!text) throw new Error('alert text is required');
		if (text.length > 3500) throw new Error('alert text exceeds 3500 characters');
		const result = await larkClient.im.v1.message.create({
			params: { receive_id_type: feishuAlertReceiveIdType },
			data: { receive_id: feishuAlertReceiveId, msg_type: 'text', content: JSON.stringify({ text }) },
		});
		response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
		response.end(JSON.stringify({ status: 'sent', message_id: result?.data?.message_id ?? null }));
	} catch (error) {
		console.error(`盘中提醒投递失败：${error instanceof Error ? error.message : String(error)}`);
		response.writeHead(502, { 'content-type': 'application/json' });
		response.end(JSON.stringify({ status: 'failed', message: 'Feishu alert delivery failed' }));
	}
}

async function handleFeishuUserOauth(request, response) {
	if (!quantWriteApiKey || request.headers['x-quant-write-key'] !== quantWriteApiKey) {
		response.writeHead(401, { 'content-type': 'application/json' });
		response.end(JSON.stringify({ status: 'unauthorized' }));
		return;
	}
	try {
		const payload = await readJsonBody(request, 16 * 1024);
		const token = payload.refresh_token
			? await feishuUserOauth.bootstrapRefreshToken(payload.refresh_token)
			: await feishuUserOauth.exchangeAuthorizationCode(payload.authorization_code, payload.redirect_uri);
		response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
		response.end(JSON.stringify({ status: 'stored', ...token }));
	} catch (error) {
		console.error(`飞书用户 OAuth 授权失败：${error instanceof Error ? error.message : String(error)}`);
		response.writeHead(400, { 'content-type': 'application/json', 'cache-control': 'no-store' });
		response.end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) }));
	}
}

function readMultipartBody(request) {
	return new Promise((resolve, reject) => {
		let parser;
		try { parser = Busboy({ headers: request.headers, limits: { files: 12, fileSize: Number(process.env.INGESTION_MAX_FILE_BYTES ?? 524_288_000), fields: 8 } }); }
		catch (error) { reject(error); return; }
		const fields = {};
		const resources = [];
		const pending = [];
		parser.on('field', (name, value) => { fields[name] = value; });
		parser.on('file', (name, stream, info) => {
			const index = resources.length;
			const task = persistReadableAsset(stream, `manual_${index}`, String(info.filename || `manual-${index + 1}.bin`), String(info.mimeType || 'application/octet-stream'), Date.now()).then((asset) => { resources[index] = { ...asset, filename: asset.filename.replace(/[^\w.\-()\u4e00-\u9fff]+/g, '_').slice(0, 255) }; });
			pending.push(task);
		});
		parser.on('filesLimit', () => reject(new Error('一次最多上传 12 个文件')));
		parser.on('error', reject);
		parser.on('finish', async () => { try { await Promise.all(pending); resolve({ fields, resources }); } catch (error) { reject(error); } });
		request.pipe(parser);
	});
}

function createRelayDraft(text) {
	const now = Date.now();
	for (const [id, draft] of relayDrafts) if (draft.expires_at <= now) relayDrafts.delete(id);
	const id = randomUUID();
	relayDrafts.set(id, { text, expires_at: now + 5 * 60 * 1000 });
	return id;
}

async function buildManualEvent(input) {
	const tag = String(input?.tag ?? '').toLowerCase();
	resolveRoute(tag);
	const text = String(input?.text ?? '').trim();
	const contentDate = input?.content_date ? String(input.content_date) : '';
	const contentTime = input?.content_time ? String(input.content_time) : '';
	if ((contentDate && !contentTime) || (!contentDate && contentTime) || (contentDate && !isValidDateTime(contentDate, contentTime))) {
		throw new Error('指定时间无效，请同时填写日期和时间');
	}
	const resources = Array.isArray(input?.resources) ? input.resources : (Array.isArray(input?.media) ? input.media.map(manualResource) : []);
	if (!text && !resources.length) throw new Error('请至少提供正文或一个媒体');
	if (resources.length > 12) throw new Error('一次最多投递 12 个媒体');
	const eventId = `manual-${randomUUID()}`;
	const messageId = `manual_${randomUUID().replace(/-/g, '')}`;
	const timestamp = contentDate ? `@${contentDate} ${contentTime}` : '';
	const messageText = [`#${tag}`, timestamp, text].filter(Boolean).join('\n');
	const content = {
		title: '',
		content_v2: [
			[{ tag: 'text', text: messageText, style: [] }],
			...resources.map((resource, index) => [resource.media_type.startsWith('image/')
				? { tag: 'img', image_key: `manual-image-${index + 1}` }
				: { tag: 'media', file_key: `manual-media-${index + 1}` }]),
		],
	};
	return {
		data: {
			event_id: eventId,
			event_type: 'manual.relay',
			source: input?.ingress_source ? String(input.ingress_source).slice(0, 80) : 'manual-relay',
			source_label: input?.source_label ? String(input.source_label).slice(0, 120) : null,
			message: {
				message_id: messageId, chat_id: 'local-manual-relay', chat_type: 'local', message_type: 'post',
				create_time: String(Date.now()), content: JSON.stringify(content),
			},
			sender: { sender_id: { open_id: 'local-manual-relay' }, sender_type: 'user' },
		},
		resources,
		messageText,
	};
}

async function handleManualRelay(request, response) {
	let manual = null;
	try {
		const multipart = String(request.headers['content-type'] ?? '').toLowerCase().startsWith('multipart/form-data');
		const parsed = multipart ? await readMultipartBody(request) : await readJsonBody(request);
		const input = multipart ? { ...parsed.fields, resources: parsed.resources } : parsed;
		manual = await buildManualEvent(input);
		addEvent(manual.data);
		updateEvent(manual.data.event_id, { n8n_status: manual.resources.length ? '上传媒体并转发中' : '转发中' });
		const result = await forwardToN8n(manual.data, { resources: manual.resources, messageText: manual.messageText, sourceLabel: input.source_label });
		updateEvent(manual.data.event_id, { n8n_status: result?.duplicate ? '重复已跳过' : '已接收，处理中', target_status: result?.duplicate ? '本地幂等去重，未重复请求远端' : null });
		response.writeHead(202, { 'content-type': 'application/json' });
		response.end(JSON.stringify({ status: 'accepted', message_id: manual.data.message.message_id }));
	} catch (error) {
		if (manual?.resources) await Promise.all(manual.resources.map((resource) => resource.path ? unlink(resource.path).catch(() => {}) : Promise.resolve()));
		const message = error instanceof Error ? error.message : String(error);
		response.writeHead(400, { 'content-type': 'application/json' });
		response.end(JSON.stringify({ status: 'error', message }));
	}
}

async function renderMetrics() {
	const [rows, summary] = await Promise.all([ledger.metrics(), ledger.observability()]);
	const lines = ['# HELP ingestion_jobs Number of durable ingestion jobs by status and stage', '# TYPE ingestion_jobs gauge'];
	for (const row of rows) lines.push(`ingestion_jobs{status="${row.status}",stage="${row.stage}"} ${row.count}`);
	lines.push('# HELP ingestion_attempts_total Total recorded ingestion attempts', '# TYPE ingestion_attempts_total counter');
	for (const row of rows) lines.push(`ingestion_attempts_total{status="${row.status}",stage="${row.stage}"} ${row.attempts}`);
	let bytes = 0; let files = 0;
	for (const name of readdirSync(ingestionStorageDir)) { try { const stat = statSync(join(ingestionStorageDir, name)); if (stat.isFile()) { files++; bytes += stat.size; } } catch {} }
	lines.push('# HELP ingestion_temp_files Temporary media files on local disk', '# TYPE ingestion_temp_files gauge', `ingestion_temp_files ${files}`);
	lines.push('# HELP ingestion_temp_bytes Temporary media bytes on local disk', '# TYPE ingestion_temp_bytes gauge', `ingestion_temp_bytes ${bytes}`);
	lines.push('# HELP ingestion_queue_depth Durable jobs awaiting local or remote completion', '# TYPE ingestion_queue_depth gauge', `ingestion_queue_depth ${summary.queue_depth ?? 0}`);
	lines.push('# HELP ingestion_completed_media_bytes Total completed media bytes', '# TYPE ingestion_completed_media_bytes counter', `ingestion_completed_media_bytes ${summary.completed_media_bytes ?? 0}`);
	lines.push('# HELP ingestion_completed_seconds_mean Mean local completion duration in seconds', '# TYPE ingestion_completed_seconds_mean gauge', `ingestion_completed_seconds_mean ${summary.completed_seconds ?? 0}`);
	lines.push('# HELP ingestion_duplicate_ratio Completed or duplicate jobs that were duplicates', '# TYPE ingestion_duplicate_ratio gauge', `ingestion_duplicate_ratio ${(Number(summary.duplicates ?? 0) / Math.max(1, Number(summary.completed ?? 0) + Number(summary.duplicates ?? 0))).toFixed(6)}`);
	lines.push('# HELP ingestion_failed_jobs Durable failed jobs', '# TYPE ingestion_failed_jobs gauge', `ingestion_failed_jobs ${summary.failed ?? 0}`);
	return `${lines.join('\n')}\n`;
}

function asIsoString(value) {
	if (!value) return null;
	const timestamp = new Date(value).getTime();
	return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : null;
}

async function groupRelayDashboardStatus() {
	const [persistedSources, routes, oauth] = await Promise.all([ledger.relayStatus(), ledger.relayRoutes(), feishuUserOauth.status()]);
	const runtime = groupRelay.status();
	const persistedByKey = new Map(persistedSources.map((source) => [source.source_key, source]));
	const runtimeByKey = new Map(runtime.sources.map((source) => [source.key, source]));
	const staleAfterSeconds = Math.max(45, groupRelayConfig.intervalSeconds * 3);
	const now = Date.now();
	const sources = routes.map((source) => {
		const persisted = persistedByKey.get(source.key);
		const current = runtimeByKey.get(source.key);
		const lastPolledAt = asIsoString(persisted?.last_polled_at);
		const pollAgeSeconds = lastPolledAt ? Math.max(0, Math.floor((now - Date.parse(lastPolledAt)) / 1000)) : null;
		const failedCount = Number(persisted?.failed_count ?? 0);
		let state = 'healthy';
		if (!groupRelayConfig.enabled || source.enabled === false) state = 'disabled';
		else if (!oauth.configured) state = 'not_configured';
		else if (current?.state === 'error' || current?.state === 'unavailable') state = current.state;
		else if (!persisted || pollAgeSeconds === null) state = 'starting';
		else if (pollAgeSeconds > staleAfterSeconds) state = 'delayed';
		else if (failedCount > 0) state = 'degraded';
		return {
			key: source.key, tag: source.tag, chat_name: source.chatName, enabled: source.enabled !== false,
			state, last_polled_at: lastPolledAt, poll_age_seconds: pollAgeSeconds,
			last_source_message_at: asIsoString(persisted?.last_source_message_at),
			last_forwarded_at: asIsoString(persisted?.last_forwarded_at),
			last_reconciled_at: current?.last_reconciled_at ?? null,
			last_message_status: persisted?.last_message_status ?? null,
			failed_count: failedCount,
			last_error: current?.last_error ?? persisted?.latest_failure_error ?? null,
		};
	});
	const overall = !groupRelayConfig.enabled ? 'disabled'
		: sources.every((source) => source.state === 'healthy') ? 'healthy'
		: sources.some((source) => ['error', 'unavailable', 'delayed', 'degraded', 'not_configured'].includes(source.state)) ? 'degraded'
		: 'starting';
	return {
		status: overall, observed_at: new Date(now).toISOString(), enabled: groupRelayConfig.enabled,
		interval_seconds: groupRelayConfig.intervalSeconds, stale_after_seconds: staleAfterSeconds,
		user_oauth_configured: Boolean(oauth.configured), target_configured: Boolean(groupRelayConfig.targetChatId),
		last_tick_started_at: runtime.last_tick_started_at, last_tick_completed_at: runtime.last_tick_completed_at,
		last_tick_error: runtime.last_tick_error, sources,
	};
}

function publicRelayRoute(route) {
	return { key: route.key, chat_name: route.chatName, tag: route.tag, enabled: route.enabled !== false, created_at: asIsoString(route.created_at), updated_at: asIsoString(route.updated_at) };
}

function relayRouteTag(value) {
	const tag = String(value ?? '').trim().replace(/^#/, '').toLowerCase();
	if (!/^[a-z0-9][a-z0-9_-]{0,31}$/.test(tag)) throw new Error('标签必须为 1–32 位小写字母、数字、- 或 _，且不能以符号开头');
	return tag;
}

async function resolveRelayRouteInput(payload, current = null) {
	const chatName = String(payload?.chat_name ?? current?.chatName ?? '').trim();
	if (!chatName || chatName.length > 120) throw new Error('请填写 1–120 字的源群名称');
	const tag = relayRouteTag(payload?.tag ?? current?.tag);
	const enabled = payload?.enabled === undefined ? current?.enabled !== false : payload.enabled !== false;
	const requestedChatId = String(payload?.chat_id ?? '').trim();
	if (requestedChatId && !/^oc_[A-Za-z0-9]+$/.test(requestedChatId)) throw new Error('chat_id 格式无效');
	if (current && chatName === current.chatName && !requestedChatId) return { chatId: current.chatId, chatName, tag, enabled };
	const result = await feishuUserOauth.sourceApi.chatSearch(chatName);
	const exact = (result.data?.items ?? []).filter((chat) => chat.name === chatName);
	if (!exact.length && !requestedChatId) throw new Error(`未找到可读取的群“${chatName}”`);
	if (requestedChatId) {
		const matched = exact.find((chat) => chat.chat_id === requestedChatId);
		return { chatId: requestedChatId, chatName: matched?.name ?? chatName, tag, enabled };
	}
	if (exact.length > 1) throw new Error(`找到多个同名群“${chatName}”，请填写 chat_id 后再保存`);
	return { chatId: exact[0].chat_id, chatName: exact[0].name, tag, enabled };
}

async function createRelayRoute(payload) {
	const route = await resolveRelayRouteInput(payload);
	return ledger.createRelayRoute({ sourceKey: `relay_${randomUUID().replaceAll('-', '')}`, ...route });
}

async function updateRelayRoute(sourceKey, payload) {
	const current = (await ledger.relayRoutes()).find((route) => route.key === sourceKey);
	if (!current) return null;
	return ledger.updateRelayRoute(sourceKey, await resolveRelayRouteInput(payload, current));
}

const researchPaths = new Map([
	['/api/research/runtime/health', '/health'],
	['/api/research/overview', '/api/v1/research/overview'],
	['/api/research/reports', '/api/v1/remote-archive/reports'],
	['/api/research/remote-archive/messages', '/api/v1/remote-archive/messages'],
	['/api/research/claims', '/api/v1/analyst-claims'],
	['/api/research/providers', '/api/v1/providers/health'],
	['/api/research/provider-capabilities', '/api/v1/providers/capabilities'],
	['/api/research/quality', '/api/v1/data-quality/issues'],
	['/api/research/recommendations', '/api/v1/recommendations/latest'],
	['/api/research/universes/core', '/api/v1/universes/core'],
	['/api/research/features/latest', '/api/v1/features/latest'],
	['/api/research/claim-review', '/api/v1/claim-review'],
	['/api/research/factors', '/api/v1/factors'],
	['/api/research/factor-evaluations', '/api/v1/factors/evaluations'],
	['/api/research/strategies', '/api/v1/strategies'],
	['/api/research/strategy-experiments', '/api/v1/strategies/experiments'],
	['/api/research/strategy-experiments-watchlist', '/api/v1/strategies/experiments'],
	['/api/research/frameworks', '/api/v1/research-frameworks'],
	['/api/research/training/roadmap', '/api/v1/training/roadmap'],
	['/api/research/data-readiness/history-estimate', '/api/v1/data-readiness/history-estimate'],
	['/api/research/data-readiness/features', '/api/v1/data-readiness/features'],
	['/api/research/data-readiness/replay', '/api/v1/data-readiness/replay'],
	['/api/research/tushare/catalog', '/api/v1/providers/tushare/catalog'],
	['/api/research/tushare/raw', '/api/v1/providers/tushare/raw'],
	['/api/research/minute/imports', '/api/v1/market/minute/imports'],
	['/api/research/market/snapshots', '/api/v1/market/snapshots'],
	['/api/research/market/sectors', '/api/v1/market/sectors'],
	['/api/research/market/sector-flows', '/api/v1/market/sectors/flows'],
	['/api/research/market/sectors/concepts', '/api/v1/market/sectors/concepts'],
	['/api/research/market/sectors/concepts/candidates', '/api/v1/market/sectors/concepts/candidates'],
	['/api/research/market/sectors/concepts/members/backfill/status', '/api/v1/market/sectors/concepts/members/backfill/status'],
	['/api/research/market/sectors/review/report/latest', '/api/v1/market/sectors/review/report/latest'],
	['/api/research/market/sectors/intraday/curves', '/api/v1/market/sectors/intraday/curves'],
	['/api/research/market/flow/features', '/api/v1/market/flow/features'],
	['/api/research/intraday/board-rotations/latest', '/api/v1/intraday/board-rotations/latest'],
	['/api/research/intraday/board-stock-mining/latest', '/api/v1/intraday/board-stock-mining/latest'],
	['/api/research/intraday/limit-linkage/latest', '/api/v1/intraday/limit-linkage/latest'],
	['/api/research/strategy/reviews/latest', '/api/v1/strategy/reviews/latest'],
	['/api/research/strategy/post-close/latest', '/api/v1/strategy/post-close/latest'],
	['/api/research/strategy/ablation/latest', '/api/v1/strategy/ablation/latest'],
	['/api/research/strategy/health', '/api/v1/strategy/health'],
	['/api/research/strategy/pattern-mining/latest', '/api/v1/strategy/pattern-mining/latest'],
	['/api/research/intraday/outcomes/latest', '/api/v1/intraday/outcomes/latest'],
	['/api/research/paper/status', '/api/v1/paper/status'],
	['/api/research/strategy/contracts', '/api/v1/strategy/contracts'],
	['/api/research/strategy/funnel', '/api/v1/strategy/funnel'],
	['/api/research/intraday/services/status', '/api/v1/intraday/services/status'],
	['/api/research/analyst-scorecards', '/api/v1/analyst-scorecards'],
	['/api/research/analyst-research/observations', '/api/v1/analyst-research/observations'],
	['/api/research/analyst-research/status', '/api/v1/analyst-research/status'],
	['/api/research/analyst-skills', '/api/v1/analyst-skills'],
	['/api/research/analyst-research/sync-health', '/api/v1/analyst-research/sync-health'],
	['/api/research/analyst-prompt-lab/status', '/api/v1/analyst-prompt-lab/status'],
	['/api/research/strategy/governance', '/api/v1/strategy/governance'],
	['/api/research/paper/accounts', '/api/v1/paper/accounts'],
	['/api/research/events/announcements', '/api/v1/events/announcements'],
	['/api/research/events/lhb', '/api/v1/events/lhb'],
]);

const researchActions = new Map([
	['/api/research/tushare/fetch', '/api/v1/providers/tushare/fetch'],
	['/api/research/tushare/audit', '/api/v1/providers/tushare/audit'],
	['/api/research/pipeline/daily', '/api/v1/pipeline/daily'],
	['/api/research/snapshots/build', '/api/v1/data-snapshots/build'],
	['/api/research/reports/reprocess', '/api/v1/remote-archive/reports/reprocess'],
	['/api/research/outcomes/recompute', '/api/v1/outcomes/recompute'],
	['/api/research/intraday/outcomes/recompute', '/api/v1/intraday/outcomes/recompute'],
	['/api/research/scorecards/recompute', '/api/v1/analyst-scorecards/recompute'],
	['/api/research/features/build', '/api/v1/features/build'],
	['/api/research/recommendations/generate', '/api/v1/recommendations/generate'],
	['/api/research/universes/members', '/api/v1/universes/members'],
	['/api/research/factors/evaluate', '/api/v1/factors/evaluate'],
	['/api/research/strategies/backtest', '/api/v1/strategies/backtest'],
	['/api/research/strategy/post-close/run', '/api/v1/strategy/post-close/run'],
	['/api/research/strategy/pattern-mining/run', '/api/v1/strategy/pattern-mining/run'],
	['/api/research/strategy/watchlist-main-wave/run', '/api/v1/strategy/watchlist-main-wave/run'],
	['/api/research/market/universe/sync', '/api/v1/market/universe/sync'],
	['/api/research/market/full-daily/sync', '/api/v1/market/sync/full-daily'],
	['/api/research/market/post-close/refresh', '/api/v1/market/post-close/refresh'],
	['/api/research/market/flow/features/rebuild', '/api/v1/market/flow/features/rebuild'],
	['/api/research/market/snapshots/run', '/api/v1/market/snapshots/run'],
	['/api/research/market/sectors/sync', '/api/v1/market/sectors/sync'],
	['/api/research/market/sector-flows/sync', '/api/v1/market/sectors/flows/sync'],
	['/api/research/market/sectors/concepts/sync', '/api/v1/market/sectors/concepts/sync'],
	['/api/research/market/sectors/review/report/run', '/api/v1/market/sectors/review/report/run'],
	['/api/research/market/sectors/concepts/members/backfill/run', '/api/v1/market/sectors/concepts/members/backfill/run'],
	['/api/research/market/sectors/concepts/candidates/sync', '/api/v1/market/sectors/concepts/candidates/sync'],
	['/api/research/market/sectors/concepts/research/run', '/api/v1/market/sectors/concepts/research/run'],
	['/api/research/events/cninfo/sync', '/api/v1/events/cninfo/sync'],
	['/api/research/providers/realtime/probe', '/api/v1/providers/realtime/probe'],
	['/api/research/providers/akshare/probe', '/api/v1/providers/akshare/probe'],
	['/api/research/operations/fetch-runs/reconcile-stale', '/api/v1/operations/fetch-runs/reconcile-stale'],
	['/api/research/analyst-prompt-lab/materialize', '/api/v1/analyst-prompt-lab/materialize'],
	['/api/research/analyst-intraday-outcomes/recompute', '/api/v1/analyst-intraday-outcomes/recompute'],
]);

async function proxyResearch(path, search, response) {
	if (!quantServiceUrl) throw new Error('量化研究服务未配置');
	const upstream = await fetch(`${quantServiceUrl}${path}${search}`, { headers: { accept: 'application/json' }, signal: AbortSignal.timeout(15_000) });
	const body = await upstream.text();
	response.writeHead(upstream.status, { 'content-type': upstream.headers.get('content-type') ?? 'application/json', 'cache-control': 'no-store' });
	response.end(body);
}

async function proxyResearchAction(path, request, response, method = 'POST') {
	if (!quantServiceUrl) throw new Error('量化研究服务未配置');
	const chunks = []; let size = 0;
	for await (const chunk of request) {
		size += chunk.length;
		if (size > 64 * 1024) throw new Error('研究操作请求超过 64 KiB 上限');
		chunks.push(chunk);
	}
	const longRunning = path.includes('/market/') || path.includes('/tushare/audit') || path.includes('/realtime/probe') || path.includes('/akshare/probe') || path.includes('/strategy/post-close/run') || path.includes('/strategy/pattern-mining/run') || path.includes('/strategy/watchlist-main-wave/run');
	const timeoutMs = path.includes('/market/post-close/refresh') ? 360_000 : longRunning ? 180_000 : 45_000;
	const upstream = await fetch(`${quantServiceUrl}${path}`, {
		method,
		headers: {
			'content-type': 'application/json', accept: 'application/json',
			...(quantWriteApiKey ? { 'X-Quant-Write-Key': quantWriteApiKey } : {}),
		},
		body: Buffer.concat(chunks), signal: AbortSignal.timeout(timeoutMs),
	});
	const body = await upstream.text();
	response.writeHead(upstream.status, { 'content-type': upstream.headers.get('content-type') ?? 'application/json', 'cache-control': 'no-store' });
	response.end(body);
}

const dashboard = createServer((request, response) => {
	const url = new URL(request.url ?? '/', 'http://localhost');
	const researchPath = researchPaths.get(url.pathname);
	if (researchPath && request.method === 'GET') {
		void proxyResearch(researchPath, url.search, response).catch((error) => {
			response.writeHead(503, { 'content-type': 'application/json', 'cache-control': 'no-store' });
			response.end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) }));
		});
		return;
	}
	const researchAction = researchActions.get(url.pathname);
	if (researchAction && request.method === 'POST') {
		void proxyResearchAction(researchAction, request, response).catch((error) => {
			response.writeHead(503, { 'content-type': 'application/json', 'cache-control': 'no-store' });
			response.end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) }));
		});
		return;
	}
	if (url.pathname === '/api/research/paper/accounts' && request.method === 'PUT') {
		void proxyResearchAction('/api/v1/paper/accounts', request, response, 'PUT').catch((error) => {
			response.writeHead(503, { 'content-type': 'application/json', 'cache-control': 'no-store' });
			response.end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) }));
		});
		return;
	}
	const paperDecisionAccept = /^\/api\/research\/paper\/decisions\/([0-9a-f-]{36})\/accept$/i.exec(url.pathname);
	if (paperDecisionAccept && request.method === 'POST') {
		void proxyResearchAction(`/api/v1/paper/decisions/${paperDecisionAccept[1]}/accept`, request, response).catch((error) => {
			response.writeHead(503, { 'content-type': 'application/json', 'cache-control': 'no-store' });
			response.end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) }));
		});
		return;
	}
	const promptLabel = /^\/api\/research\/analyst-prompt-lab\/candidates\/([0-9a-f-]{36})\/label$/i.exec(url.pathname);
	if (promptLabel && request.method === 'POST') {
		void proxyResearchAction(`/api/v1/analyst-prompt-lab/candidates/${promptLabel[1]}/label`, request, response).catch((error) => {
			response.writeHead(503, { 'content-type': 'application/json', 'cache-control': 'no-store' });
			response.end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) }));
		});
		return;
	}
	const promptEvaluate = /^\/api\/research\/analyst-prompt-lab\/evaluate\/(strict_action|scenario_context|risk_first)$/i.exec(url.pathname);
	if (promptEvaluate && request.method === 'POST') {
		void proxyResearchAction(`/api/v1/analyst-prompt-lab/evaluate/${promptEvaluate[1]}`, request, response).catch((error) => {
			response.writeHead(503, { 'content-type': 'application/json', 'cache-control': 'no-store' });
			response.end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) }));
		});
		return;
	}
	const stockStudy = /^\/api\/research\/stocks\/(\d{6}\.(?:SH|SZ|BJ))\/study$/i.exec(url.pathname);
	if (stockStudy && request.method === 'POST') {
		const symbol = stockStudy[1].toUpperCase();
		void proxyResearchAction(`/api/v1/stocks/${encodeURIComponent(symbol)}/study`, request, response).catch((error) => {
			response.writeHead(503, { 'content-type': 'application/json', 'cache-control': 'no-store' });
			response.end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) }));
		});
		return;
	}
	const claimReview = /^\/api\/research\/claim-review\/([0-9a-f-]{36})$/i.exec(url.pathname);
	if (claimReview && request.method === 'POST') {
		void proxyResearchAction(`/api/v1/claim-review/${claimReview[1]}`, request, response).catch((error) => {
			response.writeHead(503, { 'content-type': 'application/json', 'cache-control': 'no-store' });
			response.end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) }));
		});
		return;
	}
	if (url.pathname === '/api/config' && request.method === 'GET') {
		response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
		response.end(JSON.stringify({ routes: [...sourceRoutes.values()].filter((route) => route.enabled !== false).map(({ tag, label, topic_key, publisher_key }) => ({ tag, label, topic_key, publisher_key })) }));
		return;
	}
	if (url.pathname.startsWith('/api/jobs/') && request.method === 'GET') {
		void ledger.getJob(url.pathname.slice('/api/jobs/'.length)).then((job) => { if (!job) { response.writeHead(404).end(); return; } response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' }); response.end(JSON.stringify({ job })); }).catch((error) => response.writeHead(503).end(String(error)));
		return;
	}
	if (url.pathname.startsWith('/api/assets/') && url.pathname.endsWith('/parts') && request.method === 'GET') {
		const assetId = url.pathname.slice('/api/assets/'.length, -'/parts'.length);
		void ledger.assetParts(assetId).then((parts) => response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' }).end(JSON.stringify({ parts }))).catch((error) => response.writeHead(503).end(String(error)));
		return;
	}
	if (url.pathname.startsWith('/api/jobs/') && url.pathname.endsWith('/retry') && request.method === 'POST') {
		const jobId = url.pathname.slice('/api/jobs/'.length, -'/retry'.length);
		void ledger.retryJob(jobId).then((job) => { if (!job) { response.writeHead(409, { 'content-type': 'application/json' }).end(JSON.stringify({ status: 'error', message: '只有失败、重复或卡住的排队任务可以手动重试' })); return; } response.writeHead(202, { 'content-type': 'application/json' }).end(JSON.stringify({ status: 'queued', job_id: job.job_id, message: '已进入本地重试队列；不会自动创建重复远端请求' })); }).catch((error) => response.writeHead(503).end(String(error)));
		return;
	}
	if (url.pathname === '/api/group-relay/status' && request.method === 'GET') {
		void groupRelayDashboardStatus().then((status) => {
			response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
			response.end(JSON.stringify(status));
		}).catch((error) => {
			response.writeHead(503, { 'content-type': 'application/json', 'cache-control': 'no-store' });
			response.end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) }));
		});
		return;
	}
	if (url.pathname === '/api/group-relay/routes' && request.method === 'GET') {
		void ledger.relayRoutes().then((routes) => {
			response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
			response.end(JSON.stringify({ routes: routes.map(publicRelayRoute) }));
		}).catch((error) => response.writeHead(503, { 'content-type': 'application/json' }).end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) })));
		return;
	}
	if (url.pathname === '/api/group-relay/routes' && request.method === 'POST') {
		void readJsonBody(request, 16 * 1024).then(createRelayRoute).then((route) => {
			response.writeHead(201, { 'content-type': 'application/json', 'cache-control': 'no-store' });
			response.end(JSON.stringify({ route: publicRelayRoute(route) }));
		}).catch((error) => response.writeHead(400, { 'content-type': 'application/json' }).end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) })));
		return;
	}
	const relayRouteMatch = /^\/api\/group-relay\/routes\/([A-Za-z0-9_-]+)$/.exec(url.pathname);
	if (relayRouteMatch && request.method === 'PUT') {
		void readJsonBody(request, 16 * 1024).then((payload) => updateRelayRoute(relayRouteMatch[1], payload)).then((route) => {
			if (!route) { response.writeHead(404, { 'content-type': 'application/json' }).end(JSON.stringify({ status: 'error', message: '源群不存在' })); return; }
			response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
			response.end(JSON.stringify({ route: publicRelayRoute(route) }));
		}).catch((error) => response.writeHead(400, { 'content-type': 'application/json' }).end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) })));
		return;
	}
	if (relayRouteMatch && request.method === 'DELETE') {
		void ledger.deleteRelayRoute(relayRouteMatch[1]).then((deleted) => {
			if (!deleted) { response.writeHead(404, { 'content-type': 'application/json' }).end(JSON.stringify({ status: 'error', message: '源群不存在' })); return; }
			response.writeHead(204, { 'cache-control': 'no-store' }).end();
		}).catch((error) => response.writeHead(503, { 'content-type': 'application/json' }).end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) })));
		return;
	}
	if (url.pathname === '/api/feishu-workbench/status' && request.method === 'GET') {
		void Promise.all([feishuWorkbench.status(), feishuUserOauth.status()]).then(([workbench, oauth]) => {
			response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
			response.end(JSON.stringify({ ...workbench, user_oauth_configured: Boolean(oauth.configured), user_oauth_scopes: oauth.scopes ?? '' }));
		}).catch((error) => response.writeHead(503, { 'content-type': 'application/json', 'cache-control': 'no-store' }).end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) })));
		return;
	}
	if (url.pathname === '/api/feishu-workbench/messages' && request.method === 'GET') {
		const limit = Number(url.searchParams.get('limit') ?? 50);
		void ledger.recentRelayMessages(limit).then((items) => {
			response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
			response.end(JSON.stringify({ items }));
		}).catch((error) => response.writeHead(503, { 'content-type': 'application/json' }).end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) })));
		return;
	}
	if (url.pathname === '/api/feishu-workbench/actions' && request.method === 'POST') {
		void readJsonBody(request, 32 * 1024).then((payload) => feishuWorkbench.performAction({
			sourceMessageId: String(payload?.source_message_id ?? ''), action: String(payload?.action ?? ''),
			operatorOpenId: String(payload?.operator_open_id ?? ''), operatorName: String(payload?.operator_name ?? ''),
		})).then((result) => {
			response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
			response.end(JSON.stringify({ status: 'ok', record: result.record, external: result.external ?? null }));
		}).catch((error) => response.writeHead(400, { 'content-type': 'application/json', 'cache-control': 'no-store' }).end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) })));
		return;
	}
	if (url.pathname === '/api/feishu-workbench/documents' && request.method === 'POST') {
		void readJsonBody(request, 64 * 1024).then((payload) => feishuWorkbench.createDocument(payload ?? {})).then((result) => {
			response.writeHead(201, { 'content-type': 'application/json', 'cache-control': 'no-store' }); response.end(JSON.stringify({ status: 'created', result }));
		}).catch((error) => response.writeHead(400, { 'content-type': 'application/json' }).end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) })));
		return;
	}
	if (url.pathname === '/api/feishu-workbench/base-records' && request.method === 'POST') {
		void readJsonBody(request, 128 * 1024).then((payload) => feishuWorkbench.createBaseRecord(payload ?? {})).then((result) => {
			response.writeHead(201, { 'content-type': 'application/json', 'cache-control': 'no-store' }); response.end(JSON.stringify({ status: 'created', result }));
		}).catch((error) => response.writeHead(400, { 'content-type': 'application/json' }).end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) })));
		return;
	}
	if (url.pathname === '/api/feishu-workbench/calendar-events' && request.method === 'POST') {
		void readJsonBody(request, 64 * 1024).then((payload) => feishuWorkbench.createCalendarEvent(payload ?? {})).then((result) => {
			response.writeHead(201, { 'content-type': 'application/json', 'cache-control': 'no-store' }); response.end(JSON.stringify({ status: 'created', result }));
		}).catch((error) => response.writeHead(400, { 'content-type': 'application/json' }).end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) })));
		return;
	}
	if (url.pathname === '/api/feishu-workbench/approvals' && request.method === 'POST') {
		void readJsonBody(request, 128 * 1024).then((payload) => feishuWorkbench.createApproval(payload ?? {})).then((result) => {
			response.writeHead(201, { 'content-type': 'application/json', 'cache-control': 'no-store' }); response.end(JSON.stringify({ status: 'created', result }));
		}).catch((error) => response.writeHead(400, { 'content-type': 'application/json' }).end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) })));
		return;
	}
	if (url.pathname === '/api/feishu-workbench/message-search' && request.method === 'POST') {
		void readJsonBody(request, 16 * 1024).then((payload) => feishuWorkbench.searchMessages(payload ?? {})).then((result) => {
			response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' }); response.end(JSON.stringify({ status: 'ok', result }));
		}).catch((error) => response.writeHead(400, { 'content-type': 'application/json' }).end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) })));
		return;
	}
	// API paths must never fall through to the SPA.  A missing API route used to
	// return index.html (200), which surfaced in the dashboard as the misleading
	// "Unexpected token '<'" JSON parse error instead of an actionable 404.
	if (url.pathname.startsWith('/api/')) {
		response.writeHead(404, { 'content-type': 'application/json', 'cache-control': 'no-store' });
		response.end(JSON.stringify({ status: 'not_found', message: `未知 API：${url.pathname}` }));
		return;
	}
	if (frontendMode === 'spa' && request.method === 'GET' && !['/health', '/events', '/metrics', '/jobs', '/analysis/jobs'].includes(url.pathname)) {
		const requested = url.pathname === '/relay' ? 'index.html' : url.pathname.slice(1);
		const assetPath = join(frontendDist, requested.includes('.') ? requested : 'index.html');
		try { const body = readFileSync(assetPath); const type = assetPath.endsWith('.js') ? 'text/javascript' : assetPath.endsWith('.css') ? 'text/css' : 'text/html; charset=utf-8'; response.writeHead(200, { 'content-type': type, 'cache-control': assetPath.endsWith('index.html') ? 'no-cache' : 'public, max-age=31536000, immutable' }); response.end(body); } catch { response.writeHead(404).end(); }
		return;
	}
	if (url.pathname === '/health') {
		response.writeHead(200, { 'content-type': 'application/json' });
		response.end(JSON.stringify({ status: 'ok', events: recentEvents.length, quant_alert_configured: Boolean(quantAlertWebhookToken && feishuAlertReceiveId) }));
		return;
	}
	if (url.pathname === '/internal/quant-alert' && request.method === 'POST') {
		void handleQuantAlert(request, response);
		return;
	}
	if (url.pathname === '/internal/feishu-user-oauth' && request.method === 'POST') {
		void handleFeishuUserOauth(request, response);
		return;
	}
	if (url.pathname === '/metrics') {
		void renderMetrics().then((metrics) => { response.writeHead(200, { 'content-type': 'text/plain; version=0.0.4' }); response.end(metrics); }).catch((error) => { response.writeHead(503).end(String(error)); });
		return;
	}
	if (url.pathname === '/jobs' && request.method === 'GET') {
		void ledger.pendingJobs().then((jobs) => { response.writeHead(200, { 'content-type': 'application/json' }); response.end(JSON.stringify({ jobs })); }).catch((error) => { response.writeHead(503).end(JSON.stringify({ status: 'error', message: String(error) })); });
		return;
	}
	if (url.pathname === '/analysis/jobs' && request.method === 'GET') {
		void ledger.pendingAnalysis().then((jobs) => { response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' }).end(JSON.stringify({ jobs })); }).catch((error) => response.writeHead(503).end(String(error)));
		return;
	}
	if (url.pathname === '/reconcile' && request.method === 'POST') {
		void reconcileNow().then((result) => response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' }).end(JSON.stringify(result))).catch((error) => response.writeHead(503, { 'content-type': 'application/json' }).end(JSON.stringify({ status: 'error', message: String(error) })));
		return;
	}
	if (url.pathname === '/events') {
		response.writeHead(200, {
			'content-type': 'text/event-stream',
			'cache-control': 'no-cache',
			connection: 'keep-alive',
		});
		sendSse(response, 'snapshot', recentEvents);
		eventStreams.add(response);
		request.on('close', () => eventStreams.delete(response));
		return;
	}
	if (url.pathname === '/n8n-status' && request.method === 'POST') {
		const chunks = [];
		request.on('data', (chunk) => chunks.push(chunk));
		request.on('end', async () => {
			try {
				const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
				const event = recentEvents.find((entry) => entry.message_id === payload.message_id);
				if (event) {
					Object.assign(event, {
						n8n_status: payload.n8n_status ?? '已完成',
						target_status: payload.target_status ?? null,
						target_batch_id: payload.target_batch_id ?? null,
						n8n_error: payload.n8n_error ?? null,
					});
					broadcastSnapshot();
				}
				if (payload.n8n_status === '已完成' && payload.message_id) await ledger.queueAnalysisByMessage(payload.message_id, payload.target_batch_id);
				response.writeHead(200, { 'content-type': 'application/json' });
				response.end(JSON.stringify({ status: 'ok' }));
			} catch (error) {
				response.writeHead(400, { 'content-type': 'application/json' });
				response.end(JSON.stringify({ status: 'error', message: String(error) }));
			}
		});
		return;
	}
	if (url.pathname === '/n8n-error' && request.method === 'POST') {
		void (async () => { try { const payload = await readJsonBody(request, 512 * 1024); await ledger.recordError(payload); response.writeHead(202, { 'content-type': 'application/json' }).end(JSON.stringify({ status: 'recorded' })); } catch (error) { response.writeHead(400, { 'content-type': 'application/json' }).end(JSON.stringify({ status: 'error', message: String(error) })); } })();
		return;
	}
	if (url.pathname === '/manual-relay' && request.method === 'POST') {
		void handleManualRelay(request, response);
		return;
	}
	if (url.pathname === '/relay-clipboard-draft' && request.method === 'POST') {
		void (async () => {
			try {
				const input = await readJsonBody(request, 1024 * 1024);
				const text = String(input?.text ?? '');
				if (!text.trim()) throw new Error('剪贴板没有可投递的文字');
				response.writeHead(201, { 'content-type': 'application/json' });
				response.end(JSON.stringify({ draft_id: createRelayDraft(text) }));
			} catch (error) {
				response.writeHead(400, { 'content-type': 'application/json' });
				response.end(JSON.stringify({ status: 'error', message: error instanceof Error ? error.message : String(error) }));
			}
		})();
		return;
	}
	if (url.pathname.startsWith('/relay-draft/') && request.method === 'GET') {
		const id = url.pathname.slice('/relay-draft/'.length);
		const draft = relayDrafts.get(id);
		if (!draft || draft.expires_at <= Date.now()) {
			relayDrafts.delete(id);
			response.writeHead(404, { 'content-type': 'application/json' });
			response.end(JSON.stringify({ status: 'error', message: '草稿已过期，请重新运行快捷键' }));
			return;
		}
		relayDrafts.delete(id);
		response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
		response.end(JSON.stringify({ text: draft.text }));
		return;
	}
	if (url.pathname === '/relay') {
		response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
		response.end(relayHtml);
		return;
	}
	if (url.pathname === '/') {
		response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
		response.end(dashboardHtml);
		return;
	}
	response.writeHead(404).end();
});

async function forwardToN8n(data, manual = null) {
	const resources = manual?.resources ?? await downloadMedia(data);
	const messageText = manual?.messageText ?? String(extractMessagePayload(data.message ?? {}).text ?? '').trim();
	const route = routeFromMessageText(messageText);
	const receivedAt = manual?.receivedAt ?? new Date().toISOString();
	const importContent = manual?.importContent ?? extractImportContent(messageText, receivedAt);
	const payload = {
			source: manual?.source ?? (manual ? 'manual-relay' : 'feishu'),
			source_label: manual?.sourceLabel ? String(manual.sourceLabel).slice(0, 120) : null,
			receivedAt,
			event: data,
			message_text: messageText,
			import_content: importContent.content,
			content_date: importContent.content_date,
			content_time: importContent.content_time,
			text_content_sha256: importContent.content ? createHash('sha256').update(importContent.content).digest('hex') : null,
		resources: resources.map(({ data: _data, path: _path, ...metadata }) => metadata),
			topic_key: route.topic_key ?? sourceRegistry.default_topic_key ?? 'general',
			publisher_key: route.publisher_key,
		analyst_id: route.remote_analyst_id,
	};
	const { job, duplicate } = await ledger.getOrCreateJob({
		jobId: randomUUID(), eventId: payload.event?.event_id, messageId: payload.event?.message?.message_id,
		route, payload: { source: payload.source, source_label: payload.source_label, receivedAt, event: payload.event, message_text: messageText, import_content: importContent.content, content_date: importContent.content_date, content_time: importContent.content_time, resources: payload.resources }, contentSha256: payload.text_content_sha256,
	});
	if (duplicate && !manual?.replayJobId) return { jobId: job.job_id, duplicate: true, batchId: job.remote_batch_id };
	if (payload.import_content) await ledger.recordContentItem(job.job_id, { content_type: 'text', content_sha256: payload.text_content_sha256, content_date: importContent.content_date, content_time: importContent.content_time, body: importContent.content });
	const previousAssets = await ledger.findCompletedAssets(resources.map((resource) => resource.content_sha256));
	if (resources.length && previousAssets.length === resources.length) {
		for (const [ordinal, resource] of resources.entries()) { await ledger.recordAsset(job.job_id, ordinal, resource); }
		await ledger.markAssets(job.job_id, 'duplicate');
		await ledger.updateJob(job.job_id, { status: 'duplicate', stage: 'duplicate_media', error_class: 'remote_duplicate_prevented', error_message: '本地已存在相同媒体 SHA256，未再次创建远端请求' });
		await Promise.all(resources.map((resource) => resource.path ? unlink(resource.path).catch(() => {}) : Promise.resolve()));
		return { jobId: job.job_id, duplicate: true };
	}
	await ledger.updateJob(job.job_id, { status: resources.length ? 'uploading' : 'queued', stage: resources.length ? 'uploading_parts' : 'creating_text', attempt_count: Number(job.attempt_count ?? 0) + 1 });
	const assetIds = [];
	for (const [ordinal, resource] of resources.entries()) assetIds.push(await ledger.recordAsset(job.job_id, ordinal, resource));
	const totalBytes = resources.reduce((sum, resource) => sum + resource.declared_bytes, 0);
	if (resources.length) {
		let batchId = manual?.remoteBatchId ?? job.remote_batch_id ?? null;
		for (const resource of resources) {
			let lastUpload = batchId && resource.remote_upload_id ? { batch_id: batchId, upload_id: resource.remote_upload_id } : null;
			let offset = 0;
			for (let partIndex = 0; partIndex < resource.parts.length; partIndex++) {
				const manifest = resource.parts[partIndex];
				if (manifest.uploaded) { offset += manifest.bytes; continue; }
				const bytes = resource.path ? await readAssetPart(resource, offset, manifest.bytes) : resource.data.subarray(offset, offset + manifest.bytes);
				if (bytes.length !== manifest.bytes) throw new Error(`媒体分片大小不一致：${resource.filename} part ${partIndex}`);
				const form = new FormData();
				const fields = {
					analyst_id: payload.analyst_id,
					topic_key: payload.topic_key,
					publisher_key: payload.publisher_key,
					batch_key: payload.event?.message?.message_id ? `b_${String(payload.event.message.message_id).replace(/[^A-Za-z0-9_-]/g, '').slice(0, 52).padEnd(24, '0')}` : `b_${randomUUID().replace(/-/g, '')}`,
					item_key: payload.event?.message?.message_id ? `i_${String(payload.event.message.message_id).replace(/[^A-Za-z0-9_-]/g, '').slice(0, 52).padEnd(24, '0')}` : `i_${randomUUID().replace(/-/g, '')}`,
					media_upload_key: `${payload.event?.message?.message_id ? `u_${String(payload.event.message.message_id).replace(/[^A-Za-z0-9_-]/g, '').slice(0, 52).padEnd(24, '0')}` : `u_${randomUUID().replace(/-/g, '')}`}_${resources.indexOf(resource) + 1}`,
					media_filename: resource.filename,
					media_type: resource.media_type,
					media_bytes: String(resource.declared_bytes),
					media_sha256: resource.content_sha256,
					media_last_modified: String(resource.last_modified),
					batch_id: lastUpload?.batch_id ?? batchId ?? '',
					upload_id: lastUpload?.upload_id ?? resource.remote_upload_id ?? '',
					part_index: String(partIndex),
					part_sha256: manifest.sha256,
					content: importContent.content,
					content_sha256: payload.text_content_sha256 ?? '',
					content_date: importContent.content_date,
					content_time: importContent.content_time,
					source_label: payload.source_label ?? (payload.source === 'manual-relay' ? '本机手动投递' : '飞书机器人'),
				};
				for (const [key, value] of Object.entries(fields)) form.append(key, String(value ?? ''));
				form.append('media', new Blob([bytes], { type: resource.media_type }), `${resource.filename}.part-${partIndex}`);
				const controller = new AbortController();
				const timer = setTimeout(() => controller.abort(), Math.min(180_000, 30_000 + Math.ceil(totalBytes / uploadPartBytes) * 5_000));
				try {
					const response = await fetchWithBackoff(mediaPartWebhookUrl, { method: 'POST', body: form, signal: controller.signal }, { maxAttempts: 1 });
					if (!response.ok) {
						const remoteText = (await response.text()).slice(0, 500);
						await ledger.updateJob(job.job_id, {
							status: response.status === 409 || response.status >= 500 ? 'retryable_failed' : 'failed',
							stage: 'upload_part', last_http_status: response.status,
							error_class: response.status === 409 ? 'remote_conflict' : 'remote_http',
							error_message: `media part HTTP ${response.status}${remoteText ? `: ${remoteText}` : ''}`,
						});
						throw new Error(`n8n media part webhook returned HTTP ${response.status}: ${remoteText.slice(0, 240)}`);
					}
					lastUpload = await response.json();
					if (!lastUpload?.batch_id || !lastUpload?.upload_id) throw new Error('n8n 未返回媒体批次或 upload_id');
					batchId = lastUpload.batch_id;
					await ledger.updateJob(job.job_id, { remote_batch_id: batchId, status: 'uploading', stage: 'uploading_parts' });
					await ledger.updateAssetSession(assetIds[resources.indexOf(resource)], 'uploading', lastUpload.upload_id);
					await ledger.recordPart(assetIds[resources.indexOf(resource)], partIndex, response.status);
				} finally { clearTimeout(timer); }
				 offset += bytes.length;
			}
			if (!lastUpload?.batch_id || !lastUpload?.upload_id) throw new Error('缺少可恢复的媒体批次或 upload_id');
			const finalResponse = await fetchWithBackoff(mediaFinalizeWebhookUrl, {
				method: 'POST', headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ batch_id: lastUpload.batch_id, upload_id: lastUpload.upload_id, media_sha256: resource.content_sha256, message_id: payload.event?.message?.message_id ?? null, submit: resources.indexOf(resource) === resources.length - 1 }),
			}, { maxAttempts: 1 });
			if (!finalResponse.ok) throw new Error(`n8n media finalize webhook returned HTTP ${finalResponse.status}: ${(await finalResponse.text()).slice(0, 240)}`);
			await finalResponse.text();
			await ledger.updateJob(job.job_id, { status: 'submitting', stage: 'remote_submit', remote_batch_id: lastUpload.batch_id });
			await ledger.updateAssetSession(assetIds[resources.indexOf(resource)], 'completed', lastUpload.upload_id);
		}
		await ledger.updateJob(job.job_id, { status: 'completed', stage: 'submitted' });
		await Promise.all(resources.map((resource) => resource.path ? unlink(resource.path).catch(() => {}) : Promise.resolve()));
		return { jobId: job.job_id, batchId: job.remote_batch_id };
	}
	const headers = { 'content-type': 'application/json' };
	const body = JSON.stringify(payload);
	const targetWebhookUrl = resources.length ? mediaWebhookUrl : textWebhookUrl;
	const timeoutMs = Math.min(180_000, 30_000 + Math.ceil(totalBytes / uploadPartBytes) * 5_000);
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), timeoutMs);
	try {
		const response = await fetchWithBackoff(targetWebhookUrl, {
			method: 'POST',
			headers,
			body,
			signal: controller.signal,
		}, { maxAttempts: 1 });

		if (!response.ok) {
			const remoteBody = await response.text();
			await ledger.updateJob(job.job_id, { status: response.status >= 500 ? 'retryable_failed' : 'failed', stage: 'text_or_submit', last_http_status: response.status, error_class: 'n8n_webhook', error_message: remoteBody.slice(0, 500) });
			throw new Error(`n8n webhook returned HTTP ${response.status}${remoteBody ? `: ${remoteBody.slice(0, 240)}` : ''}`);
		}

		await response.text();
		await ledger.updateJob(job.job_id, { status: 'completed', stage: 'submitted' });
		return { jobId: job.job_id };
	} finally {
		clearTimeout(timer);
	}
}

function feishuDedupeKeys(data) {
	return [...new Set([
		data?.event_id ? `event:${data.event_id}` : null,
		data?.message?.message_id ? `message:${data.message.message_id}` : null,
	].filter(Boolean))];
}

function isQuantAlertBindingCommand(data) {
	const text = String(extractMessagePayload(data?.message ?? {}).text ?? '').replace(/@_user_\d+\s*/g, '').trim();
	return text === '盘中提醒绑定';
}

function pruneFeishuDedupe(now = Date.now()) {
	for (const [key, entry] of feishuEventPromises) {
		if (entry.expiresAt <= now) feishuEventPromises.delete(key);
	}
}

async function processFeishuEvent(data) {
	const eventId = data?.event_id ?? 'unknown';
	console.info(`Forwarding im.message.receive_v1 event ${eventId} to n8n`);
	addEvent(data);
	// Binding a private alert group is an adapter control command.  It must not
	// be interpreted as analyst research content or require an ingestion route.
	if (isQuantAlertBindingCommand(data)) {
		updateEvent(eventId, { n8n_status: '已识别为盘中提醒绑定命令，未转发研究导入' });
		return { bound_alert_group: true };
	}
	const hasMedia = extractMessagePayload(data?.message ?? {}).resources.length > 0;
	updateEvent(eventId, { n8n_status: hasMedia ? '下载媒体并转发中' : '转发中' });
	try {
		const result = await forwardToN8n(data);
		updateEvent(eventId, { n8n_status: result?.duplicate ? '重复已跳过' : '已接收，处理中', target_status: result?.duplicate ? '本地幂等去重，未重复请求远端' : null });
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		updateEvent(eventId, { n8n_status: '失败', n8n_error: message });
		throw error;
	}
}

function cardActionInput(data) {
	const value = data?.event?.action?.value ?? data?.action?.value ?? data?.event?.action?.form_value ?? {};
	if (typeof value === 'string') { try { return JSON.parse(value); } catch { return {}; } }
	return value && typeof value === 'object' ? value : {};
}

async function processFeishuCardAction(data) {
	const input = cardActionInput(data);
	const action = String(input.action ?? '');
	const sourceMessageId = String(input.source_message_id ?? '');
	if (!action || !sourceMessageId) return { toast: { type: 'warning', content: '无效的行动卡片参数' } };
	const operator = data?.event?.operator ?? data?.operator ?? {};
	try {
		await feishuWorkbench.performAction({
			sourceMessageId, action,
			operatorOpenId: String(operator?.open_id ?? operator?.operator_id?.open_id ?? ''),
			operatorName: String(operator?.name ?? ''),
		});
		return { toast: { type: 'success', content: '已更新协作状态' } };
	} catch (error) {
		console.error(`飞书行动卡片处理失败：${error instanceof Error ? error.message : String(error)}`);
		return { toast: { type: 'error', content: error instanceof Error ? error.message.slice(0, 80) : '操作失败' } };
	}
}

async function processFeishuReaction(data) {
	const event = data?.event ?? data ?? {};
	const messageId = String(event?.message_id ?? event?.reaction?.message_id ?? '');
	const emoji = String(event?.reaction?.reaction_type?.emoji_type ?? event?.reaction_type?.emoji_type ?? '').toUpperCase();
	const action = ({ THUMBSUP: 'research', EYES: 'focus', CHECK_MARK: 'task', CROSS_MARK: 'ignore' })[emoji];
	if (!messageId || !action) return;
	const record = await ledger.getRelayMessageByActionCard(messageId);
	if (!record) return;
	const operator = event?.operator ?? event?.user ?? {};
	await feishuWorkbench.performAction({ sourceMessageId: record.source_message_id, action, operatorOpenId: String(operator?.open_id ?? operator?.operator_id?.open_id ?? ''), operatorName: String(operator?.name ?? '') });
}

async function processBotMenuEvent(data) {
	const event = data?.event ?? data ?? {};
	const eventKey = String(event?.event_key ?? event?.key ?? '');
	if (eventKey !== 'workbench') return;
	const h5Base = String(process.env.FEISHU_WORKBENCH_PUBLIC_BASE_URL ?? '').replace(/\/$/, '');
	const openId = event?.operator?.open_id ?? event?.operator_id?.open_id;
	if (!h5Base || !openId) return;
	const result = await larkClient.im.v1.message.create({ params: { receive_id_type: 'open_id' }, data: {
		receive_id: openId, msg_type: 'interactive', content: JSON.stringify({
			config: { wide_screen_mode: true }, header: { title: { tag: 'plain_text', content: '分析师工作台' }, template: 'blue' },
			elements: [{ tag: 'div', text: { tag: 'lark_md', content: '打开工作台查看群监听健康、协作行动与已配置能力。' } }, { tag: 'action', actions: [{ tag: 'button', type: 'primary', text: { tag: 'plain_text', content: '打开工作台' }, url: `${h5Base}/workbench` }] }],
		}),
	} });
	if (result.code && result.code !== 0) throw new Error(`发送工作台菜单回复失败：${result.msg ?? result.code}`);
}

const eventDispatcher = new Lark.EventDispatcher({ loggerLevel: Lark.LoggerLevel.info }).register({
	'im.message.receive_v1': async (data) => {
		const keys = feishuDedupeKeys(data);
		if (!keys.length || feishuDedupeTtlMs === 0) return processFeishuEvent(data);
		const now = Date.now();
		pruneFeishuDedupe(now);
		const existing = keys.map((key) => feishuEventPromises.get(key)).find((entry) => entry && entry.expiresAt > now);
		if (existing) {
			console.info(`Skipping duplicate Feishu event ${data?.event_id ?? data?.message?.message_id ?? 'unknown'}`);
			return existing.promise;
		}

		const promise = processFeishuEvent(data);
		const entry = { promise, expiresAt: now + feishuDedupeTtlMs, keys };
		for (const key of keys) feishuEventPromises.set(key, entry);
		try {
			return await promise;
		} catch (error) {
			for (const key of keys) {
				if (feishuEventPromises.get(key) === entry) feishuEventPromises.delete(key);
			}
			throw error;
		}
	},
	'card.action.trigger': processFeishuCardAction,
	'im.message.reaction.created_v1': processFeishuReaction,
	'application.bot.menu_v6': processBotMenuEvent,
});

const wsClient = new Lark.WSClient({
	appId,
	appSecret,
	domain: Lark.Domain.Feishu,
	loggerLevel: Lark.LoggerLevel.info,
});

process.on('unhandledRejection', (error) => {
	console.error('Unhandled adapter rejection', error);
});

dashboard.listen(dashboardPort, '0.0.0.0', () => {
	console.info(`Feishu monitor available on port ${dashboardPort}`);
});
console.info('Starting Feishu long-connection client');
wsClient.start({ eventDispatcher });
