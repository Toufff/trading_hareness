import { createHash } from 'node:crypto';
import { Readable } from 'node:stream';
import { decryptSecret, encryptSecret } from './secretbox.mjs';

const FEISHU_API_BASE = 'https://open.feishu.cn/open-apis';
const TOKEN_PATH = '/authen/v2/oauth/token';
const ACCESS_TOKEN_SKEW_MS = 60_000;
const REQUIRED_RELAY_SCOPES = ['auth:user.id:read', 'im:chat:readonly', 'im:message', 'im:message.group_msg', 'im:message.group_msg:get_as_user', 'im:resource', 'offline_access'];
const INVALID_TOKEN_MESSAGE = '保存的用户飞书凭据格式无效，请重新授权';

function deriveKey(appSecret) {
	return createHash('sha256').update(`feishu-user-oauth:${appSecret}`).digest();
}

function encrypt(value, key) {
	return encryptSecret(value, key);
}

function decrypt(value, key) {
	return decryptSecret(value, key, INVALID_TOKEN_MESSAGE);
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

function auditScopes(value, required = REQUIRED_RELAY_SCOPES) {
	const granted = new Set(safeScopes(value).split(/\s+/).map((scope) => scope.trim()).filter(Boolean));
	const missing = required.filter((scope) => !granted.has(scope));
	return {
		required_scopes: required,
		granted_scopes: [...granted],
		missing_scopes: missing,
		// A refresh response may omit `scope`.  Absence is therefore unknown,
		// rather than proof that the authorization was revoked.
		verified: granted.size ? missing.length === 0 : null,
	};
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

	async function saveToken(data, { fallbackRefreshToken = '', fallbackRefreshExpiresAt = '', fallbackScopes = '' } = {}) {
		const refreshToken = String(data.refresh_token ?? fallbackRefreshToken ?? '');
		if (!refreshToken) throw new Error('飞书用户授权未返回 refresh_token');
		const refreshLifetime = data.refresh_token_expires_in ?? data.refresh_expires_in;
		const refreshExpiresAt = refreshLifetime ? asExpiry(refreshLifetime, 30 * 24 * 3600) : fallbackRefreshExpiresAt || asExpiry(null, 30 * 24 * 3600);
		const scopes = safeScopes(data.scope) || safeScopes(fallbackScopes);
		await ledger.saveFeishuUserOauthToken({
			accessCiphertext: encrypt(data.access_token, key), refreshCiphertext: encrypt(refreshToken, key),
			accessExpiresAt: asExpiry(data.expires_in, 7200), refreshExpiresAt,
			scopes,
		});
		return { access_expires_at: asExpiry(data.expires_in, 7200), refresh_expires_at: refreshExpiresAt, scopes };
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
		await saveToken(data, { fallbackRefreshToken: refreshToken, fallbackRefreshExpiresAt: record.refresh_expires_at, fallbackScopes: record.scopes });
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

	async function status() {
		const record = await ledger.getFeishuUserOauthToken();
		return record
			? { configured: true, access_expires_at: record.access_expires_at, refresh_expires_at: record.refresh_expires_at, scopes: record.scopes ?? '', scope_audit: auditScopes(record.scopes) }
			: { configured: false, scope_audit: { required_scopes: REQUIRED_RELAY_SCOPES, granted_scopes: [], missing_scopes: REQUIRED_RELAY_SCOPES, verified: false } };
	}

	async function forceRefresh() {
		const record = await ledger.getFeishuUserOauthToken();
		if (!record) throw new Error('尚未保存飞书用户授权；请完成本机 OAuth 授权');
		await refreshToken(record);
		return status();
	}

	async function userRequest(path, { method = 'GET', params = {}, body, headers = {}, stream = false, retry = true } = {}) {
		const query = new URLSearchParams(Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== '').map(([name, value]) => [name, String(value)]));
		const normalizedMethod = String(method).toUpperCase();
		const payload = body === undefined || body === null || typeof body === 'string' || body instanceof Uint8Array ? body : JSON.stringify(body);
		// The 30s bound below guards only getting a response (the fetch() promise
		// settling). It must not also bound reading a large media body: a stream
		// response's body can legitimately take much longer than 30s for a
		// multi-hundred-MB attachment. Since the timer is cleared as soon as
		// fetchImpl() resolves or rejects, a later `response.getReadableStream()`
		// read is never tied to this AbortController and can run unbounded.
		const send = async (token) => {
			const controller = new AbortController();
			const timer = setTimeout(() => controller.abort(), 30_000);
			try {
				return await fetchImpl(`${FEISHU_API_BASE}${path}${query.size ? `?${query}` : ''}`, {
					method: normalizedMethod,
					headers: {
						authorization: `Bearer ${token}`, accept: stream ? '*/*' : 'application/json',
						...(payload !== undefined && !headers['content-type'] && !headers['Content-Type'] ? { 'content-type': 'application/json' } : {}),
						...headers,
					},
					...(payload === undefined ? {} : { body: payload }), signal: controller.signal,
				});
			} finally {
				clearTimeout(timer);
			}
		};
		let response = await send(await accessToken());
		if ((response.status === 401 || response.status === 403) && retry) response = await send(await accessToken({ forceRefresh: true }));
		if (stream) {
			if (!response.ok || !response.body) {
				let detail = '';
				const logId = response.headers.get('x-tt-logid') || response.headers.get('x-ogw-request-id');
				try {
					const errorBody = await response.clone().json();
					const code = errorBody?.code ?? errorBody?.error?.code;
					const message = errorBody?.msg ?? errorBody?.message ?? errorBody?.error?.message;
					if (code || message) detail = `：${code ? `${code} ` : ''}${String(message ?? '').slice(0, 240)}`;
				} catch { /* some resource errors return an empty/non-JSON body */ }
				throw new Error(`读取飞书消息资源失败（HTTP ${response.status}）${detail}${logId ? ` [log_id: ${logId}]` : ''}`);
			}
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
		forceRefresh,
		status,
		sourceApi: {
			chatSearch: (query) => userRequest('/im/v1/chats/search', { params: { query, page_size: 100 } }),
			chatList: (params = {}) => userRequest('/im/v1/chats', { params: { page_size: 100, ...params } }),
			messageList: (params) => userRequest('/im/v1/messages', { params }),
			messageResourceGet: ({ messageId, fileKey, type }) => userRequest(`/im/v1/messages/${encodeURIComponent(messageId)}/resources/${encodeURIComponent(fileKey)}`, {
				params: { type }, stream: true, headers: { 'content-type': 'application/json; charset=utf-8' },
			}),
		},
		userRequest,
	};
}
