import assert from 'node:assert/strict';
import test from 'node:test';
import { Readable } from 'node:stream';
import { createGroupRelay } from './group-relay.mjs';

function createHarness(messages, { imageResponse = { image_key: 'img_target' }, imageError = null, targetChatIds = [], canWrite = null } = {}) {
	const saved = new Map();
	const sourceStates = new Map();
	const sent = [];
	const updated = [];
	const ledger = {
		relayRetryQueue: async () => [],
		portableInteractiveSummaryUpgradeQueue: async () => [],
		markPortableSummaryVersion: async (id, version) => saved.set(id, { ...saved.get(id), portableSummaryVersion: version }),
		relaySourceState: async (key) => sourceStates.get(key) ?? null,
		saveRelaySourceCursor: async ({ sourceKey, chatId, cursorCreateTime }) => sourceStates.set(sourceKey, { chat_id: chatId, cursor_create_time: cursorCreateTime }),
		getRelayMessage: async (id) => saved.get(id) ?? null,
		skipRelayMessage: async (record) => saved.set(record.sourceMessageId, { ...record, status: 'skipped_bootstrap' }),
		filterRelayMessage: async (record, reason) => saved.set(record.sourceMessageId, { ...saved.get(record.sourceMessageId), ...record, status: 'filtered_system', errorMessage: reason }),
		claimRelayMessage: async (record) => {
			if (saved.has(record.sourceMessageId)) return null;
			const claimed = { ...record, status: 'processing' }; saved.set(record.sourceMessageId, claimed); return claimed;
		},
		markRelayMessage: async (id, update) => saved.set(id, { ...saved.get(id), ...update }),
		updateRelaySourceMessage: async (id, update) => { const next = { ...saved.get(id), ...update }; saved.set(id, next); return next; },
	};
	const larkClient = {
		im: { v1: {
			message: { create: async ({ data }) => { sent.push(data); return { data: { message_id: `om_target_${sent.length}` } }; }, update: async (payload) => { updated.push(payload); return { code: 0, data: {} }; } },
			image: { create: async () => { if (imageError) throw imageError; return imageResponse; } },
			file: { create: async () => ({ file_key: 'file_target' }) },
		} },
	};
	const sourceApi = {
		messageList: async () => ({ data: { items: messages, has_more: false } }),
		messageResourceGet: async ({ fileKey }) => ({
			headers: { 'content-type': fileKey.startsWith('img') ? 'image/png' : 'video/mp4', 'content-disposition': `attachment; filename=${fileKey}.bin`, 'content-length': '5' },
			getReadableStream: () => Readable.from([Buffer.from('bytes')]),
		}),
	};
	const relay = createGroupRelay({
		larkClient, sourceApi, ledger, workbench: { publishActionCard: async () => { throw new Error('action card should be off by default'); } }, logger: { info() {}, error() {}, warn() {} },
		canWrite,
		config: {
			enabled: true, targetChatId: 'oc_summary', intervalSeconds: 10, historyLookbackSeconds: 300, overlapSeconds: 30,
			bootstrapMode: 'forward_existing', sources: [{ key: 'anqiang', tag: 'anqiang', chatId: 'oc_source', chatName: '马安强 (1)', targetChatIds }],
		},
	});
	return { relay, sent, updated, saved };
}

test('a fenced relay observes no source messages and never sends', async () => {
	const message = { message_id: 'om_fenced', msg_type: 'text', create_time: String(Date.now()), body: { content: JSON.stringify({ text: 'must not send' }) } };
	const { relay, sent } = createHarness([message], { canWrite: async () => ({ allowed: false, writer_id: 'relay-edge-47' }) });
	await relay.tick();
	assert.equal(sent.length, 0);
	assert.equal(relay.status().writer_state, 'fenced');
	assert.match(relay.status().last_tick_error, /relay 写入权归属/);
});

test('image is relayed once as one tagged rich-text bubble and source ID is deduplicated', async () => {
	const message = { message_id: 'om_image_1', msg_type: 'image', create_time: String(Date.now()), body: { content: JSON.stringify({ image_key: 'img_source' }) } };
	const { relay, sent, saved } = createHarness([message]);
	await relay.tick();
	await relay.tick();
	assert.equal(sent.length, 1);
	assert.equal(sent[0].msg_type, 'post');
	assert.deepEqual(JSON.parse(sent[0].content), { zh_cn: { title: '', content: [[{ tag: 'text', text: '#anqiang' }], [{ tag: 'img', image_key: 'img_target' }]] } });
	assert.deepEqual(saved.get('om_image_1').targetMessageIds, [{ targetChatId: 'oc_summary', messageId: 'om_target_1' }]);
});

