import test from 'node:test';
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { mkdtemp, mkdir, readFile, rm, stat, utimes, writeFile, readdir } from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import {
	createKeyFile, decryptFile, encryptFile, keyFingerprint, listLocalBackupFiles, loadKey, planUploads,
	remotePartPaths, runFetch, runPrune, runUpload, selectLocalRemovals,
} from './stock-backup-offsite.mjs';

function fakePan() {
	const files = new Map();
	let nextId = 1;
	return {
		files,
		async uploadFile({ localPath, remotePath, start = 0, length }) {
			const whole = await readFile(localPath);
			const bytes = whole.subarray(start, length === undefined ? whole.length : start + length);
			const existing = [...files.values()].find((item) => item.path === remotePath);
			const fsId = existing?.fs_id ?? nextId++;
			files.set(fsId, { fs_id: fsId, path: remotePath, bytes: Buffer.from(bytes) });
			return { path: remotePath, fs_id: fsId, size: bytes.length, md5: 'x', rapid_upload: false };
		},
		async fileMeta(ids) { return { list: ids.map((id) => ({ fs_id: id, size: files.get(id).bytes.length })) }; },
		async list({ dir }) { return { list: [...files.values()].filter((item) => item.path.startsWith(`${dir}/`) && !item.path.slice(dir.length + 1).includes('/')).map((item) => ({ fs_id: item.fs_id, server_filename: item.path.split('/').pop() })) }; },
		async download(fsId) { return { body: Readable.from([files.get(fsId).bytes]) }; },
	};
}

async function tempRoot() { return mkdtemp(join(tmpdir(), 'stock-offsite-test-')); }

async function seedBackup(root) {
	await mkdir(join(root, '2026-09-15'), { recursive: true });
	await mkdir(join(root, '2026-09-16'), { recursive: true });
	await mkdir(join(root, 'incremental', 'quant.raw_market_observations'), { recursive: true });
	await writeFile(join(root, '2026-09-15', 'trading_hareness-2026-09-15.dump'), randomBytes(5000));
	await writeFile(join(root, '2026-09-15', 'trading_hareness-2026-09-15.dump.sha256'), 'abc  x\n');
	await writeFile(join(root, '2026-09-16', 'trading_hareness-2026-09-16.dump'), randomBytes(7000));
	await writeFile(join(root, '2026-09-16', 'trading_hareness-2026-09-16.dump.excluded-table-data.json'), '{}');
	const chunk = '20260915T160000.000000Z_20260916T120000.000000Z';
	await writeFile(join(root, 'incremental', 'quant.raw_market_observations', `${chunk}.copy.gz`), randomBytes(9000));
	await writeFile(join(root, 'incremental', 'quant.raw_market_observations', `${chunk}.json`), '{"rows":1}');
	await writeFile(join(root, 'incremental', 'quant.raw_market_observations', 'state.json'), '{"watermark":"x"}');
	await writeFile(join(root, 'incremental', 'quant.raw_market_observations', `${chunk}.copy.gz.partial`), 'half');
}

test('encryption round-trips, reports the plaintext hash and rejects a wrong key or tampering', async () => {
	const root = await tempRoot();
	try {
		const key = randomBytes(32);
		const plain = randomBytes(3 * 1024 * 1024 + 17);
		await writeFile(join(root, 'plain'), plain);
		const encrypted = await encryptFile(join(root, 'plain'), join(root, 'enc'), key);
		assert.equal(encrypted.plaintext_bytes, plain.length);
		assert.equal(encrypted.encrypted_bytes, plain.length + 6 + 12 + 16);
		assert.notDeepEqual((await readFile(join(root, 'enc'))).subarray(18, 50), plain.subarray(0, 32));
		const decrypted = await decryptFile(join(root, 'enc'), join(root, 'out'), key);
		assert.equal(decrypted.sha256, encrypted.sha256);
		assert.deepEqual(await readFile(join(root, 'out')), plain);
		await assert.rejects(decryptFile(join(root, 'enc'), join(root, 'wrong'), randomBytes(32)), /解密失败/);
		const tampered = await readFile(join(root, 'enc')); tampered[100] ^= 1; await writeFile(join(root, 'tampered'), tampered);
		await assert.rejects(decryptFile(join(root, 'tampered'), join(root, 'bad'), key), /解密失败/);
		await assert.rejects(stat(join(root, 'bad')), /ENOENT/);
	} finally { await rm(root, { recursive: true, force: true }); }
});

test('key files are created once and loaded as 32 bytes', async () => {
	const root = await tempRoot();
	try {
		const path = join(root, 'config', 'offsite.key');
		await createKeyFile(path);
		const key = await loadKey(path);
		assert.equal(key.length, 32);
		await assert.rejects(createKeyFile(path), /EEXIST/);
		assert.equal(keyFingerprint(key).length, 16);
	} finally { await rm(root, { recursive: true, force: true }); }
});

test('inventory includes dumps and chunk pairs only', async () => {
	const root = await tempRoot();
	try {
		await seedBackup(root);
		const files = await listLocalBackupFiles(root);
		assert.deepEqual(files.map((file) => file.relative), [
			'2026-09-15/trading_hareness-2026-09-15.dump',
			'2026-09-15/trading_hareness-2026-09-15.dump.sha256',
			'2026-09-16/trading_hareness-2026-09-16.dump',
			'2026-09-16/trading_hareness-2026-09-16.dump.excluded-table-data.json',
			'incremental/quant.raw_market_observations/20260915T160000.000000Z_20260916T120000.000000Z.copy.gz',
			'incremental/quant.raw_market_observations/20260915T160000.000000Z_20260916T120000.000000Z.json',
		]);
		assert.deepEqual(remotePartPaths('/apps/a/db', 'x/y.dump', 1), ['/apps/a/db/files/x/y.dump.enc']);
		assert.deepEqual(remotePartPaths('/apps/a/db', 'x/y.dump', 2), ['/apps/a/db/files/x/y.dump.enc.part000', '/apps/a/db/files/x/y.dump.enc.part001']);
		const state = { files: { [files[0].relative]: { verified: true, size: files[0].size, mtime_ms: files[0].mtime_ms } } };
		assert.equal(planUploads(files, state).length, files.length - 1);
		state.files[files[0].relative].size += 1;
		assert.equal(planUploads(files, state).length, files.length);
	} finally { await rm(root, { recursive: true, force: true }); }
});

