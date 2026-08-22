import assert from 'node:assert/strict';
import test from 'node:test';
import { createFeishuUserOauth } from './feishu-user-oauth.mjs';

test('OAuth scope audit reports missing relay scopes and preserves scopes when refresh omits them', async () => {
	let record = null;
	let tokenCalls = 0;
	const ledger = {
		getFeishuUserOauthToken: async () => record,
		saveFeishuUserOauthToken: async (next) => {
			record = {
				token_key: 'default', access_ciphertext: next.accessCiphertext, refresh_ciphertext: next.refreshCiphertext,
				access_expires_at: next.accessExpiresAt, refresh_expires_at: next.refreshExpiresAt, scopes: next.scopes,
			};
		},
	};
	const oauth = createFeishuUserOauth({
		appId: 'app', appSecret: 'secret', redirectUri: 'http://localhost/callback', ledger,
		fetchImpl: async (url) => {
			if (url.includes('/authen/v2/oauth/token')) {
				tokenCalls += 1;
				return new Response(JSON.stringify({ data: tokenCalls === 1
					? { access_token: 'access_1', refresh_token: 'refresh_1', expires_in: 3600, refresh_token_expires_in: 86_400, scope: 'auth:user.id:read im:chat:readonly im:message im:message.group_msg im:message.group_msg:get_as_user im:resource offline_access' }
					: { access_token: 'access_2', refresh_token: 'refresh_2', expires_in: 3600, refresh_token_expires_in: 86_400 },
				}));
			}
			return new Response(JSON.stringify({ code: 0, data: { items: [] } }));
		},
	});
	await oauth.bootstrapRefreshToken('initial_refresh');
	assert.equal((await oauth.status()).scope_audit.verified, true);
	record.access_expires_at = new Date(0).toISOString();
	await oauth.sourceApi.messageList({ container_id_type: 'chat', container_id: 'oc_source' });
	const refreshed = await oauth.status();
	assert.equal(tokenCalls, 2);
	assert.equal(refreshed.scope_audit.verified, true);
	assert.match(refreshed.scopes, /im:message\.group_msg/);
});

test('OAuth status fails relay preflight only when saved scopes are demonstrably incomplete', async () => {
	const ledger = {
		getFeishuUserOauthToken: async () => ({ scopes: 'auth:user.id:read im:chat:readonly offline_access' }),
	};
	const oauth = createFeishuUserOauth({ appId: 'app', appSecret: 'secret', redirectUri: 'http://localhost/callback', ledger, fetchImpl: async () => { throw new Error('not used'); } });
	const status = await oauth.status();
	assert.equal(status.scope_audit.verified, false);
	assert.deepEqual(status.scope_audit.missing_scopes, ['im:message', 'im:message.group_msg', 'im:message.group_msg:get_as_user', 'im:resource']);
});

test('resource failures preserve Feishu error code for diagnosis', async () => {
	let tokenCalls = 0;
	let resourceContentType = '';
	let record = null;
	const ledger = {
		getFeishuUserOauthToken: async () => record,
		saveFeishuUserOauthToken: async (next) => { record = { access_ciphertext: next.accessCiphertext, refresh_ciphertext: next.refreshCiphertext, access_expires_at: next.accessExpiresAt, refresh_expires_at: next.refreshExpiresAt, scopes: next.scopes }; },
	};
	const oauth = createFeishuUserOauth({
		appId: 'app', appSecret: 'secret', redirectUri: 'http://localhost/callback', ledger,
		fetchImpl: async (url, options = {}) => url.includes('/authen/v2/oauth/token')
			? new Response(JSON.stringify({ data: { access_token: `access_${++tokenCalls}`, refresh_token: `refresh_${tokenCalls}`, expires_in: 3600, scope: 'auth:user.id:read im:chat:readonly im:message im:message.group_msg im:message.group_msg:get_as_user im:resource offline_access' } }))
			: (resourceContentType = options.headers?.['content-type'] ?? '', new Response(JSON.stringify({ code: 99991679, msg: 'permission denied' }), { status: 401 })),
	});
	await oauth.bootstrapRefreshToken('initial_refresh');
	await assert.rejects(() => oauth.sourceApi.messageResourceGet({ messageId: 'om_1', fileKey: 'img_1', type: 'image' }), /读取飞书消息资源失败（HTTP 401）：99991679 permission denied/);
	assert.equal(resourceContentType, 'application/json; charset=utf-8');
});
