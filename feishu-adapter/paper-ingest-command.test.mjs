import assert from 'node:assert/strict';
import test from 'node:test';
import { parsePaperIngestIds } from './paper-ingest-command.mjs';

test('parses the exact Chinese Feishu ingest command with versioned arXiv ids', () => {
	assert.deepEqual(
		parsePaperIngestIds('收录 2608.23658v1 2608.21614v1 2608.24637v1'),
		['2608.23658v1', '2608.21614v1', '2608.24637v1'],
	);
});

test('accepts supported command separators and English command', () => {
	assert.deepEqual(parsePaperIngestIds('收录：2608.23658v1'), ['2608.23658v1']);
	assert.deepEqual(parsePaperIngestIds('收: 2608.21614'), ['2608.21614']);
	assert.deepEqual(parsePaperIngestIds('ingest 2608.24637V1'), ['2608.24637V1']);
});

test('does not treat ordinary Chinese text or an empty command as paper ingest', () => {
	assert.equal(parsePaperIngestIds('收入增长 2608.23658v1'), null);
	assert.equal(parsePaperIngestIds('收录'), null);
	assert.equal(parsePaperIngestIds('收录 不是 arXiv 编号'), null);
});

test('deduplicates repeated ids while preserving input order', () => {
	assert.deepEqual(
		parsePaperIngestIds('收录 2608.23658v1, 2608.23658v1 2608.21614v1'),
		['2608.23658v1', '2608.21614v1'],
	);
});
