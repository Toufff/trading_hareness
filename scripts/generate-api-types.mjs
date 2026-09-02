#!/usr/bin/env node

// Regenerate (or --check) frontend/src/api/generated.ts from the quant-service
// OpenAPI document.  Three sources, in priority order:
//   1. --spec <file> or QUANT_OPENAPI_FILE=<file>: a local openapi.json
//   2. --offline: render the document with scripts/dump-openapi.py (no DB, no
//      server; QUANT_PYTHON selects the interpreter, default `python`)
//   3. default: fetch `${QUANT_API_BASE ?? http://127.0.0.1:5681}/openapi.json`

import { execFile } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const output = resolve(root, 'frontend/src/api/generated.ts');
const base = process.env.QUANT_API_BASE ?? 'http://127.0.0.1:5681';
const args = process.argv.slice(2);
const checkOnly = args.includes('--check');
const offline = args.includes('--offline');
const specIndex = args.indexOf('--spec');
const specArgument = specIndex >= 0 ? args[specIndex + 1] : undefined;
const specFile = specArgument ?? process.env.QUANT_OPENAPI_FILE;
if (specIndex >= 0 && !specArgument) throw new Error('--spec requires a file path');

const temp = await mkdtemp(`${tmpdir()}/quant-openapi-`);
const generated = resolve(temp, 'generated.ts');

/** Resolve the OpenAPI source to something openapi-typescript accepts (URL or path). */
async function resolveSpecSource({ specFile, offline, base, temp, python = process.env.QUANT_PYTHON ?? 'python' }) {
  if (specFile) return { source: resolve(specFile), label: `file ${specFile}` };
  if (offline) {
    const dumped = resolve(temp, 'openapi.json');
    await execFileAsync(python, [resolve(root, 'scripts/dump-openapi.py'), dumped], { cwd: root, maxBuffer: 64 * 1024 * 1024 });
    return { source: dumped, label: 'offline dump-openapi.py' };
  }
  return { source: `${base}/openapi.json`, label: `url ${base}` };
}

try {
  const cli = resolve(root, 'frontend/node_modules/openapi-typescript/bin/cli.js');
  const { source, label } = await resolveSpecSource({ specFile, offline, base, temp });
  await execFileAsync(process.execPath, [cli, source, '-o', generated], { cwd: resolve(root, 'frontend') });
  const content = await readFile(generated, 'utf8');
  if (checkOnly) {
    let current;
    try { current = await readFile(output, 'utf8'); } catch { current = null; }
    if (current !== content) {
      console.error(`generated API types are stale (source: ${label}): run node scripts/generate-api-types.mjs`);
      process.exitCode = 1;
    } else {
      console.log(`generated API types are current (source: ${label})`);
    }
  } else {
    await writeFile(output, content);
    console.log(`generated ${output} (source: ${label})`);
  }
} finally {
  await rm(temp, { recursive: true, force: true });
}
