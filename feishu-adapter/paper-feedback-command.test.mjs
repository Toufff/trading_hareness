import assert from 'node:assert/strict';
import test from 'node:test';
import { parsePaperFeedback } from './paper-feedback-command.mjs';

test('parses explicit item feedback and stable recommendation ids', () => {
	assert.deepEqual(parsePaperFeedback('收 1 3'), { action: 'accept', items: [1, 3] });
	assert.deepEqual(parsePaperFeedback('略：2，5'), { action: 'dismiss', items: [2, 5] });
	assert.deepEqual(parsePaperFeedback('原因 2026-W35-02'), {
		action: 'reason', items: ['2026-W35-02'],
	});
	assert.deepEqual(parsePaperFeedback('稍后 6'), { action: 'snooze', items: [6] });
});

test('parses topic and author preferences', () => {
	assert.deepEqual(parsePaperFeedback('多点 网络'), { action: 'topic_more', topic: '网络' });
	assert.deepEqual(parsePaperFeedback('少点：MoE'), { action: 'topic_less', topic: 'MoE' });
	assert.deepEqual(parsePaperFeedback('作者 + Alice Smith'), {
		action: 'follow_author', author: 'Alice Smith',
	});
});

test('does not steal ingest commands or ordinary chatter', () => {
	assert.equal(parsePaperFeedback('收录 2608.23658v1'), null);
	assert.equal(parsePaperFeedback('收入增长 20%'), null);
	assert.equal(parsePaperFeedback('这篇原因不够充分'), null);
});
