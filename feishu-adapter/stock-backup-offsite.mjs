import { createCipheriv, createDecipheriv, createHash, randomBytes } from 'node:crypto';
import { createReadStream, createWriteStream } from 'node:fs';
import { mkdir, open, readFile, readdir, rename, rm, stat, writeFile } from 'node:fs/promises';
import { dirname, join, relative, sep } from 'node:path';
import { pipeline } from 'node:stream/promises';

// Off-site copy of the nightly stock database backup in Baidu Pan.
//
// The local backup (scripts/windows/backup-stock-database.ps1) writes a base
// dump per day plus append-only incremental chunks of the large raw tables.
// Every such file is encrypted here (Baidu Pan is third-party storage) and
// uploaded once; state.json records what is safely in the cloud, and an
// encrypted copy of that catalog is uploaded too, so a restore works even
// after the local disk -- and its state file -- is gone.  Local files are
// removed only after their upload was verified and they are older than the
// local retention window.

export const ENVELOPE_MAGIC = Buffer.from('SBOFF1', 'ascii');
const IV_BYTES = 12;
const TAG_BYTES = 16;
const DAY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const DUMP_FILE_PATTERN = /\.dump(\.sha256|\.excluded-table-data\.json)?$/;
const CHUNK_FILE_PATTERN = /^\d{8}T\d{6}\.\d{6}Z_\d{8}T\d{6}\.\d{6}Z\.(copy\.gz|json)$/;

export async function createKeyFile(path) {
	await mkdir(dirname(path), { recursive: true });
	const handle = await open(path, 'wx');
	try { await handle.writeFile(`${randomBytes(32).toString('base64')}\n`, 'utf8'); } finally { await handle.close(); }
}

export async function loadKey(path) {
	const key = Buffer.from((await readFile(path, 'utf8')).trim(), 'base64');
	if (key.length !== 32) throw new Error(`异地备份密钥文件格式无效：${path}`);
	return key;
}

export function keyFingerprint(key) {
	return createHash('sha256').update('stock-backup-offsite-key:').update(key).digest('hex').slice(0, 16);
}

// Envelope: magic | iv | AES-256-GCM ciphertext | tag.  Returns the plaintext
// SHA-256 so the catalog can verify a restored file end to end.
export async function encryptFile(source, destination, key) {
	const iv = randomBytes(IV_BYTES);
	const cipher = createCipheriv('aes-256-gcm', key, iv);
	const hash = createHash('sha256');
	let plaintextBytes = 0;
	const partial = `${destination}.partial`;
	await mkdir(dirname(destination), { recursive: true });
	const output = createWriteStream(partial);
	output.write(Buffer.concat([ENVELOPE_MAGIC, iv]));
	await pipeline(
		createReadStream(source),
		async function* (chunks) { for await (const chunk of chunks) { hash.update(chunk); plaintextBytes += chunk.length; yield cipher.update(chunk); } const last = cipher.final(); if (last.length) yield last; yield cipher.getAuthTag(); },
		output,
	);
	await rename(partial, destination);
	return { sha256: hash.digest('hex'), plaintext_bytes: plaintextBytes, encrypted_bytes: (await stat(destination)).size };
}

export async function decryptFile(source, destination, key) {
	const { size } = await stat(source);
	if (size < ENVELOPE_MAGIC.length + IV_BYTES + TAG_BYTES) throw new Error(`异地备份文件过短：${source}`);
	const handle = await open(source, 'r');
	const header = Buffer.alloc(ENVELOPE_MAGIC.length + IV_BYTES);
	const tag = Buffer.alloc(TAG_BYTES);
	try {
		await handle.read(header, 0, header.length, 0);
		await handle.read(tag, 0, TAG_BYTES, size - TAG_BYTES);
	} finally { await handle.close(); }
	if (!header.subarray(0, ENVELOPE_MAGIC.length).equals(ENVELOPE_MAGIC)) throw new Error(`不是异地备份加密文件：${source}`);
	const decipher = createDecipheriv('aes-256-gcm', key, header.subarray(ENVELOPE_MAGIC.length));
	decipher.setAuthTag(tag);
	const hash = createHash('sha256');
	const partial = `${destination}.partial`;
	await mkdir(dirname(destination), { recursive: true });
	try {
		await pipeline(
			createReadStream(source, { start: header.length, end: size - TAG_BYTES - 1 }),
			async function* (chunks) { for await (const chunk of chunks) { const plain = decipher.update(chunk); hash.update(plain); yield plain; } const last = decipher.final(); if (last.length) { hash.update(last); yield last; } },
			createWriteStream(partial),
		);
	} catch (error) {
		await rm(partial, { force: true });
		throw new Error(`异地备份解密失败（密钥错误或文件损坏）：${source}：${error.message}`);
	}
	await rename(partial, destination);
	return { sha256: hash.digest('hex') };
}

