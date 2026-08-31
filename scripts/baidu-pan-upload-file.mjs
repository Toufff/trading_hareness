#!/usr/bin/env node

import { createReadStream } from 'node:fs';
import { stat } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import pg from '../feishu-adapter/node_modules/pg/lib/index.js';
import { createLedger } from '../feishu-adapter/ledger.mjs';
import { createBaiduPanStorage } from '../feishu-adapter/baidu-pan-storage.mjs';

function args(argv) {
	if (argv.length !== 2) throw new Error('usage: baidu-pan-upload-file.mjs <local-file> <remote-path>');
	return { file: argv[0], remotePath: argv[1] };
}

async function main() {
	const { file, remotePath } = args(process.argv.slice(2));
	const password = process.env.PGPASSWORD || process.env.POSTGRES_PASSWORD;
	if (!password) throw new Error('PGPASSWORD or POSTGRES_PASSWORD is required');
	const connectionString = `postgresql://${encodeURIComponent(process.env.PGUSER || 'n8n')}:${encodeURIComponent(password)}@${process.env.PGHOST || '127.0.0.1'}:${process.env.PGPORT || '5432'}/${encodeURIComponent(process.env.PGDATABASE || 'n8n')}`;
	const ledger = createLedger(connectionString);
	const pan = createBaiduPanStorage({ appKey: process.env.BAIDU_PAN_APP_KEY, secretKey: process.env.BAIDU_PAN_SECRET_KEY, redirectUri: process.env.BAIDU_PAN_REDIRECT_URI || 'oob', ledger, rootPath: '/' });
	const size = (await stat(file)).size;
	const result = await pan.uploadReadable({ readable: createReadStream(file), fileName: remotePath.split('/').pop(), size, remotePath });
	console.log(JSON.stringify({ path: result.path, fsId: result.fsId ?? null, size }));
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) main().catch((error) => { console.error(error?.stack || error); process.exitCode = 1; });

export { args };
