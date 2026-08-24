import assert from 'node:assert/strict';
import test from 'node:test';
import { createSummaryListener } from './summary-listener.mjs';

test('summary listener processes both bot and human messages and normalizes history payloads', async () => {
	const cursors = [];
	const processed = [];
	const messages = [
		{ message_id: 'om_bot', msg_type: 'post', create_time: '1700000000000', body: { content: '{"content":[[{"tag":"text","text":"#quanneng"}]]}' }, sender: { sender_type: 'bot' } },
		{ message_id: 'om_human', msg_type: 'text', create_time: '1700000001000', body: { content: '{"text":"#anqiang\\n手动消息"}' }, sender: { sender_type: 'user' } },
	];
	const listener = createSummaryListener({
		sourceApi: { messageList: async () => ({ data: { items: messages, has_more: false } }) },
		ledger: {
			summaryListenerState: async () => null,
			saveSummaryListenerCursor: async (value) => { cursors.push(value); },
		},
		processMessage: async (event) => { processed.push(event); return {}; },
		logger: { error() {} },
		config: { enabled: true, key: 'summary', chatId: 'oc_summary', intervalSeconds: 10, historyLookbackSeconds: 3600, overlapSeconds: 30, bootstrapMode: 'forward_existing', sourceLabel: '分析师发送汇总群' },
	});
	await listener.tick();
	assert.deepEqual(processed.map((event) => [event.message.message_id, event.message.message_type, event.sender.sender_type]), [['om_bot', 'post', 'bot'], ['om_human', 'text', 'user']]);
	assert.equal(processed[0].message.content, messages[0].body.content);
	assert.equal(cursors.length, 1);
	assert.ok(cursors[0].cursorCreateTime >= 1700000001000);
	assert.equal(cursors[0].lastSourceCreateTime, 1700000001000);
	assert.equal(listener.status().state, 'healthy');
});

test('summary listener can establish a baseline without replaying old messages', async () => {
	let calls = 0;
	const listener = createSummaryListener({
		sourceApi: { messageList: async () => ({ data: { items: [{ message_id: 'om_old', msg_type: 'text', create_time: '1700000000000', body: { content: '{"text":"#liwei"}' } }], has_more: false } }) },
		ledger: { summaryListenerState: async () => null, saveSummaryListenerCursor: async () => {} },
		processMessage: async () => { calls += 1; }, logger: { error() {} },
		config: { enabled: true, key: 'summary', chatId: 'oc_summary', intervalSeconds: 10, historyLookbackSeconds: 3600, overlapSeconds: 30, bootstrapMode: 'skip_existing', sourceLabel: '分析师发送汇总群' },
	});
	await listener.tick();
	assert.equal(calls, 0);
	assert.equal(listener.status().ignored_count, 1);
});

test('a fenced summary listener does not poll or hand messages to the remote delivery path', async () => {
	let polled = 0;
	let processed = 0;
	const listener = createSummaryListener({
		sourceApi: { messageList: async () => { polled += 1; return { data: { items: [], has_more: false } }; } },
		ledger: { summaryListenerState: async () => null, saveSummaryListenerCursor: async () => {} },
		processMessage: async () => { processed += 1; return {}; }, logger: { error() {} },
		canWrite: async () => ({ allowed: false, writer_id: 'relay-edge-47' }),
		config: { enabled: true, key: 'summary', chatId: 'oc_summary', intervalSeconds: 10, historyLookbackSeconds: 3600, overlapSeconds: 30, bootstrapMode: 'forward_existing', sourceLabel: '分析师发送汇总群' },
	});
	await listener.tick();
	assert.equal(polled, 0);
	assert.equal(processed, 0);
	assert.equal(listener.status().writer_state, 'fenced');
});

test('summary listener reports repeated observations as local idempotent duplicates', async () => {
	const listener = createSummaryListener({
		sourceApi: { messageList: async () => ({ data: { items: [{ message_id: 'om_seen', msg_type: 'text', create_time: String(Date.now()), body: { content: '{"text":"#liwei"}' } }], has_more: false } }) },
		ledger: { summaryListenerState: async () => null, saveSummaryListenerCursor: async () => {} },
		processMessage: async () => ({ duplicate: true }), logger: { error() {} },
		config: { enabled: true, key: 'summary', chatId: 'oc_summary', intervalSeconds: 10, historyLookbackSeconds: 3600, overlapSeconds: 30, bootstrapMode: 'forward_existing', sourceLabel: '分析师发送汇总群' },
	});
	await listener.tick();
	assert.equal(listener.status().processed_count, 0);
	assert.equal(listener.status().duplicate_count, 1);
});

test('summary listener restores the last source-message timestamp after a restart', async () => {
	const listener = createSummaryListener({
		sourceApi: { messageList: async () => ({ data: { items: [], has_more: false } }) },
		ledger: { summaryListenerState: async () => ({ chat_id: 'oc_summary', cursor_create_time: Date.now(), last_source_create_time: 1700000000000 }), saveSummaryListenerCursor: async () => {} },
		processMessage: async () => ({}), logger: { error() {} },
		config: { enabled: true, key: 'summary', chatId: 'oc_summary', intervalSeconds: 10, historyLookbackSeconds: 3600, overlapSeconds: 30, bootstrapMode: 'forward_existing', sourceLabel: '分析师发送汇总群' },
	});
	await listener.tick();
	assert.equal(listener.status().last_source_message_at, '2023-11-14T22:13:20.000Z');
});
