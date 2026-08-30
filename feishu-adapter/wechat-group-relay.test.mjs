import assert from 'node:assert/strict';
import test from 'node:test';
import { createWeChatGroupRelay, validateWeChatRelayPayload } from './wechat-group-relay.mjs';

function harness() {
	const saved = new Map();
	const sent = [];
	const config = { sourceKey: 'wechat_xiaolan', sourceChatId: '50136408612@chatroom', routeTag: 'xiaolan', targetChatId: 'oc_summary', maxTextLength: 3500 };
	const ledger = {
		claimRelayMessage: async (record) => {
			if (saved.has(record.sourceMessageId)) return null;
			saved.set(record.sourceMessageId, { ...record, status: 'processing' });
			return record;
		},
		markRelayMessage: async (id, update) => saved.set(id, { ...saved.get(id), ...update }),
	};
	const larkClient = { im: { v1: { message: { create: async ({ data }) => { sent.push(data); return { data: { message_id: 'om_target' } }; } }, image: { create: async () => ({ data: { image_key: 'img_target' } }) } } } };
	return { config, ledger, larkClient, sent, saved };
}

test('validates the fixed WeChat group and preserves sender text', () => {
	const { config } = harness();
	const value = validateWeChatRelayPayload({ source_message_id: 'wx-1', source_chat_id: config.sourceChatId, source_create_time: 1_700_000_000, sender: 'wxid_a', text: '看盘' }, config);
	assert.equal(value.sourceCreateTime, 1_700_000_000_000);
	assert.equal(value.sender, 'wxid_a');
	assert.throws(() => validateWeChatRelayPayload({ source_message_id: 'wx-1', source_chat_id: 'other', text: '看盘' }, config), /configured WeChat group/);
});

test('posts one tagged message and deduplicates the source ID', async () => {
	const h = harness();
	const relay = createWeChatGroupRelay({ ...h, logger: { info() {}, error() {} } });
	const payload = { source_message_id: 'wx-1', source_chat_id: h.config.sourceChatId, source_create_time: 1_700_000_000, sender: 'wxid_a', text: '看盘' };
	assert.deepEqual(await relay.process(payload), { status: 'sent', source_message_id: 'wx-1', target_message_id: 'om_target' });
	assert.deepEqual(await relay.process(payload), { status: 'duplicate', source_message_id: 'wx-1' });
	assert.equal(h.sent.length, 1);
	assert.deepEqual(JSON.parse(h.sent[0].content), { text: '#xiaolan\n2023-11-15 06:13\n小蓝炒股会-2023-11-15 06:13:20wxid_a:\n看盘' });
	assert.equal(h.saved.get('wx-1').status, 'sent');
});

test('uploads and posts an image in one tagged Feishu post', async () => {
	const h = harness();
	const relay = createWeChatGroupRelay({ ...h, logger: { info() {}, error() {} } });
	const result = await relay.process({ source_message_id: 'wx-image-1', source_chat_id: h.config.sourceChatId, source_create_time: 1_700_000_000, text: '图片', media: [{ media_type: 'image/jpeg', data_base64: Buffer.from('jpeg').toString('base64') }] });
	assert.equal(result.status, 'sent');
	assert.equal(h.sent[0].msg_type, 'post');
	assert.deepEqual(JSON.parse(h.sent[0].content).zh_cn.content, [[{ tag: 'text', text: '#xiaolan' }], [{ tag: 'text', text: '2023-11-15 06:13\n小蓝炒股会-2023-11-15 06:13:20未知发送者:\n图片' }], [{ tag: 'img', image_key: 'img_target' }]]);
});

test('same-day (live) message omits the explicit content_date prefix', async () => {
	const h = harness();
	const relay = createWeChatGroupRelay({ ...h, logger: { info() {}, error() {} } });
	const nowSeconds = Math.floor(Date.now() / 1000);
	await relay.process({ source_message_id: 'wx-today', source_chat_id: h.config.sourceChatId, source_create_time: nowSeconds, sender: 'wxid_a', text: '看盘' });
	const text = JSON.parse(h.sent[0].content).text;
	assert.ok(text.startsWith('#xiaolan\n小蓝炒股会-'), '今天的消息不应带补传日期前缀');
	assert.ok(!/^#xiaolan\n\d{4}-\d{2}-\d{2} \d{2}:\d{2}\n/.test(text), '今天的消息正文开头不应是裸时间戳');
});

test('backfill (older day) message prepends explicit YYYY-MM-DD HH:mm', async () => {
	const h = harness();
	const relay = createWeChatGroupRelay({ ...h, logger: { info() {}, error() {} } });
	await relay.process({ source_message_id: 'wx-old', source_chat_id: h.config.sourceChatId, source_create_time: 1_700_000_000, sender: 'wxid_a', text: '看盘' });
	const text = JSON.parse(h.sent[0].content).text;
	assert.ok(/^#xiaolan\n2023-11-15 06:13\n/.test(text), '补传消息应在正文开头带原始日期时间');
});
