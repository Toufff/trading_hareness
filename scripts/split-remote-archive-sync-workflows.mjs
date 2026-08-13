import { readFileSync, writeFileSync } from 'node:fs';

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) {
  throw new Error('usage: split-remote-archive-sync-workflows.mjs INPUT OUTPUT');
}

// Kept as a compatibility wrapper for operators/scripts that still call the
// old "split" step. The sync implementation is now one lightweight scheduler;
// quant-research owns both bounded text-only streams and their cursors.
const document = JSON.parse(readFileSync(inputPath, 'utf8'));
const workflow = Array.isArray(document) ? document[0] : document;
if (!workflow || workflow.id !== 'remoteArchiveSync123') {
  throw new Error('combined remote archive workflow is missing or has an unexpected id');
}
writeFileSync(outputPath, JSON.stringify([workflow], null, 2) + '\n');
