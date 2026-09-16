#!/usr/bin/env node
// Off-site (Baidu Pan) copy of the stock database backup.
//
//   init-key                 create the encryption key file (refuses to overwrite)
//   authorize                device-code OAuth: prints the verification URL and user code, waits for approval
//   status                   authorization, quota and catalog summary
//   nightly                  upload every new backup file, upload the catalog, prune verified old local copies
//   upload | prune           the two halves of nightly
//   fetch --dest DIR [--day YYYY-MM-DD]
//                            rebuild a backup tree from the cloud for restore-stock-database.ps1 -BackupRoot DIR
//   selftest                 encrypt, upload, download, decrypt and compare a random file, then delete it remotely
//
// Configuration comes from the environment (run-stock-backup-offsite.ps1 maps runtime.env):
// PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD (OAuth token store), BAIDU_PAN_APP_KEY, BAIDU_PAN_SECRET_KEY,
// STOCK_BACKUP_ROOT, STOCK_BACKUP_OFFSITE_REMOTE_ROOT, STOCK_BACKUP_OFFSITE_KEY_FILE,
// STOCK_BACKUP_LOCAL_RETENTION_DAYS, STOCK_BACKUP_OFFSITE_PART_BYTES, STOCK_BACKUP_OFFSITE_SLICE_BYTES.
// Tokens, keys and secrets are never printed.