function toPosix(value) { return value.split(sep).join('/'); }

// Pure-ish inventory: which local files belong to the backup set.
export async function listLocalBackupFiles(backupRoot) {
	const files = [];
	for (const entry of await readdir(backupRoot, { withFileTypes: true }).catch(() => [])) {
		if (entry.isDirectory() && DAY_PATTERN.test(entry.name)) {
			for (const file of await readdir(join(backupRoot, entry.name), { withFileTypes: true })) {
				if (file.isFile() && DUMP_FILE_PATTERN.test(file.name)) files.push({ kind: 'dump', day: entry.name, path: join(backupRoot, entry.name, file.name) });
			}
		}
	}
	const incrementalRoot = join(backupRoot, 'incremental');
	for (const table of await readdir(incrementalRoot, { withFileTypes: true }).catch(() => [])) {
		if (!table.isDirectory()) continue;
		for (const file of await readdir(join(incrementalRoot, table.name), { withFileTypes: true })) {
			if (file.isFile() && CHUNK_FILE_PATTERN.test(file.name)) files.push({ kind: 'incremental', table: table.name, day: null, path: join(incrementalRoot, table.name, file.name) });
		}
	}
	for (const file of files) {
		const info = await stat(file.path);
		file.relative = toPosix(relative(backupRoot, file.path));
		file.size = info.size;
		file.mtime_ms = Math.floor(info.mtimeMs);
	}
	return files.sort((a, b) => a.relative.localeCompare(b.relative));
}

export function remotePartPaths(remoteRoot, relativePath, parts) {
	const base = `${remoteRoot.replace(/\/+$/, '')}/files/${relativePath}.enc`;
	return parts === 1 ? [base] : Array.from({ length: parts }, (_, index) => `${base}.part${String(index).padStart(3, '0')}`);
}

// Pure.  A file needs uploading unless the catalog holds a verified copy of
// exactly this size and modification time.
export function planUploads(files, state) {
	return files.filter((file) => {
		const entry = state?.files?.[file.relative];
		return !(entry?.verified && entry.size === file.size && entry.mtime_ms === file.mtime_ms);
	});
}

// Pure.  Local copies that may be deleted: verified in the cloud and older
// than the retention window.  A day directory's dump files go together, and
// a chunk goes only together with its manifest, so the local tree never holds
// half of a restorable unit.
export function selectLocalRemovals({ files, state, nowMs, retentionDays }) {
	const cutoff = nowMs - Math.max(0, Number(retentionDays)) * 24 * 3600 * 1000;
	const safe = (file) => { const entry = state?.files?.[file.relative]; return Boolean(entry?.verified && entry.size === file.size && entry.mtime_ms === file.mtime_ms && file.mtime_ms < cutoff); };
	const groups = new Map();
	for (const file of files) {
		const key = file.kind === 'dump' ? `dump:${file.day}` : `chunk:${file.relative.replace(/\.(copy\.gz|json)$/, '')}`;
		if (!groups.has(key)) groups.set(key, []);
		groups.get(key).push(file);
	}
	const removals = [];
	for (const group of groups.values()) {
		if (group.every(safe)) removals.push(...group);
	}
	return removals;
}

export async function readState(path) {
	try { return JSON.parse(await readFile(path, 'utf8')); } catch (error) { if (error.code === 'ENOENT') return { schema_version: 1, files: {} }; throw error; }
}

