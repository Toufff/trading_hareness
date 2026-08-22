#!/usr/bin/env node

const base = process.env.QUANT_API_BASE ?? 'http://127.0.0.1:5681';
const response = await fetch(`${base}/openapi.json`, { signal: AbortSignal.timeout(5000) });
if (!response.ok) throw new Error(`openapi HTTP ${response.status}`);
const spec = await response.json();
const required = [
  '/api/v1/agent/context',
  '/api/v1/automation/runs',
  '/api/v1/analyst-research/reviews/latest',
  '/api/v1/analyst-research/reviews/run',
];
const missing = required.filter((path) => !spec.paths?.[path]);
if (missing.length) throw new Error(`missing API paths: ${missing.join(', ')}`);
if (!spec.paths['/api/v1/analyst-research/reviews/run'].post) throw new Error('review run is not POST');
console.log(`API contract verified: ${required.length} paths, ${Object.keys(spec.paths).length} total`);
