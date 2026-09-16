import test from 'node:test';
import assert from 'node:assert/strict';
import { Readable } from 'node:stream';
import { baiduPanAuthorizationUrl, createBaiduPanStorage, isBaiduPanAuthorizationPending, normalizeBaiduPanPath } from './baidu-pan-storage.mjs';

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

test('keeps the OAuth error code so a device-code poller can tell pending from fatal', async () => {
	// Baidu answers a not-yet-confirmed device code with HTTP 400 and a
	// description ("User has not yet completed the authorization") that carries
	// no "pending" wording; responseMessage() prefers that description, so the
	// code has to survive on the error object or the poller exits immediately.
	const pendingResponses = [
		{ error: 'authorization_pending', error_description: 'User has not yet completed the authorization' },
		{ error_description: 'User has not yet completed the authorization' },
	];
	for (const body of pendingResponses) {
		const storage = createBaiduPanStorage({
			appKey: 'app-key', secretKey: 'secret', ledger: ledgerStub(),
			fetchImpl: async () => new Response(JSON.stringify(body), { status: 400, headers: { 'content-type': 'application/json' } }),
		});
		const error = await storage.exchangeDeviceCode('device-1').then(() => null, (thrown) => thrown);
		assert.ok(error, 'pending authorization must reject');
		assert.equal(isBaiduPanAuthorizationPending(error), true, `expected pending for ${JSON.stringify(body)}`);
		assert.match(error.message, /not yet completed/);
	}

	// A real failure must not be mistaken for a pending approval and retried
	// until the device code expires.
	const fatal = createBaiduPanStorage({
		appKey: 'app-key', secretKey: 'secret', ledger: ledgerStub(),
		fetchImpl: async () => new Response(JSON.stringify({ error: 'expired_token', error_description: 'device code expired' }), { status: 400, headers: { 'content-type': 'application/json' } }),
	});
	const error = await fatal.exchangeDeviceCode('device-1').then(() => null, (thrown) => thrown);
	assert.equal(error.oauthCode, 'expired_token');
	assert.equal(isBaiduPanAuthorizationPending(error), false);
	assert.equal(isBaiduPanAuthorizationPending(new Error('百度网盘 OAuth 失败：invalid client')), false);
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

test('follows dlink with the refreshed OAuth token for readback verification', async () => {
	const ledger = ledgerStub();
	const calls = [];
	const fetchImpl = async (url, options = {}) => {
		calls.push({ url: String(url), options });
		if (String(url).startsWith('https://openapi.baidu.com')) return new Response(JSON.stringify({ access_token: 'access-read', refresh_token: 'refresh-read', expires_in: 3600 }), { status: 200 });
		const parsed = new URL(url);
		if (parsed.searchParams.get('method') === 'filemetas') return new Response(JSON.stringify({ errno: 0, list: [{ dlink: 'https://d.pcs.baidu.com/file?x=1' }] }), { status: 200 });
		assert.equal(parsed.hostname, 'd.pcs.baidu.com');
		assert.equal(parsed.searchParams.get('access_token'), 'access-read');
		assert.equal(options.redirect, 'follow');
		return new Response('archive-bytes', { status: 200 });
	};
	const storage = createBaiduPanStorage({ appKey: 'app', secretKey: 'secret', ledger, fetchImpl });
	await storage.exchangeAuthorizationCode('c');
	const response = await storage.download(42);
	assert.equal(await response.text(), 'archive-bytes');
	const downloadCall = calls.find((call) => call.url.startsWith('https://d.pcs.baidu.com/'));
	assert.ok(downloadCall);
	assert.match(downloadCall.url, /access_token=access-read/);
});

test('falls back to directory walking when listall is unavailable', async () => {
	const ledger = ledgerStub();
	const urls = [];
	const fetchImpl = async (url) => {
		urls.push(String(url));
		if (String(url).startsWith('https://openapi.baidu.com')) return new Response(JSON.stringify({ access_token: 'a', refresh_token: 'r', expires_in: 3600 }), { status: 200 });
		const parsed = new URL(url);
		if (parsed.searchParams.get('method') === 'listall') return new Response(JSON.stringify({ errno: 20020, error_code: 20020 }), { status: 200 });
		if (parsed.searchParams.get('method') === 'list' && parsed.searchParams.get('dir') === '/') return new Response(JSON.stringify({ errno: 0, list: [{ path: '/child', isdir: 1 }, { path: '/root.txt', isdir: 0 }], has_more: 0 }), { status: 200 });
		if (parsed.searchParams.get('method') === 'list' && parsed.searchParams.get('dir') === '/child') return new Response(JSON.stringify({ errno: 0, list: [{ path: '/child/nested.txt', isdir: 0 }], has_more: 0 }), { status: 200 });
		throw new Error(`unexpected ${url}`);
	};
	const storage = createBaiduPanStorage({ appKey: 'app', secretKey: 'secret', ledger, fetchImpl });
	await storage.exchangeAuthorizationCode('c');
	const result = await storage.listAll({ path: '/', recursion: 1, limit: 10 });
	assert.deepEqual(result.list.map((item) => item.path), ['/child', '/root.txt', '/child/nested.txt']);
	assert.equal(result.fallback, 'directory_walk');
	assert.equal(new URL(urls[1]).searchParams.get('method'), 'listall');
});

test('uploadFile streams slices through the located upload server and verifies the created size', async () => {
	const { mkdtemp, writeFile, rm } = await import('node:fs/promises');
	const { tmpdir } = await import('node:os');
	const { join } = await import('node:path');
	const { createHash, randomBytes } = await import('node:crypto');
	const dir = await mkdtemp(join(tmpdir(), 'pan-upload-'));
	const MiB = 1024 * 1024;
	const payload = randomBytes(9 * MiB + 123);
	await writeFile(join(dir, 'file.bin'), Buffer.concat([randomBytes(100), payload]));
	const expectedBlocks = [0, 1, 2].map((i) => createHash('md5').update(payload.subarray(i * 4 * MiB, Math.min(payload.length, (i + 1) * 4 * MiB))).digest('hex'));
	const ledger = ledgerStub();
	const seen = [];
	let failedOnce = false;
	const json = (body) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
	const fetchImpl = async (url, options = {}) => {
		const u = new URL(String(url));
		if (u.hostname === 'openapi.baidu.com') return json({ access_token: 'a', refresh_token: 'r', expires_in: 3600 });
		const method = u.searchParams.get('method');
		seen.push(`${u.hostname}:${method}`);
		if (method === 'precreate') {
			const form = new URLSearchParams(options.body);
			assert.deepEqual(JSON.parse(form.get('block_list')), expectedBlocks);
			assert.equal(form.get('size'), String(payload.length));
			assert.equal(form.get('rtype'), '3');
			return json({ errno: 0, uploadid: 'U1', block_list: [0, 1, 2] });
		}
		if (method === 'locateupload') return json({ error_code: 0, servers: [{ server: 'https://c3.pcs.baidu.com' }, { server: 'http://insecure.example' }], bak_servers: [{ server: 'https://c.pcs.baidu.com' }] });
		if (method === 'upload') {
			const index = Number(u.searchParams.get('partseq'));
			if (index === 1 && !failedOnce) { failedOnce = true; throw new Error('network reset'); }
			const part = Buffer.from(await options.body.get('file').arrayBuffer());
			assert.ok(part.length <= 4 * MiB);
			return json({ md5: createHash('md5').update(part).digest('hex') });
		}
		if (method === 'create') return json({ errno: 0, fs_id: 42, path: '/apps/t/file.bin', size: payload.length });
		throw new Error(`unexpected ${u}`);
	};
	try {
		const storage = createBaiduPanStorage({ appKey: 'k', secretKey: 's', ledger, fetchImpl });
		await storage.exchangeAuthorizationCode('code');
		const realTimeout = globalThis.setTimeout;
		globalThis.setTimeout = (fn) => realTimeout(fn, 0);
		let result;
		try { result = await storage.uploadFile({ localPath: join(dir, 'file.bin'), remotePath: '/apps/t/file.bin', start: 100, length: payload.length, sliceBytes: 4 * MiB }); }
		finally { globalThis.setTimeout = realTimeout; }
		assert.equal(result.fs_id, 42);
		assert.equal(result.size, payload.length);
		assert.equal(result.md5, createHash('md5').update(payload).digest('hex'));
		assert.deepEqual(seen.filter((s) => s.endsWith(':upload')).map((s) => s.split(':')[0]), ['c3.pcs.baidu.com', 'c3.pcs.baidu.com', 'c.pcs.baidu.com', 'c3.pcs.baidu.com']);
		assert.ok(seen.indexOf('d.pcs.baidu.com:locateupload') < seen.indexOf('c3.pcs.baidu.com:upload'));
		await assert.rejects(storage.uploadFile({ localPath: join(dir, 'file.bin'), remotePath: '/apps/t/x', sliceBytes: 4 * MiB, length: 4 * MiB * 1025 }), /最多 1024 个分片/);
	} finally { await rm(dir, { recursive: true, force: true }); }
});
