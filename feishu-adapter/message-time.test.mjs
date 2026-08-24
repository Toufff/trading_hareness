import assert from 'node:assert/strict';
import test from 'node:test';
import { extractImportContent } from './message-time.mjs';

test('uses the leading month-day timestamp in Asia/Shanghai as import metadata', () => {
	assert.deepEqual(extractImportContent('#quanneng\n8-24 09:42:09\n\n内容', { referenceTime: '2026-08-24T01:42:09.000Z' }), {
		content: '内容', content_date: '2026-08-24', content_time: '09:42',
	});
});

test('keeps the explicit ISO timestamp behaviour', () => {
	assert.deepEqual(extractImportContent('#liwei\n@2026-08-24 09:42\n内容'), {
		content: '内容', content_date: '2026-08-24', content_time: '09:42',
	});
});

test('does not invent a timestamp when the message has none', () => {
	assert.deepEqual(extractImportContent('#anqiang\n没有显式时间的内容'), { content: '没有显式时间的内容' });
});
