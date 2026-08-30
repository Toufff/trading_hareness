import { createCipheriv, createDecipheriv, createHash, randomBytes } from 'node:crypto';
import { createReadStream, createWriteStream } from 'node:fs';
import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const API_BASE = 'https://pan.baidu.com';
const OAUTH_BASE = 'https://openapi.baidu.com';
const ACCESS_TOKEN_SKEW_MS = 60_000;
const DEFAULT_SLICE_BYTES = 4 * 1024 * 1024;
const DEFAULT_MAX_UPLOAD_BYTES = 500 * 1024 * 1024;
const BAIDU_OAUTH_SCOPES = 'basic,netdisk';

function deriveKey(secretKey) {
	return createHash('sha256').update(`baidu-pan-oauth:${secretKey}`).digest();
}

function encrypt(value, key) {
	const iv = randomBytes(12);
	const cipher = createCipheriv('aes-256-gcm', key, iv);
	const ciphertext = Buffer.concat([cipher.update(String(value), 'utf8'), cipher.final()]);
	return `v1.${iv.toString('base64url')}.${cipher.getAuthTag().toString('base64url')}.${ciphertext.toString('base64url')}`;
}

function decrypt(value, key) {
	const [version, iv, tag, ciphertext] = String(value ?? '').split('.');
	if (version !== 'v1' || !iv || !tag || !ciphertext) throw new Error('百度网盘凭据格式无效，请重新授权');
	const decipher = createDecipheriv('aes-256-gcm', key, Buffer.from(iv, 'base64url'));
	decipher.setAuthTag(Buffer.from(tag, 'base64url'));
	return Buffer.concat([decipher.update(Buffer.from(ciphertext, 'base64url')), decipher.final()]).toString('utf8');
}

function asText(value, limit = 240) {
	return String(value ?? '').replace(/[\u0000-\u001f]/g, ' ').trim().slice(0, limit);
}

export function normalizeBaiduPanPath(value, fallback = '/') {
	let path = String(value ?? '').trim() || fallback;
	if (!path.startsWith('/')) path = `/${path}`;
	path = path.replaceAll('\\', '/').replace(/\/+/g, '/');
	if (path.includes('/../') || path.endsWith('/..') || path.includes('/./') || path.endsWith('/.')) throw new Error('百度网盘路径不能包含 . 或 .. 段');
	return path.length > 1 ? path.replace(/\/$/, '') : '/';
}

export function baiduPanAuthorizationUrl({ appKey, redirectUri = 'oob', state = 'baidu-pan' }) {
	if (!String(appKey ?? '').trim()) throw new Error('BAIDU_PAN_APP_KEY 未配置');
	const query = new URLSearchParams({ response_type: 'code', client_id: String(appKey).trim(), redirect_uri: String(redirectUri).trim() || 'oob', scope: BAIDU_OAUTH_SCOPES, state: String(state ?? 'baidu-pan') });
	return `${OAUTH_BASE}/oauth/2.0/authorize?${query}`;
}

function responseMessage(body, fallback) {
	return asText(body?.errmsg ?? body?.error_description ?? body?.msg ?? fallback, 420);
}

