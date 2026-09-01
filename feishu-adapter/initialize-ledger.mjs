#!/usr/bin/env node

// Explicit fresh-database prerequisite for the quant legacy baseline.
// Reuse the adapter's authoritative ledger DDL instead of maintaining a
// second, inevitably drifting copy in a deployment script.

import { createLedger } from './ledger.mjs';

const required = ['PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD'];
const missing = required.filter((name) => !String(process.env[name] ?? '').trim());
if (missing.length) {
	throw new Error(`missing PostgreSQL environment variables: ${missing.join(', ')}`);
}

const ledger = createLedger();
try {
	await ledger.init({ routes: [] });
	process.stdout.write(JSON.stringify({ status: 'ready', component: 'ingestion-ledger' }) + '\n');
} finally {
	await ledger.close();
}
