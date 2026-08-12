import { readFileSync, writeFileSync } from 'node:fs';

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) throw new Error('usage: harden-remote-http-workflow.mjs INPUT OUTPUT');
const document = JSON.parse(readFileSync(inputPath, 'utf8'));
const workflows = Array.isArray(document) ? document : [document];
for (const workflow of workflows) {
	for (const node of workflow.nodes ?? []) {
		if (node.type !== 'n8n-nodes-base.httpRequest' || !String(node.parameters?.url ?? '').includes('47.114.113.152:18081')) continue;
		node.retryOnFail = true;
		node.maxTries = 4;
		node.waitBetweenTries = 1000;
		node.parameters.options = { ...(node.parameters.options ?? {}), timeout: 120000 };
	}
}
writeFileSync(outputPath, JSON.stringify(Array.isArray(document) ? workflows : workflows[0], null, 2) + '\n');
