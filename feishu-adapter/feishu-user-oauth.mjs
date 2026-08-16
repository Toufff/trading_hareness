import { createCipheriv, createDecipheriv, createHash, randomBytes } from 'node:crypto';
import { Readable } from 'node:stream';

const FEISHU_API_BASE = 'https://open.feishu.cn/open-apis';
const TOKEN_PATH = '/authen/v2/oauth/token';
const ACCESS_TOKEN_SKEW_MS = 60_000;

function deriveKey(appSecret) {
	return createHash('sha256').update(`feishu-user-oauth:${appSecret}`).digest();
}

function encrypt(value, key) {
	const iv = randomBytes(12);
	const cipher = createCipheriv('aes-256-gcm', key, iv);
	const ciphertext = Buffer.concat([cipher.update(value, 'utf8'), cipher.final()]);
	return `v1.${iv.toString('base64url')}.${cipher.getAuthTag().toString('base64url')}.${ciphertext.toString('base64url')}`;
}

function decrypt(value, key) {
	const [version, iv, tag, ciphertext] = String(value ?? '').split('.');
	if (version !== 'v1' || !iv || !tag || !ciphertext) throw new Error('保存的用户飞书凭据格式无效，请重新授权');
	const decipher = createDecipheriv('aes-256-gcm', key, Buffer.from(iv, 'base64url'));
	decipher.setAuthTag(Buffer.from(tag, 'base64url'));
	return Buffer.concat([decipher.update(Buffer.from(ciphertext, 'base64url')), decipher.final()]).toString('utf8');
}

function oauthError(payload, fallback) {
	return new Error(`飞书用户授权失败：${payload?.msg ?? payload?.error_description ?? fallback}`);
}

function asExpiry(value, fallbackSeconds) {
	const seconds = Number(value);
	return new Date(Date.now() + (Number.isFinite(seconds) && seconds > 0 ? seconds : fallbackSeconds) * 1000).toISOString();
}

function safeScopes(value) {
	if (Array.isArray(value)) return value.join(' ');
	return typeof value === 'string' ? value : '';
}