test('a route-specific target fans one source message out to the summary and dedicated group', async () => {
	const message = { message_id: 'om_fanout_1', msg_type: 'text', create_time: String(Date.now()), body: { content: JSON.stringify({ text: 'liwei update' }) } };
	const { relay, sent, saved } = createHarness([message], { targetChatIds: ['oc_liwei_forward'] });
	await relay.tick();
	assert.equal(sent.length, 2);
	assert.deepEqual(sent.map((item) => item.receive_id), ['oc_summary', 'oc_liwei_forward']);
	assert.deepEqual(saved.get('om_fanout_1').targetMessageIds, [
		{ targetChatId: 'oc_summary', messageId: 'om_target_1' },
		{ targetChatId: 'oc_liwei_forward', messageId: 'om_target_2' },
	]);
});

test('image accepts the SDK nested response shape and reports a missing upload scope safely', async () => {
	const message = { message_id: 'om_image_nested', msg_type: 'image', create_time: String(Date.now()), body: { content: JSON.stringify({ image_key: 'img_source' }) } };
	const nested = createHarness([message], { imageResponse: { data: { image_key: 'img_target_nested' } } });
	await nested.relay.tick();
	assert.equal(JSON.parse(nested.sent[0].content).zh_cn.content[1][0].image_key, 'img_target_nested');

	const denied = createHarness([message], { imageError: { message: 'Request failed with status code 400', response: { data: { code: 99991672, msg: 'Access denied. One of the following scopes is required: [im:resource:upload, im:resource].' } } } });
	await denied.relay.tick();
	assert.equal(denied.saved.get('om_image_nested').status, 'failed');
	assert.equal(denied.saved.get('om_image_nested').errorMessage, '飞书图片上传失败：机器人应用缺少 im:resource:upload（或 im:resource）应用身份权限');
});

test('rich text retains text, image and video under one tag in one outgoing post', async () => {
	const message = {
		message_id: 'om_post_1', msg_type: 'post', create_time: String(Date.now()),
		body: { content: JSON.stringify({ title: '原始标题', content: [[{ tag: 'text', text: '原文' }, { tag: 'img', image_key: 'img_post' }], [{ tag: 'media', file_key: 'video_post' }]] }) },
	};
	const { relay, sent } = createHarness([message]);
	await relay.tick();
	assert.equal(sent.length, 1);
	assert.equal(sent[0].msg_type, 'post');
	const content = JSON.parse(sent[0].content);
	assert.deepEqual(content.zh_cn.content[0], [{ tag: 'text', text: '#anqiang' }]);
	assert.deepEqual(content.zh_cn.content[1], [{ tag: 'text', text: '原文' }, { tag: 'img', image_key: 'img_target' }]);
	assert.deepEqual(content.zh_cn.content[2], [{ tag: 'media', file_key: 'file_target' }]);
});

test('ordinary files preserve their tag in the single native file bubble', async () => {
	const message = { message_id: 'om_file_1', msg_type: 'file', create_time: String(Date.now()), body: { content: JSON.stringify({ file_key: 'file_source', file_name: 'source.pdf' }) } };
	const { relay, sent } = createHarness([message]);
	await relay.tick();
	assert.equal(sent.length, 1);
	assert.equal(sent[0].msg_type, 'file');
	assert.deepEqual(JSON.parse(sent[0].content), { file_key: 'file_target', file_name: '#anqiang file_source.bin' });
});

test('group system notices are filtered before they can be tagged or sent', async () => {
	const message = { message_id: 'om_system_1', msg_type: 'system', create_time: String(Date.now()), body: { content: JSON.stringify({ template: '{to_chatters} joined the group' }) } };
	const { relay, sent, saved } = createHarness([message]);
	await relay.tick();
	assert.equal(sent.length, 0);
	assert.equal(saved.get('om_system_1').status, 'filtered_system');
});

test('an edited text source updates the original single outgoing message instead of sending another bubble', async () => {
	const message = { message_id: 'om_text_edit_1', msg_type: 'text', create_time: String(Date.now()), body: { content: JSON.stringify({ text: '第一版' }) } };
	const { relay, sent, updated } = createHarness([message]);
	await relay.tick();
	message.updated = true;
	message.update_time = String(Date.now() + 1_000);
	message.body.content = JSON.stringify({ text: '修订版' });
	await relay.tick();
	assert.equal(sent.length, 1);
	assert.equal(updated.length, 1);
	assert.deepEqual(updated[0], { path: { message_id: 'om_target_1' }, data: { msg_type: 'text', content: JSON.stringify({ text: '#anqiang\n修订版' }) } });
});

