import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const grants = readFileSync('deploy/intraday-edge/edge_export_grants.sql', 'utf8');
for (const table of [
	'quant.ten_day_leader_rotation_runs',
	'quant.intraday_signal_events',
	'quant.ten_day_leader_rotation_intraday_observations',
	'quant.edge_evidence_changes',
]) assert.match(grants, new RegExp(`\\b${table.replaceAll('.', '\\.')}\\b`), `missing export grant for ${table}`);
assert.match(grants, /REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA quant FROM quant_edge_export/);
console.log('edge export grants cover the evidence journal and remain least-privilege');
