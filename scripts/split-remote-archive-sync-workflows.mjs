import { readFileSync, writeFileSync } from 'node:fs';

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) {
  throw new Error('usage: split-remote-archive-sync-workflows.mjs INPUT OUTPUT');
}

// Kept as a compatibility validation step for operators/scripts that still
// call the old "split" step.  The generated graph now contains independent
// message and report schedulers; quant-research owns their durable cursors.
const document = JSON.parse(readFileSync(inputPath, 'utf8'));
const workflows = Array.isArray(document) ? document : [document];
const ids = workflows.map((workflow) => workflow?.id).sort();
if (JSON.stringify(ids) !== JSON.stringify(['remoteArchiveMessages123', 'remoteArchiveReports123'])) {
  throw new Error('remote archive split workflows are missing or have unexpected ids');
}
writeFileSync(outputPath, JSON.stringify(workflows, null, 2) + '\n');
