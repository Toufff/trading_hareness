#!/usr/bin/env node

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
const checkOnly = process.argv.includes('--check');
const temp = await mkdtemp(`${tmpdir()}/quant-openapi-`);
const generated = resolve(temp, 'generated.ts');

try {
  const cli = resolve(root, 'frontend/node_modules/openapi-typescript/bin/cli.js');
  await execFileAsync(process.execPath, [cli, `${base}/openapi.json`, '-o', generated], { cwd: resolve(root, 'frontend') });
  const content = await readFile(generated, 'utf8');
  if (checkOnly) {
    let current;
    try { current = await readFile(output, 'utf8'); } catch { current = null; }
    if (current !== content) {
      console.error(`generated API types are stale: run node scripts/generate-api-types.mjs`);
      process.exitCode = 1;
    } else {
      console.log('generated API types are current');
    }
  } else {
    await writeFile(output, content);
    console.log(`generated ${output}`);
  }
} finally {
  await rm(temp, { recursive: true, force: true });
}
