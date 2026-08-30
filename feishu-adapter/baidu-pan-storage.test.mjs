import test from 'node:test';
import assert from 'node:assert/strict';
import { Readable } from 'node:stream';
import { baiduPanAuthorizationUrl, createBaiduPanStorage, normalizeBaiduPanPath } from './baidu-pan-storage.mjs';

function ledgerStub() {
	let record = null;
	return {
		async getBaiduPanOAuthToken() { return record; },
		async saveBaiduPanOAuthToken(value) { record = { access_ciphertext: value.accessCiphertext, refresh_ciphertext: value.refreshCiphertext, access_expires_at: value.accessExpiresAt, refresh_expires_at: value.refreshExpiresAt, scopes: value.scopes }; },
		get record() { return record; },
	};
}

test('normalizes safe paths and rejects traversal', () => {
	assert.equal(normalizeBaiduPanPath('foo//bar/'), '/foo/bar');
	assert.throws(() => normalizeBaiduPanPath('/foo/../bar'), /不能包含/);
});

test('builds OAuth URL without exposing a secret', () => {
	const url = new URL(baiduPanAuthorizationUrl({ appKey: 'app-key', redirectUri: 'oob', state: 'test' }));
	assert.equal(url.searchParams.get('client_id'), 'app-key');
	assert.equal(url.searchParams.get('scope'), 'basic,netdisk');
	assert.equal(url.searchParams.get('redirect_uri'), 'oob');
	assert.equal(url.searchParams.get('state'), 'test');
});

test('exchanges OAuth code, lists files and refreshes on unauthorized', async () => {
	const ledger = ledgerStub();
	let calls = [];
	let apiCalls = 0;
	const fetchImpl = async (url, options = {}) => {
		calls.push({ url: String(url), options });
		if (String(url).startsWith('https://openapi.baidu.com')) return new Response(JSON.stringify({ access_token: 'access-1', refresh_token: 'refresh-1', expires_in: 3600, scope: 'basic,netdisk' }), { status: 200, headers: { 'content-type': 'application/json' } });
		apiCalls += 1;
		if (apiCalls === 1) return new Response(JSON.stringify({ errno: 110, errmsg: 'expired' }), { status: 401, headers: { 'content-type': 'application/json' } });
		return new Response(JSON.stringify({ errno: 0, list: [{ fs_id: 1, server_filename: 'a.txt' }] }), { status: 200, headers: { 'content-type': 'application/json' } });
	};
	const storage = createBaiduPanStorage({ appKey: 'app-key', secretKey: 'secret', ledger, fetchImpl });
	const status = await storage.exchangeAuthorizationCode('code-1');
	assert.equal(status.authorized, true);
	const result = await storage.list({ dir: '/apps/demo' });
	assert.equal(result.list[0].fs_id, 1);
	assert.equal(apiCalls, 2);
	assert.match(calls[0].url, /grant_type=authorization_code/);
	assert.match(calls.at(-1).url, /method=list/);
	assert.equal(calls.at(-1).options.headers.authorization, undefined);
});

test('uploads a small readable through precreate, chunk and create', async () => {
	const ledger = ledgerStub();
	let phase = 0;
	const fetchImpl = async (url, options = {}) => {
		if (String(url).startsWith('https://openapi.baidu.com')) return new Response(JSON.stringify({ access_token: 'access-1', refresh_token: 'refresh-1', expires_in: 3600 }), { status: 200 });
		phase += 1;
		if (String(url).includes('superfile2')) {
			assert.equal(options.method, 'POST');
			assert.equal(options.body instanceof FormData, true);
			return new Response(JSON.stringify({ errno: 0, md5: 'chunk-md5' }), { status: 200 });
		}
		const parsed = new URL(url);
		if (parsed.searchParams.get('method') === 'precreate') return new Response(JSON.stringify({ errno: 0, return_type: 1, uploadid: 'upload-1', block_list: [0] }), { status: 200 });
		if (parsed.searchParams.get('method') === 'create') return new Response(JSON.stringify({ errno: 0, fs_id: 99, path: '/apps/demo/a.txt', server_filename: 'a.txt' }), { status: 200 });
		throw new Error(`unexpected ${url}`);
	};
	const storage = createBaiduPanStorage({ appKey: 'app-key', secretKey: 'secret', ledger, fetchImpl, sliceBytes: 256 * 1024 });
	await storage.exchangeAuthorizationCode('code-1');
	const result = await storage.uploadReadable({ readable: Readable.from([Buffer.from('hello')]), fileName: 'a.txt', size: 5, remotePath: '/apps/demo/a.txt' });
	assert.equal(result.fsId, 99);
	assert.equal(phase, 3);
});

test('uses official file manager query and JSON filelist shape', async () => {
	const ledger = ledgerStub();
	const seen = [];
	const fetchImpl = async (url) => {
		seen.push(String(url));
		if (String(url).startsWith('https://openapi.baidu.com')) return new Response(JSON.stringify({ access_token: 'a', refresh_token: 'r', expires_in: 3600 }), { status: 200 });
		return new Response(JSON.stringify({ errno: 0 }), { status: 200 });
	};
	const storage = createBaiduPanStorage({ appKey: 'app', secretKey: 'secret', ledger, fetchImpl });
	await storage.exchangeAuthorizationCode('c');
	await storage.copy('/apps/a.txt', '/apps/archive', 'a.txt');
	const url = new URL(seen.at(-1));
	assert.equal(url.searchParams.get('method'), 'filemanager');
	assert.equal(url.searchParams.get('opera'), 'copy');
});

test('uses the official quota endpoint and optional checks', async () => {
	const ledger = ledgerStub();
	const urls = [];
	const fetchImpl = async (url) => {
		urls.push(String(url));
		if (String(url).startsWith('https://openapi.baidu.com')) return new Response(JSON.stringify({ access_token: 'a', refresh_token: 'r', expires_in: 3600 }), { status: 200 });
		return new Response(JSON.stringify({ errno: 0, total: 100, used: 1, free: 99 }), { status: 200 });
	};
	const storage = createBaiduPanStorage({ appKey: 'app', secretKey: 'secret', ledger, fetchImpl });
	await storage.exchangeAuthorizationCode('c');
	const result = await storage.quota({ checkFree: true, checkExpire: true });
	assert.equal(result.total, 100);
	const quotaUrl = new URL(urls.at(-1));
	assert.equal(quotaUrl.pathname, '/api/quota');
	assert.equal(quotaUrl.searchParams.get('checkfree'), '1');
	assert.equal(quotaUrl.searchParams.get('checkexpire'), '1');
});