export function createFeishuUserOauth({ appId, appSecret, redirectUri, ledger, fetchImpl = fetch }) {
	if (!appId || !appSecret) throw new Error('FEISHU_APP_ID and FEISHU_APP_SECRET are required for user OAuth');
	const key = deriveKey(appSecret);
	let refreshInFlight = null;

	async function postToken(payload) {
		const response = await fetchImpl(`${FEISHU_API_BASE}${TOKEN_PATH}`, {
			method: 'POST', headers: { 'content-type': 'application/json', accept: 'application/json' },
			body: JSON.stringify({ client_id: appId, client_secret: appSecret, ...payload }), signal: AbortSignal.timeout(15_000),
		});
		let body;
		try { body = await response.json(); } catch { throw new Error(`飞书用户授权返回了无效响应（HTTP ${response.status}）`); }
		if (!response.ok || body?.code) throw oauthError(body, `HTTP ${response.status}`);
		const token = body?.data ?? body;
		if (!token?.access_token) throw new Error('飞书用户授权未返回 access_token');
		return token;
	}

	async function saveToken(data, { fallbackRefreshToken = '', fallbackRefreshExpiresAt = '' } = {}) {
		const refreshToken = String(data.refresh_token ?? fallbackRefreshToken ?? '');
		if (!refreshToken) throw new Error('飞书用户授权未返回 refresh_token');
		const refreshLifetime = data.refresh_token_expires_in ?? data.refresh_expires_in;
		const refreshExpiresAt = refreshLifetime ? asExpiry(refreshLifetime, 30 * 24 * 3600) : fallbackRefreshExpiresAt || asExpiry(null, 30 * 24 * 3600);
		await ledger.saveFeishuUserOauthToken({
			accessCiphertext: encrypt(data.access_token, key), refreshCiphertext: encrypt(refreshToken, key),
			accessExpiresAt: asExpiry(data.expires_in, 7200), refreshExpiresAt,
			scopes: safeScopes(data.scope),
		});
		return { access_expires_at: asExpiry(data.expires_in, 7200), refresh_expires_at: refreshExpiresAt, scopes: safeScopes(data.scope) };
	}

	async function exchangeAuthorizationCode(code, requestedRedirectUri) {
		const authorizationCode = String(code ?? '').trim();
		const effectiveRedirectUri = String(requestedRedirectUri ?? redirectUri ?? '').trim();
		if (!authorizationCode) throw new Error('authorization_code is required');
		if (!effectiveRedirectUri) throw new Error('redirect_uri is required');
		return saveToken(await postToken({ grant_type: 'authorization_code', code: authorizationCode, redirect_uri: effectiveRedirectUri }));
	}

	async function bootstrapRefreshToken(value) {
		const refreshToken = String(value ?? '').trim();
		if (!refreshToken) throw new Error('refresh_token is required');
		return saveToken(await postToken({ grant_type: 'refresh_token', refresh_token: refreshToken, redirect_uri: redirectUri }), { fallbackRefreshToken: refreshToken });
	}

	async function refreshToken(record) {
		const refreshToken = decrypt(record.refresh_ciphertext, key);
		const data = await postToken({ grant_type: 'refresh_token', refresh_token: refreshToken, redirect_uri: redirectUri });
		await saveToken(data, { fallbackRefreshToken: refreshToken, fallbackRefreshExpiresAt: record.refresh_expires_at });
		return data.access_token;
	}

	async function accessToken({ forceRefresh = false } = {}) {
		const record = await ledger.getFeishuUserOauthToken();
		if (!record) throw new Error('尚未保存飞书用户授权；请完成本机 OAuth 授权');
		const expiresAt = Date.parse(record.access_expires_at ?? '');
		if (!forceRefresh && Number.isFinite(expiresAt) && expiresAt > Date.now() + ACCESS_TOKEN_SKEW_MS) return decrypt(record.access_ciphertext, key);
		if (!refreshInFlight) {
			refreshInFlight = refreshToken(record).finally(() => { refreshInFlight = null; });
		}
		return refreshInFlight;
	}

	async function userRequest(path, { method = 'GET', params = {}, body, headers = {}, stream = false, retry = true } = {}) {
		const query = new URLSearchParams(Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== '').map(([name, value]) => [name, String(value)]));
		const normalizedMethod = String(method).toUpperCase();
		const payload = body === undefined || body === null || typeof body === 'string' || body instanceof Uint8Array ? body : JSON.stringify(body);
		const send = async (token) => fetchImpl(`${FEISHU_API_BASE}${path}${query.size ? `?${query}` : ''}`, {
			method: normalizedMethod,
			headers: {
				authorization: `Bearer ${token}`, accept: stream ? '*/*' : 'application/json',
				...(payload !== undefined && !headers['content-type'] && !headers['Content-Type'] ? { 'content-type': 'application/json' } : {}),
				...headers,
			},
			...(payload === undefined ? {} : { body: payload }), signal: AbortSignal.timeout(30_000),
		});
		let response = await send(await accessToken());
		if ((response.status === 401 || response.status === 403) && retry) response = await send(await accessToken({ forceRefresh: true }));
		if (stream) {
			if (!response.ok || !response.body) throw new Error(`读取飞书消息资源失败（HTTP ${response.status}）`);
			return { headers: Object.fromEntries(response.headers.entries()), getReadableStream: () => Readable.fromWeb(response.body) };
		}
		let responseBody;
		try { responseBody = await response.json(); } catch { throw new Error(`飞书用户 API 返回无效 JSON（HTTP ${response.status}）`); }
		if (!response.ok || responseBody?.code) throw new Error(`飞书用户 API 请求失败：${responseBody?.msg ?? `HTTP ${response.status}`}`);
		return responseBody;
	}

	return {
		exchangeAuthorizationCode,
		bootstrapRefreshToken,
		status: async () => {
			const record = await ledger.getFeishuUserOauthToken();
			return record ? { configured: true, access_expires_at: record.access_expires_at, refresh_expires_at: record.refresh_expires_at, scopes: record.scopes ?? '' } : { configured: false };
		},
		sourceApi: {
			chatSearch: (query) => userRequest('/im/v1/chats/search', { params: { query, page_size: 100 } }),
			messageList: (params) => userRequest('/im/v1/messages', { params }),
			messageResourceGet: ({ messageId, fileKey, type }) => userRequest(`/im/v1/messages/${encodeURIComponent(messageId)}/resources/${encodeURIComponent(fileKey)}`, { params: { type }, stream: true }),
		},
		userRequest,
	};
}