export async function writeState(path, state) {
	await mkdir(dirname(path), { recursive: true });
	const temp = `${path}.tmp`;
	await writeFile(temp, `${JSON.stringify(state, null, 2)}\n`, 'utf8');
	await rename(temp, path);
}

async function uploadEncrypted({ pan, encryptedPath, encryptedBytes, remotePaths, partBytes, sliceBytes, log }) {
	const parts = [];
	for (const [index, remotePath] of remotePaths.entries()) {
		const start = index * partBytes;
		const length = Math.min(partBytes, encryptedBytes - start);
		const uploaded = await pan.uploadFile({ localPath: encryptedPath, remotePath, start, length, sliceBytes });
		const meta = uploaded.fs_id ? await pan.fileMeta([uploaded.fs_id]) : null;
		const remoteSize = Number(meta?.list?.[0]?.size ?? NaN);
		if (remoteSize !== length) throw new Error(`云端文件大小校验失败：${remotePath} 本地 ${length}，云端 ${remoteSize}`);
		parts.push({ path: uploaded.path, fs_id: uploaded.fs_id, size: length, md5: uploaded.md5 });
		log?.({ event: 'part_uploaded', remote_path: uploaded.path, bytes: length, rapid_upload: uploaded.rapid_upload });
	}
	return parts;
}

export async function runUpload({ pan, backupRoot, remoteRoot, key, statePath, spoolDir, partBytes = 16 * 1024 ** 3, sliceBytes = 32 * 1024 ** 2, log = () => {} }) {
	const state = await readState(statePath);
	state.files ??= {};
	state.key_fingerprint ??= keyFingerprint(key);
	if (state.key_fingerprint !== keyFingerprint(key)) throw new Error('异地备份密钥与已上传记录不一致，拒绝混用密钥');
	const files = await listLocalBackupFiles(backupRoot);
	const pending = planUploads(files, state);
	let uploadedBytes = 0;
	for (const file of pending) {
		const encryptedPath = join(spoolDir, `${file.relative.replaceAll('/', '__')}.enc`);
		try {
			const encrypted = await encryptFile(file.path, encryptedPath, key);
			const parts = Math.max(1, Math.ceil(encrypted.encrypted_bytes / partBytes));
			const remoteParts = await uploadEncrypted({ pan, encryptedPath, encryptedBytes: encrypted.encrypted_bytes, remotePaths: remotePartPaths(remoteRoot, file.relative, parts), partBytes, sliceBytes, log });
			state.files[file.relative] = {
				kind: file.kind, day: file.day, table: file.table ?? null, size: file.size, mtime_ms: file.mtime_ms,
				sha256: encrypted.sha256, encrypted_bytes: encrypted.encrypted_bytes, parts: remoteParts,
				verified: true, uploaded_at: new Date().toISOString(),
			};
			await writeState(statePath, state);
			uploadedBytes += encrypted.encrypted_bytes;
			log({ event: 'file_uploaded', relative: file.relative, bytes: encrypted.encrypted_bytes, parts: remoteParts.length });
		} finally {
			await rm(encryptedPath, { force: true });
		}
	}
	const catalog = await uploadCatalog({ pan, remoteRoot, key, statePath, spoolDir, sliceBytes });
	return { files: files.length, uploaded: pending.length, uploaded_bytes: uploadedBytes, catalog };
}

// The encrypted catalog is written under a fixed name (the restore entry
// point) and a dated name (so a corrupted latest catalog is not fatal).
export async function uploadCatalog({ pan, remoteRoot, key, statePath, spoolDir, sliceBytes }) {
	const encryptedPath = join(spoolDir, 'offsite-state.json.enc');
	try {
		await encryptFile(statePath, encryptedPath, key);
		const stamp = new Date().toISOString().slice(0, 10);
		const paths = [`${remoteRoot}/catalog/offsite-state.json.enc`, `${remoteRoot}/catalog/offsite-state-${stamp}.json.enc`];
		for (const remotePath of paths) await pan.uploadFile({ localPath: encryptedPath, remotePath, sliceBytes });
		return paths;
	} finally { await rm(encryptedPath, { force: true }); }
}

