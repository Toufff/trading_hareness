#!/usr/bin/env node

import { createReadStream } from 'node:fs';
import { existsSync } from 'node:fs';
import { stat } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
const adapterRoot = existsSync(new URL('../feishu-adapter/ledger.mjs', import.meta.url))
	? new URL('../feishu-adapter/', import.meta.url)
	: new URL('../', import.meta.url);
const [{ default: pg }, { createLedger }, { createBaiduPanStorage }] = await Promise.all([
	import(new URL('node_modules/pg/lib/index.js', adapterRoot)),
	import(new URL('ledger.mjs', adapterRoot)),
	import(new URL('baidu-pan-storage.mjs', adapterRoot)),
]);

function args(argv) {
	if (argv.length !== 2) throw new Error('usage: baidu-pan-upload-file.mjs <local-file> <remote-path>');
	return { file: argv[0], remotePath: argv[1] };
}

async function remoteExists(pan, remotePath) {
	const slash = remotePath.lastIndexOf('/');
	const directory = slash > 0 ? remotePath.slice(0, slash) : '/';
	const filename = remotePath.slice(slash + 1);
	const listed = await pan.list({ dir: directory, limit: 1000 });
	return (listed.list ?? []).find((item) => item?.server_filename === filename && Number(item?.isdir) !== 1) ?? null;
}

async function main() {
	const { file, remotePath } = args(process.argv.slice(2));
	const password = process.env.PGPASSWORD || process.env.POSTGRES_PASSWORD;
	if (!password) throw new Error('PGPASSWORD or POSTGRES_PASSWORD is required');
	const connectionString = `postgresql://${encodeURIComponent(process.env.PGUSER || 'n8n')}:${encodeURIComponent(password)}@${process.env.PGHOST || '127.0.0.1'}:${process.env.PGPORT || '5432'}/${encodeURIComponent(process.env.PGDATABASE || 'n8n')}`;
	const ledger = createLedger(connectionString);
	const pan = createBaiduPanStorage({ appKey: process.env.BAIDU_PAN_APP_KEY, secretKey: process.env.BAIDU_PAN_SECRET_KEY, redirectUri: process.env.BAIDU_PAN_REDIRECT_URI || 'oob', ledger, rootPath: '/' });
	let readable;
	let size;
	if (file === '-') {
		const chunks = [];
		for await (const chunk of process.stdin) chunks.push(Buffer.from(chunk));
		const content = Buffer.concat(chunks);
		readable = (async function* () { yield content; }());
		size = content.length;
	} else {
		size = (await stat(file)).size;
		readable = createReadStream(file);
	}
	const existing = await remoteExists(pan, remotePath);
	if (existing) {
		if (Number(existing.size) !== size) throw new Error(`远端文件已存在但大小不一致：${remotePath}`);
		console.log(JSON.stringify({ path: remotePath, skipped: true }));
		return;
	}
	const result = await pan.uploadReadable({ readable, fileName: remotePath.split('/').pop(), size, remotePath });
	console.log(JSON.stringify({ path: result.path, fsId: result.fsId ?? null, size }));
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) main().catch((error) => { console.error(error?.stack || error); process.exitCode = 1; });

export { args };