export function createBaiduPanStorage({ appKey, secretKey, redirectUri = 'oob', ledger = null, fetchImpl = fetch, rootPath = '/', spoolDir = '', maxUploadBytes = DEFAULT_MAX_UPLOAD_BYTES, sliceBytes = DEFAULT_SLICE_BYTES }) {
	const clientId = String(appKey ?? '').trim();
	const clientSecret = String(secretKey ?? '').trim();
	const effectiveRedirectUri = String(redirectUri ?? '').trim() || 'oob';
	const key = clientSecret ? deriveKey(clientSecret) : null;
	const boundedSliceBytes = Math.max(256 * 1024, Math.min(32 * 1024 * 1024, Number(sliceBytes) || DEFAULT_SLICE_BYTES));
	const boundedMaxUploadBytes = Math.max(1, Number(maxUploadBytes) || DEFAULT_MAX_UPLOAD_BYTES);
	let refreshInFlight = null;

	function ensureConfigured() {
		if (!clientId || !clientSecret) throw new Error('百度网盘 AppKey/SecretKey 未配置');
		if (!ledger?.getBaiduPanOAuthToken || !ledger?.saveBaiduPanOAuthToken) throw new Error('百度网盘 OAuth 存储未配置');
	}

	async function tokenRequest(params) {
		ensureConfigured();
		const query = new URLSearchParams({ client_id: clientId, client_secret: clientSecret, ...params });
		const response = await fetchImpl(`${OAUTH_BASE}/oauth/2.0/token?${query}`, { signal: AbortSignal.timeout(15_000), headers: { accept: 'application/json' } });
		let body; try { body = await response.json(); } catch { throw new Error(`百度网盘 OAuth 返回无效响应（HTTP ${response.status}）`); }
		if (!response.ok || body?.error || body?.errno) throw new Error(`百度网盘 OAuth 失败：${responseMessage(body, `HTTP ${response.status}`)}`);
		if (!body?.access_token) throw new Error('百度网盘 OAuth 未返回 access_token');
		return body;
	}

	async function deviceCode() {
		ensureConfigured();
		const query = new URLSearchParams({ client_id: clientId, response_type: 'device_code', scope: BAIDU_OAUTH_SCOPES });
		const response = await fetchImpl(`${OAUTH_BASE}/oauth/2.0/device/code?${query}`, { signal: AbortSignal.timeout(15_000), headers: { accept: 'application/json' } });
		let body; try { body = await response.json(); } catch { throw new Error(`百度网盘设备授权响应无效（HTTP ${response.status}）`); }
		if (!response.ok || body?.error || body?.errno) throw new Error(`百度网盘设备授权失败：${responseMessage(body, `HTTP ${response.status}`)}`);
		if (!body?.device_code) throw new Error('百度网盘设备授权未返回 device_code');
		return { device_code: body.device_code, user_code: body.user_code ?? null, verification_url: body.verification_url ?? body.verification_uri ?? null, expires_in: Number(body.expires_in) || null, interval: Number(body.interval) || 5 };
	}

	async function saveToken(data, fallbackRefreshToken = '', fallbackRefreshExpiresAt = null) {
		const accessToken = String(data?.access_token ?? '').trim();
		const refreshToken = String(data?.refresh_token ?? fallbackRefreshToken ?? '').trim();
		if (!accessToken || !refreshToken) throw new Error('百度网盘 OAuth 未返回完整 token');
		const expiresIn = Math.max(60, Number(data?.expires_in) || 30 * 24 * 3600);
		const refreshExpiresAt = fallbackRefreshExpiresAt || (Number(data?.refresh_token_expires_in) > 0 ? new Date(Date.now() + Number(data.refresh_token_expires_in) * 1000).toISOString() : new Date(Date.now() + 10 * 365 * 24 * 3600 * 1000).toISOString());
		await ledger.saveBaiduPanOAuthToken({ accessCiphertext: encrypt(accessToken, key), refreshCiphertext: encrypt(refreshToken, key), accessExpiresAt: new Date(Date.now() + expiresIn * 1000).toISOString(), refreshExpiresAt, scopes: String(data?.scope ?? BAIDU_OAUTH_SCOPES) });
	}

	async function exchangeAuthorizationCode(code, requestedRedirectUri = effectiveRedirectUri) {
		const authorizationCode = String(code ?? '').trim();
		if (!authorizationCode) throw new Error('百度网盘 authorization code 不能为空');
		await saveToken(await tokenRequest({ grant_type: 'authorization_code', code: authorizationCode, redirect_uri: String(requestedRedirectUri || 'oob') }));
		return status();
	}

	async function exchangeDeviceCode(code) {
		const deviceCodeValue = String(code ?? '').trim();
		if (!deviceCodeValue) throw new Error('百度网盘 device_code 不能为空');
		await saveToken(await tokenRequest({ grant_type: 'device_token', code: deviceCodeValue }));
		return status();
	}

	async function bootstrapRefreshToken(refreshTokenValue) {
		const refresh = String(refreshTokenValue ?? '').trim();
		if (!refresh) throw new Error('百度网盘 refresh_token 不能为空');
		await saveToken(await tokenRequest({ grant_type: 'refresh_token', refresh_token: refresh }), refresh);
		return status();
	}

	async function refreshToken(record) {
		const refresh = decrypt(record.refresh_ciphertext, key);
		const data = await tokenRequest({ grant_type: 'refresh_token', refresh_token: refresh });
		await saveToken(data, refresh, record.refresh_expires_at);
		return data.access_token;
	}

	async function accessToken({ forceRefresh = false } = {}) {
		ensureConfigured();
		const record = await ledger.getBaiduPanOAuthToken();
		if (!record) throw new Error('尚未保存百度网盘 OAuth 授权');
		const expiry = Date.parse(record.access_expires_at ?? '');
		if (!forceRefresh && Number.isFinite(expiry) && expiry > Date.now() + ACCESS_TOKEN_SKEW_MS) return decrypt(record.access_ciphertext, key);
		if (!refreshInFlight) refreshInFlight = refreshToken(record).finally(() => { refreshInFlight = null; });
		return refreshInFlight;
	}

	async function status() {
		if (!clientId || !clientSecret || !ledger?.getBaiduPanOAuthToken) return { configured: false, app_configured: Boolean(clientId && clientSecret), authorized: false, access_expires_at: null, refresh_expires_at: null, scopes: '' };
		const record = await ledger.getBaiduPanOAuthToken();
		return { configured: true, app_configured: true, authorized: Boolean(record), access_expires_at: record?.access_expires_at ?? null, refresh_expires_at: record?.refresh_expires_at ?? null, scopes: record?.scopes ?? '' };
	}

	async function request(path, { method = 'GET', params = {}, body, raw = false, retry = true } = {}) {
		const base = String(path).startsWith('http') ? String(path) : `${API_BASE}${path}`;
		const send = async (token) => {
			const requestBody = body === undefined || body instanceof URLSearchParams || body instanceof FormData || typeof body === 'string' || Buffer.isBuffer(body)
				? body
				: new URLSearchParams(body);
			const contentType = body instanceof FormData ? {} : body === undefined ? {} : typeof body === 'string' ? { 'content-type': 'application/json' } : { 'content-type': 'application/x-www-form-urlencoded' };
			return fetchImpl(`${base}${base.includes('?') ? '&' : '?'}${new URLSearchParams({ ...params, access_token: token })}`, { method, headers: { accept: raw ? '*/*' : 'application/json', ...contentType }, body: requestBody, signal: AbortSignal.timeout(30_000) });
		};
		let response = await send(await accessToken());
		if ((response.status === 401 || response.status === 403) && retry) response = await send(await accessToken({ forceRefresh: true }));
		if (raw) { if (!response.ok || !response.body) throw new Error(`百度网盘下载失败（HTTP ${response.status}）`); return response; }
		let payload; try { payload = await response.json(); } catch { throw new Error(`百度网盘 API 返回无效 JSON（HTTP ${response.status}）`); }
		if (!response.ok || Number(payload?.errno ?? 0) !== 0) throw new Error(`百度网盘 API 失败（${payload?.errno ?? response.status}）：${responseMessage(payload, `HTTP ${response.status}`)}`);
		return payload;
	}

	async function userInfo() { return request('/rest/2.0/xpan/nas', { params: { method: 'uinfo' } }); }
	async function quota() { return request('/rest/2.0/xpan/nas', { params: { method: 'quota' } }); }
	async function list({ dir = '/', order, desc, start = 0, limit = 1000, folder, web, showempty } = {}) { return request('/rest/2.0/xpan/file', { params: { method: 'list', openapi: 'xpansdk', dir: normalizeBaiduPanPath(dir), ...(order ? { order } : {}), desc: desc === undefined ? undefined : Number(desc) ? 1 : 0, start, limit: Math.min(1000, Math.max(1, Number(limit) || 1000)), ...(folder === undefined ? {} : { folder: Number(folder) ? 1 : 0 }), ...(web === undefined ? {} : { web: Number(web) ? 1 : 0 }), ...(showempty === undefined ? {} : { showempty: Number(showempty) ? 1 : 0 }) } }); }
	async function listByType(method, { dir = '/', start = 0, limit = 1000 } = {}) {
		if (!new Set(['doclist', 'imagelist', 'videolist']).has(method)) throw new Error('百度网盘文件类型不支持');
		return request('/rest/2.0/xpan/file', { params: { method, openapi: 'xpansdk', parent_path: normalizeBaiduPanPath(dir), ...(Number(start) > 0 ? { page: Math.floor(Number(start)) + 1 } : {}), num: Math.min(1000, Math.max(1, Number(limit) || 1000)) } });
	}
	async function search(query, { dir = '/', category } = {}) { return request('/rest/2.0/xpan/file', { params: { method: 'search', openapi: 'xpansdk', dir: normalizeBaiduPanPath(dir), key: asText(query, 200), ...(category === undefined ? {} : { category }) } }); }
	async function fileMeta(fsids, { extra = 0 } = {}) { const ids = Array.isArray(fsids) ? fsids : [fsids]; return request('/rest/2.0/xpan/multimedia', { params: { method: 'filemetas', fsids: JSON.stringify(ids.map(Number).filter(Number.isFinite)), dlink: 1, extra } }); }
	async function listAll({ path = '/', recursion = 1 } = {}) { return request('/rest/2.0/xpan/multimedia', { params: { method: 'listall', path: normalizeBaiduPanPath(path), recursion: Number(recursion) ? 1 : 0 } }); }
	async function semanticSearch(query, options = {}) { return request('/xpan/unisearch', { method: 'POST', params: { query: asText(query, 200), scene: 'mcpserver', ...(options.dir ? { dirs: JSON.stringify([{ path: normalizeBaiduPanPath(options.dir) }]) } : {}), ...(options.category ? { category: JSON.stringify(options.category) } : {}), ...(options.num ? { num: Math.min(100, Math.max(1, Number(options.num) || 20)) } : {}) }, body: '{}' }); }

	async function uploadReadable({ readable, fileName, size, remotePath = rootPath, rtype = 1 }) {
		if (!readable || typeof readable[Symbol.asyncIterator] !== 'function') throw new Error('百度网盘上传需要可读流');
		const bytes = Number(size); if (!Number.isFinite(bytes) || bytes <= 0) throw new Error('百度网盘上传需要有效文件大小');
		if (bytes > boundedMaxUploadBytes) throw new Error(`百度网盘文件超过 ${Math.floor(boundedMaxUploadBytes / 1024 / 1024)} MiB 上限`);
		const spoolRoot = spoolDir || tmpdir();
		await mkdir(spoolRoot, { recursive: true });
		const dir = await mkdtemp(join(spoolRoot, 'baidu-pan-'));
		const localPath = join(dir, 'payload.bin');
		try {
			let total = 0;
			const output = createWriteStream(localPath);
			for await (const chunk of readable) { const value = Buffer.from(chunk); total += value.length; if (total > boundedMaxUploadBytes) { output.destroy(); throw new Error(`百度网盘上传超过 ${Math.floor(boundedMaxUploadBytes / 1024 / 1024)} MiB 上限`); } if (!output.write(value)) await new Promise((resolve, reject) => { output.once('drain', resolve); output.once('error', reject); }); }
			await new Promise((resolve, reject) => { output.end(resolve); output.once('error', reject); });
			if (total !== bytes) throw new Error(`百度网盘上传大小不一致：声明 ${bytes}，实际 ${total}`);
			const blocks = []; const handle = createReadStream(localPath, { highWaterMark: boundedSliceBytes });
			for await (const chunk of handle) blocks.push(Buffer.from(chunk));
			const blockList = blocks.map((part) => createHash('md5').update(part).digest('hex'));
			const path = normalizeBaiduPanPath(remotePath, '/');
			const precreate = await request('/rest/2.0/xpan/file', { method: 'POST', params: { method: 'precreate' }, body: new URLSearchParams({ path, size: String(bytes), isdir: '0', autoinit: '1', rtype: String(Number(rtype) || 1), block_list: JSON.stringify(blockList) }) });
			if (Number(precreate.return_type) === 2) return { kind: 'baidu_pan', path, fsId: null, filename: String(fileName || path.split('/').pop() || 'file'), rapid_upload: true };
			const needed = Array.isArray(precreate.block_list) && precreate.block_list.length ? precreate.block_list : blocks.map((_, index) => index);
			const uploadedMd5 = [];
			for (const index of needed) {
				const part = blocks[Number(index)];
				if (!part) throw new Error(`百度网盘预创建返回了无效分片序号 ${index}`);
				const form = new FormData(); form.set('file', new Blob([part]), 'slice');
				const result = await request('https://d.pcs.baidu.com/rest/2.0/pcs/superfile2', { method: 'POST', params: { method: 'upload', type: 'tmpfile', path, uploadid: precreate.uploadid, partseq: String(index) }, body: form });
				uploadedMd5[index] = result.md5 || createHash('md5').update(part).digest('hex');
			}
			const finished = await request('/rest/2.0/xpan/file', { method: 'POST', params: { method: 'create' }, body: new URLSearchParams({ path, size: String(bytes), isdir: '0', uploadid: precreate.uploadid, rtype: String(Number(rtype) || 1), block_list: JSON.stringify(blocks.map((part, index) => uploadedMd5[index] || createHash('md5').update(part).digest('hex'))) }) });
			return { kind: 'baidu_pan', path: finished.path || path, fsId: finished.fs_id ?? null, filename: finished.server_filename || String(fileName || path.split('/').pop() || 'file'), rapid_upload: false, md5: finished.md5 ?? null };
		} finally { await rm(dir, { recursive: true, force: true }).catch(() => {}); }
	}

	async function download(fsId) { const meta = await fileMeta([fsId], { extra: 0 }); const dlink = meta?.list?.[0]?.dlink; if (!dlink) throw new Error('百度网盘未返回 dlink'); return request(dlink, { raw: true }); }
	async function createShareLink(fsids, { period = 7, password = '' } = {}) { const ids = (Array.isArray(fsids) ? fsids : [fsids]).map((value) => String(value)); if (!ids.length || ids.length > 100) throw new Error('分享文件数量必须为 1–100'); return request('/rest/2.0/xpan/share', { method: 'POST', params: { method: 'create' }, body: new URLSearchParams({ fsid_list: JSON.stringify(ids), period: String(Math.max(0, Number(period) || 7)), pwd: asText(password, 4) }) }); }
	async function manage(opera, filelist, { async = 1, ondup } = {}) {
		return request('/rest/2.0/xpan/file', { method: 'POST', params: { method: 'filemanager', opera }, body: new URLSearchParams({ async: String(async), filelist: JSON.stringify(filelist), ...(ondup ? { ondup } : {}) }) });
	}
	function copyMoveItem(from, to, name) { return { path: normalizeBaiduPanPath(from), dest: normalizeBaiduPanPath(to), ...(asText(name, 255) ? { newname: asText(name, 255) } : {}) }; }

	return { status, authorizationUrl: (state) => baiduPanAuthorizationUrl({ appKey: clientId, redirectUri: effectiveRedirectUri, state }), deviceCode, exchangeAuthorizationCode, exchangeDeviceCode, bootstrapRefreshToken, refresh: async () => { ensureConfigured(); const record = await ledger.getBaiduPanOAuthToken(); if (!record) throw new Error('尚未保存百度网盘 OAuth 授权'); await refreshToken(record); return status(); }, userInfo, quota, list, listByType, fileDocList: (options) => listByType('doclist', options), fileImageList: (options) => listByType('imagelist', options), fileVideoList: (options) => listByType('videolist', options), listAll, search, semanticSearch, fileMeta, uploadReadable, download, createShareLink, mkdir: (path) => request('/rest/2.0/xpan/file', { method: 'POST', params: { method: 'create' }, body: new URLSearchParams({ path: normalizeBaiduPanPath(path), isdir: '1', rtype: '1' }) }), copy: (from, to, name) => manage('copy', [copyMoveItem(from, to, name)], { ondup: 'newcopy' }), move: (from, to, name) => manage('move', [copyMoveItem(from, to, name)]), rename: (path, name) => manage('rename', [{ path: normalizeBaiduPanPath(path), newname: asText(name, 255) }]), remove: (path) => manage('delete', [normalizeBaiduPanPath(path)], { async: 1 }) };
}