export async function runPrune({ backupRoot, statePath, retentionDays, nowMs = Date.now(), log = () => {} }) {
	const state = await readState(statePath);
	const files = await listLocalBackupFiles(backupRoot);
	const removals = selectLocalRemovals({ files, state, nowMs, retentionDays });
	for (const file of removals) { await rm(file.path, { force: true }); log({ event: 'local_removed', relative: file.relative }); }
	for (const day of new Set(removals.filter((file) => file.kind === 'dump').map((file) => file.day))) {
		const dir = join(backupRoot, day);
		if (!(await readdir(dir).catch(() => [])).length) await rm(dir, { recursive: true, force: true });
	}
	return { removed: removals.length, removed_bytes: removals.reduce((sum, file) => sum + file.size, 0) };
}

async function downloadTo(pan, fsId, destination) {
	const response = await pan.download(fsId);
	await mkdir(dirname(destination), { recursive: true });
	await pipeline(response.body, createWriteStream(destination));
}

async function fetchEntry({ pan, key, entry, destination, spoolDir }) {
	const encryptedPath = join(spoolDir, `${createHash('sha256').update(destination).digest('hex').slice(0, 16)}.enc`);
	try {
		const output = createWriteStream(encryptedPath);
		for (const [index, part] of entry.parts.entries()) {
			const partPath = `${encryptedPath}.part${index}`;
			await downloadTo(pan, part.fs_id, partPath);
			const { size } = await stat(partPath);
			if (size !== part.size) throw new Error(`下载大小不一致：${part.path} 预期 ${part.size}，实际 ${size}`);
			await pipeline(createReadStream(partPath), output, { end: index === entry.parts.length - 1 });
			await rm(partPath, { force: true });
		}
		const { sha256 } = await decryptFile(encryptedPath, destination, key);
		if (sha256 !== entry.sha256) { await rm(destination, { force: true }); throw new Error(`还原文件 SHA-256 不一致：${destination}`); }
	} finally { await rm(encryptedPath, { force: true }); }
}

// Downloads the catalog, then one day's base dump (latest by default) and the
// complete incremental chain into a local backup-root layout that
// restore-stock-database.ps1 can use directly.
export async function runFetch({ pan, remoteRoot, key, destinationRoot, spoolDir, day = null, log = () => {} }) {
	await mkdir(spoolDir, { recursive: true });
	const listing = await pan.list({ dir: `${remoteRoot}/catalog`, limit: 1000 });
	const catalogItem = (listing?.list ?? []).find((item) => item?.server_filename === 'offsite-state.json.enc');
	if (!catalogItem) throw new Error('云端没有异地备份目录文件 catalog/offsite-state.json.enc');
	const catalogEncrypted = join(spoolDir, 'catalog.json.enc');
	const catalogPath = join(spoolDir, 'catalog.json');
	await downloadTo(pan, catalogItem.fs_id, catalogEncrypted);
	await decryptFile(catalogEncrypted, catalogPath, key);
	await rm(catalogEncrypted, { force: true });
	const catalog = JSON.parse(await readFile(catalogPath, 'utf8'));
	const days = [...new Set(Object.values(catalog.files).filter((entry) => entry.kind === 'dump').map((entry) => entry.day))].sort();
	const selectedDay = day ?? days.at(-1);
	if (!selectedDay || !days.includes(selectedDay)) throw new Error(`云端没有 ${day ?? '任何'} 日的基础备份；可用日期：${days.join(', ') || '无'}`);
	const wanted = Object.entries(catalog.files).filter(([, entry]) => entry.kind === 'incremental' || entry.day === selectedDay);
	for (const [relativePath, entry] of wanted) {
		const destination = join(destinationRoot, ...relativePath.split('/'));
		await fetchEntry({ pan, key, entry, destination, spoolDir });
		log({ event: 'file_fetched', relative: relativePath });
	}
	return { day: selectedDay, files: wanted.length, destination_root: destinationRoot };
}