test('local removal keeps unverified, recent and half-uploaded groups', () => {
	const day = 24 * 3600 * 1000; const now = 100 * day;
	const file = (relative, kind, dayName, ageDays) => ({ relative, kind, day: dayName, size: 10, mtime_ms: now - ageDays * day });
	const files = [
		file('2026-09-01/a.dump', 'dump', '2026-09-01', 10), file('2026-09-01/a.dump.sha256', 'dump', '2026-09-01', 10),
		file('2026-09-02/b.dump', 'dump', '2026-09-02', 10), file('2026-09-02/b.dump.sha256', 'dump', '2026-09-02', 10),
		file('2026-09-15/c.dump', 'dump', '2026-09-15', 1),
		file('incremental/t/old.copy.gz', 'incremental', null, 10), file('incremental/t/old.json', 'incremental', null, 10),
		file('incremental/t/half.copy.gz', 'incremental', null, 10), file('incremental/t/half.json', 'incremental', null, 10),
	];
	const verified = (f) => [f.relative, { verified: true, size: f.size, mtime_ms: f.mtime_ms }];
	const state = { files: Object.fromEntries(files.filter((f) => !['2026-09-02/b.dump.sha256', 'incremental/t/half.json'].includes(f.relative)).map(verified)) };
	const removed = selectLocalRemovals({ files, state, nowMs: now, retentionDays: 3 }).map((f) => f.relative).sort();
	assert.deepEqual(removed, ['2026-09-01/a.dump', '2026-09-01/a.dump.sha256', 'incremental/t/old.copy.gz', 'incremental/t/old.json']);
});

test('upload, prune and fetch reproduce the backup tree from the cloud alone', async () => {
	const root = await tempRoot();
	try {
		const backupRoot = join(root, 'backups');
		await seedBackup(backupRoot);
		const key = randomBytes(32);
		const pan = fakePan();
		const statePath = join(backupRoot, 'offsite', 'state.json');
		const spoolDir = join(backupRoot, 'offsite', 'spool');
		await mkdir(spoolDir, { recursive: true });
		const originals = new Map();
		for (const file of await listLocalBackupFiles(backupRoot)) originals.set(file.relative, await readFile(file.path));

		const first = await runUpload({ pan, backupRoot, remoteRoot: '/apps/test/db', key, statePath, spoolDir, partBytes: 4000 });
		assert.equal(first.uploaded, 6);
		const again = await runUpload({ pan, backupRoot, remoteRoot: '/apps/test/db', key, statePath, spoolDir, partBytes: 4000 });
		assert.equal(again.uploaded, 0, 'a second run uploads nothing');
		assert.ok([...pan.files.values()].some((item) => item.path.endsWith('.dump.enc.part001')), 'large files are split into parts');
		assert.equal((await readdir(spoolDir)).length, 0, 'spool is cleaned');
		await assert.rejects(runUpload({ pan, backupRoot, remoteRoot: '/apps/test/db', key: randomBytes(32), statePath, spoolDir }), /密钥与已上传记录不一致/);

		const old = new Date(Date.now() - 10 * 24 * 3600 * 1000);
		for (const file of await listLocalBackupFiles(backupRoot)) if (!file.relative.startsWith('2026-09-16')) await utimes(file.path, old, old);
		// mtime changed, so those files are re-uploaded before they may be pruned.
		await runUpload({ pan, backupRoot, remoteRoot: '/apps/test/db', key, statePath, spoolDir, partBytes: 4000 });
		const pruned = await runPrune({ backupRoot, statePath, retentionDays: 3 });
		assert.equal(pruned.removed, 4);
		await assert.rejects(stat(join(backupRoot, '2026-09-15')), /ENOENT/);
		assert.ok(await stat(join(backupRoot, '2026-09-16', 'trading_hareness-2026-09-16.dump')));
		assert.ok(await stat(join(backupRoot, 'incremental', 'quant.raw_market_observations', 'state.json')), 'the watermark is never pruned');

		const restoreRoot = join(root, 'restored');
		const fetched = await runFetch({ pan, remoteRoot: '/apps/test/db', key, destinationRoot: restoreRoot, spoolDir: join(root, 'fetch-spool'), day: '2026-09-15' });
		assert.equal(fetched.files, 4);
		for (const relative of ['2026-09-15/trading_hareness-2026-09-15.dump', '2026-09-15/trading_hareness-2026-09-15.dump.sha256',
			'incremental/quant.raw_market_observations/20260915T160000.000000Z_20260916T120000.000000Z.copy.gz',
			'incremental/quant.raw_market_observations/20260915T160000.000000Z_20260916T120000.000000Z.json']) {
			assert.deepEqual(await readFile(join(restoreRoot, ...relative.split('/'))), originals.get(relative), relative);
		}
		await assert.rejects(runFetch({ pan, remoteRoot: '/apps/test/db', key, destinationRoot: restoreRoot, spoolDir: join(root, 'fetch-spool'), day: '2026-01-01' }), /没有 2026-01-01/);
	} finally { await rm(root, { recursive: true, force: true }); }
});