import { randomBytes, createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const adapterRoot = existsSync(new URL('../feishu-adapter/ledger.mjs', import.meta.url))
	? new URL('../feishu-adapter/', import.meta.url)
	: new URL('../', import.meta.url);
const [{ createLedger }, { createBaiduPanStorage, isBaiduPanAuthorizationPending }, offsite] = await Promise.all([
	import(new URL('ledger.mjs', adapterRoot)),
	import(new URL('baidu-pan-storage.mjs', adapterRoot)),
	import(new URL('stock-backup-offsite.mjs', adapterRoot)),
]);

function emit(record) { process.stdout.write(`${JSON.stringify({ at: new Date().toISOString(), ...record })}\n`); }

function option(args, name, fallback = null) {
	const index = args.indexOf(name);
	return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
}

function config() {
	const platform = process.env.STOCK_PLATFORM_ROOT || 'G:\\StockPlatform';
	const backupRoot = process.env.STOCK_BACKUP_ROOT || join(platform, 'backups');
	return {
		backupRoot,
		remoteRoot: (process.env.STOCK_BACKUP_OFFSITE_REMOTE_ROOT || '/apps/股票paper存储/db-backups/trading_hareness').replace(/\/+$/, ''),
		keyFile: process.env.STOCK_BACKUP_OFFSITE_KEY_FILE || join(platform, 'config', 'backup-offsite.key'),
		statePath: join(backupRoot, 'offsite', 'state.json'),
		spoolDir: join(backupRoot, 'offsite', 'spool'),
		retentionDays: Number(process.env.STOCK_BACKUP_LOCAL_RETENTION_DAYS ?? 3),
		partBytes: Number(process.env.STOCK_BACKUP_OFFSITE_PART_BYTES || 16 * 1024 ** 3),
		sliceBytes: Number(process.env.STOCK_BACKUP_OFFSITE_SLICE_BYTES || 32 * 1024 ** 2),
	};
}

function panClient() {
	const ledger = createLedger(undefined, { logger: { error: (message) => emit({ event: 'ledger_error', message: String(message) }), warn() {}, info() {}, log() {} } });
	const pan = createBaiduPanStorage({ appKey: process.env.BAIDU_PAN_APP_KEY, secretKey: process.env.BAIDU_PAN_SECRET_KEY, redirectUri: 'oob', ledger, rootPath: '/' });
	return { ledger, pan };
}

async function withPan(fn) {
	const { ledger, pan } = panClient();
	try { return await fn(pan, ledger); } finally { await ledger.close?.().catch(() => {}); }
}

async function main() {
	const [command, ...args] = process.argv.slice(2);
	const cfg = config();
	const log = (record) => emit(record);
	if (command === 'init-key') {
		await offsite.createKeyFile(cfg.keyFile);
		const key = await offsite.loadKey(cfg.keyFile);
		emit({ event: 'key_created', key_file: cfg.keyFile, key_fingerprint: offsite.keyFingerprint(key) });
		return;
	}
	if (!['authorize', 'status', 'nightly', 'upload', 'prune', 'fetch', 'selftest'].includes(command)) {
		throw new Error('usage: stock-backup-offsite.mjs init-key|authorize|status|nightly|upload|prune|fetch --dest DIR [--day YYYY-MM-DD]|selftest');
	}
	if (command === 'prune') { emit({ event: 'prune_completed', ...(await offsite.runPrune({ backupRoot: cfg.backupRoot, statePath: cfg.statePath, retentionDays: cfg.retentionDays, log })) }); return; }
	await withPan(async (pan, ledger) => {
		if (typeof ledger.initBaiduPanTokens === 'function') await ledger.initBaiduPanTokens();
		if (command === 'authorize') {
			const device = await pan.deviceCode();
			emit({ event: 'authorize_pending', verification_url: device.verification_url, user_code: device.user_code, expires_in: device.expires_in });
			const deadline = Date.now() + (device.expires_in || 300) * 1000;
			let interval = Math.max(5, device.interval || 5) * 1000;
			while (Date.now() < deadline) {
				await new Promise((resolve) => setTimeout(resolve, interval));
				try {
					const status = await pan.exchangeDeviceCode(device.device_code);
					emit({ event: 'authorized', ...status });
					return;
				} catch (error) {
					// Only "the user has not confirmed yet" keeps the poll alive;
					// a rejected or expired device code must fail loudly instead
					// of spinning until the deadline.
					if (!isBaiduPanAuthorizationPending(error)) throw error;
					if (String(error.oauthCode).toLowerCase() === 'slow_down') interval += 5000;
					emit({ event: 'authorize_waiting', reason: error.oauthCode || 'authorization_pending', seconds_left: Math.max(0, Math.round((deadline - Date.now()) / 1000)) });
				}
			}
			throw new Error('设备授权超时，未在有效期内完成');
		}
		if (command === 'status') {
			const status = await pan.status();
			const quota = status.authorized ? await pan.quota({ checkFree: 1 }).catch((error) => ({ error: error.message })) : null;
			const state = await offsite.readState(cfg.statePath);
			const entries = Object.values(state.files ?? {});
			emit({ event: 'status', authorized: status.authorized, access_expires_at: status.access_expires_at, quota_total: quota?.total ?? null, quota_used: quota?.used ?? null, quota_error: quota?.error ?? null, catalog_files: entries.length, catalog_encrypted_bytes: entries.reduce((sum, entry) => sum + (entry.encrypted_bytes || 0), 0), remote_root: cfg.remoteRoot });
			return;
		}
		const key = await offsite.loadKey(cfg.keyFile);
		await mkdir(cfg.spoolDir, { recursive: true });
		if (command === 'upload' || command === 'nightly') {
			const uploaded = await offsite.runUpload({ pan, backupRoot: cfg.backupRoot, remoteRoot: cfg.remoteRoot, key, statePath: cfg.statePath, spoolDir: cfg.spoolDir, partBytes: cfg.partBytes, sliceBytes: cfg.sliceBytes, log });
			emit({ event: 'upload_completed', ...uploaded });
			if (command === 'nightly') emit({ event: 'prune_completed', ...(await offsite.runPrune({ backupRoot: cfg.backupRoot, statePath: cfg.statePath, retentionDays: cfg.retentionDays, log })) });
			return;
		}
		if (command === 'fetch') {
			const dest = option(args, '--dest');
			if (!dest) throw new Error('fetch 需要 --dest 目录');
			emit({ event: 'fetch_completed', ...(await offsite.runFetch({ pan, remoteRoot: cfg.remoteRoot, key, destinationRoot: dest, spoolDir: join(dest, '.offsite-spool'), day: option(args, '--day'), log })) });
			await rm(join(dest, '.offsite-spool'), { recursive: true, force: true });
			return;
		}
		if (command === 'selftest') {
			const dir = join(cfg.spoolDir, `selftest-${process.pid}`);
			await mkdir(dir, { recursive: true });
			const remotePath = `${cfg.remoteRoot}/selftest/selftest-${Date.now()}.bin.enc`;
			try {
				const plain = randomBytes(40 * 1024 * 1024 + 12345);
				await writeFile(join(dir, 'plain.bin'), plain);
				const encrypted = await offsite.encryptFile(join(dir, 'plain.bin'), join(dir, 'plain.bin.enc'), key);
				const started = Date.now();
				const uploaded = await pan.uploadFile({ localPath: join(dir, 'plain.bin.enc'), remotePath, sliceBytes: cfg.sliceBytes });
				const uploadSeconds = (Date.now() - started) / 1000;
				const meta = await pan.fileMeta([uploaded.fs_id]);
				const response = await pan.download(uploaded.fs_id);
				await writeFile(join(dir, 'downloaded.enc'), Buffer.from(await response.arrayBuffer()));
				const decrypted = await offsite.decryptFile(join(dir, 'downloaded.enc'), join(dir, 'roundtrip.bin'), key);
				const same = decrypted.sha256 === encrypted.sha256 && createHash('sha256').update(await readFile(join(dir, 'roundtrip.bin'))).digest('hex') === createHash('sha256').update(plain).digest('hex');
				await pan.remove(uploaded.path).catch((error) => emit({ event: 'selftest_cleanup_failed', message: error.message }));
				emit({ event: 'selftest', passed: same && Number(meta?.list?.[0]?.size) === encrypted.encrypted_bytes, encrypted_bytes: encrypted.encrypted_bytes, slices: uploaded.block_list.length, slice_bytes: uploaded.slice_bytes, upload_seconds: uploadSeconds, upload_mib_per_second: Number((encrypted.encrypted_bytes / 1024 / 1024 / uploadSeconds).toFixed(2)), remote_path: uploaded.path });
				if (!same) process.exitCode = 1;
			} finally { await rm(dir, { recursive: true, force: true }); }
		}
	});
}

main().catch((error) => { emit({ event: 'failed', message: error.message }); process.exitCode = 1; });
