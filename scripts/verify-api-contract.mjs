#!/usr/bin/env node

// Compare the full OpenAPI path set against frontend/src/api/generated.ts.
// Source of the OpenAPI document, in priority order:
//   --spec <file> / QUANT_OPENAPI_FILE   local openapi.json (see scripts/dump-openapi.py)
//   QUANT_API_BASE (default http://127.0.0.1:5681)   a mounted quant-research instance
// Every path the service mounts must exist in the generated types and vice
// versa, so a new or renamed route fails here instead of at the next
// `npm run api:check`.

import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const generatedPath = resolve(root, 'frontend/src/api/generated.ts');
const args = process.argv.slice(2);
const specIndex = args.indexOf('--spec');
const specFile = (specIndex >= 0 ? args[specIndex + 1] : undefined) ?? process.env.QUANT_OPENAPI_FILE;
const base = process.env.QUANT_API_BASE ?? 'http://127.0.0.1:5681';

function generatedPaths(source) {
  const start = source.indexOf('export interface paths {');
  if (start < 0) throw new Error('generated.ts has no `paths` interface');
  const end = source.indexOf('\n}\n', start);
  const block = source.slice(start, end < 0 ? undefined : end);
  return new Set([...block.matchAll(/^ {4}"(\/[^"]*)": \{/gm)].map((match) => match[1]));
}

function comparePaths(specPaths, typedPaths) {
  const missingInTypes = [...specPaths].filter((path) => !typedPaths.has(path)).sort();
  const staleInTypes = [...typedPaths].filter((path) => !specPaths.has(path)).sort();
  return { missingInTypes, staleInTypes };
}

async function loadSpec() {
  if (specFile) return { spec: JSON.parse(await readFile(resolve(specFile), 'utf8')), label: `file ${specFile}` };
  const response = await fetch(`${base}/openapi.json`, { signal: AbortSignal.timeout(5000) });
  if (!response.ok) throw new Error(`openapi HTTP ${response.status}`);
  return { spec: await response.json(), label: `url ${base}` };
}

const { spec, label } = await loadSpec();
const specPaths = new Set(Object.keys(spec.paths ?? {}));
if (!specPaths.size) throw new Error('OpenAPI document has no paths');
if (!spec.paths['/api/v1/analyst-research/reviews/run']?.post) throw new Error('review run is not POST');
const typedPaths = generatedPaths(await readFile(generatedPath, 'utf8'));
const { missingInTypes, staleInTypes } = comparePaths(specPaths, typedPaths);
if (missingInTypes.length || staleInTypes.length) {
  const lines = [];
  if (missingInTypes.length) lines.push(`missing in generated.ts: ${missingInTypes.join(', ')}`);
  if (staleInTypes.length) lines.push(`no longer mounted: ${staleInTypes.join(', ')}`);
  throw new Error(`API contract drift (${label}); run node scripts/generate-api-types.mjs\n${lines.join('\n')}`);
}
console.log(`API contract verified: ${specPaths.size} paths match generated.ts (${label})`);