test('an edited rich-text source refreshes image/video keys in its original outgoing post', async () => {
	const message = {
		message_id: 'om_post_edit_1', msg_type: 'post', create_time: String(Date.now()),
		body: { content: JSON.stringify({ title: '', content: [[{ tag: 'text', text: '第一版' }, { tag: 'img', image_key: 'img_edit' }], [{ tag: 'media', file_key: 'video_edit' }]] }) },
	};
	const { relay, sent, updated } = createHarness([message]);
	await relay.tick();
	message.updated = true;
	message.update_time = String(Date.now() + 1_000);
	message.body.content = JSON.stringify({ title: '', content: [[{ tag: 'text', text: '修订版' }, { tag: 'img', image_key: 'img_edit_2' }], [{ tag: 'media', file_key: 'video_edit_2' }]] });
	await relay.tick();
	assert.equal(sent.length, 1);
	assert.equal(updated.length, 1);
	assert.equal(updated[0].data.msg_type, 'post');
	const content = JSON.parse(updated[0].data.content);
	assert.deepEqual(content.zh_cn.content[0], [{ tag: 'text', text: '#anqiang' }]);
	assert.equal(content.zh_cn.content[1][0].text, '修订版');
	assert.equal(content.zh_cn.content[1][1].image_key, 'img_target');
	assert.equal(content.zh_cn.content[2][0].file_key, 'file_target');
});

test('interactive cards relay their portable text and links for every configured target group', async () => {
	const message = {
		message_id: 'om_interactive_1', msg_type: 'interactive', create_time: String(Date.now()),
		body: { content: JSON.stringify({ title: null, elements: [[
			{ tag: 'text', text: '8-23 20:04:05\n通过网盘分享的文件：安强8.23回放.mp4\n链接:' },
			{ tag: 'a', href: 'https://pan.baidu.com/s/example', text: 'https://pan.baidu.com/s/example' },
			{ tag: 'text', text: '提取码: g6ap' },
		]] }) },
	};
	const { relay, sent, saved } = createHarness([message], { targetChatIds: ['oc_anqiang_forward'] });
	await relay.tick();
	assert.equal(sent.length, 2);
	assert.ok(sent.every((item) => item.msg_type === 'text'));
	assert.deepEqual(JSON.parse(sent[0].content), { text: '#anqiang\n[interactive]\n8-23 20:04:05\n通过网盘分享的文件：安强8.23回放.mp4\n链接:\nhttps://pan.baidu.com/s/example\n提取码: g6ap' });
	assert.equal(saved.get('om_interactive_1').portableSummaryVersion, 'interactive-text-summary-v1');
});

test('an edited interactive card updates the original portable summary instead of sending a duplicate', async () => {
	const message = { message_id: 'om_interactive_edit_1', msg_type: 'interactive', create_time: String(Date.now()), body: { content: JSON.stringify({ elements: [[{ tag: 'text', text: '第一版卡片' }]] }) } };
	const { relay, sent, updated } = createHarness([message]);
	await relay.tick();
	message.updated = true;
	message.update_time = String(Date.now() + 1_000);
	message.body.content = JSON.stringify({ elements: [[{ tag: 'text', text: '修订后的卡片正文' }, { tag: 'a', text: '查看详情', href: 'https://example.test/detail' }]] });
	await relay.tick();
	assert.equal(sent.length, 1);
	assert.deepEqual(updated[0], { path: { message_id: 'om_target_1' }, data: { msg_type: 'text', content: JSON.stringify({ text: '#anqiang\n[interactive]\n修订后的卡片正文\n查看详情\nhttps://example.test/detail' }) } });
});

test('a permanently hung tick is recovered by the watchdog instead of stopping polling forever', async () => {
	const logs = [];
	const relay = createGroupRelay({
		larkClient: {}, sourceApi: {}, ledger: {},
		logger: { info() {}, warn() {}, error: (message) => logs.push(message) },
		config: {
			enabled: true, targetChatId: 'oc_summary', tickWatchdogSeconds: 0.02,
			// A sources provider that never resolves simulates a hung upstream call
			// happening after `running` has already been set true.
			sourcesProvider: () => new Promise(() => {}),
			sources: [],
		},
	});
	void relay.tick();
	await new Promise((resolve) => setTimeout(resolve, 80));
	assert.equal(relay.status().running, true, 'the first tick is still hung');
	void relay.tick();
	await new Promise((resolve) => setTimeout(resolve, 40));
	assert.ok(logs.some((message) => message.includes('看门狗复位')), 'watchdog must log the forced reset');
});
