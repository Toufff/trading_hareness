import { createHash } from 'node:crypto';

function deterministicUuid(sourceMessageId, targetChatId) {
	const value = createHash('sha256').update(`wechat-group-relay:${sourceMessageId}:${targetChatId}`).digest('hex');
	return `${value.slice(0, 8)}-${value.slice(8, 12)}-4${value.slice(13, 16)}-a${value.slice(17, 20)}-${value.slice(20, 32)}`;
}

function normalizeText(value) {
	return String(value ?? '').replace(/\r\n/g, '\n').trim();
}

function taggedText(tag, text) {
	const prefix = `#${tag}`;
	const normalized = normalizeText(text);
	return normalized === prefix || normalized.startsWith(`${prefix}\n`)
		? normalized
		: `${prefix}${normalized ? `\n${normalized}` : ''}`;
}

function asCreateTimeMs(value) {
	const parsed = Number(value);
	if (!Number.isFinite(parsed) || parsed <= 0) return Date.now();
	return parsed < 100_000_000_000 ? parsed * 1000 : parsed;
}

function formatCreateTime(value) {
	const date = new Date(asCreateTimeMs(value));
	const parts = new Intl.DateTimeFormat('en-CA', {
		timeZone: 'Asia/Shanghai',
		year: 'numeric', month: '2-digit', day: '2-digit',
		hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
	}).formatToParts(date).reduce((result, part) => {
		result[part.type] = part.value;
		return result;
	}, {});
	return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

function shanghaiDateParts(value) {
	const date = new Date(asCreateTimeMs(value));
	const parts = new Intl.DateTimeFormat('en-CA', {
		timeZone: 'Asia/Shanghai',
		year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
	}).formatToParts(date).reduce((result, part) => {
		result[part.type] = part.value;
		return result;
	}, {});
	return { date: `${parts.year}-${parts.month}-${parts.day}`, time: `${parts.hour}:${parts.minute}` };
}

export function validateWeChatRelayPayload(payload, config) {
	const sourceMessageId = normalizeText(payload?.source_message_id);
	const sourceChatId = normalizeText(payload?.source_chat_id);
	const text = normalizeText(payload?.text);
	if (!sourceMessageId) throw new Error('source_message_id is required');
	if (!sourceChatId || sourceChatId !== config.sourceChatId) throw new Error('source_chat_id is not the configured WeChat group');
	if (!text) throw new Error('text is required');
	if (text.length > config.maxTextLength) throw new Error(`text exceeds ${config.maxTextLength} characters`);
	return {
		sourceMessageId,
		sourceChatId,
		sourceCreateTime: asCreateTimeMs(payload.source_create_time),
		sourceKey: config.sourceKey,
		routeTag: config.routeTag,
		targetChatId: config.targetChatId,
		text,
		sender: normalizeText(payload.sender).slice(0, 120),
		messageType: normalizeText(payload.message_type || 'text').slice(0, 40),
		sourceChatName: normalizeText(payload.source_chat_name || '小蓝炒股会').slice(0, 120),
	};
}

export function createWeChatGroupRelay({ larkClient, ledger, config, logger = console }) {
	if (!config?.targetChatId) throw new Error('WECHAT_GROUP_RELAY_TARGET_CHAT_ID is required');
	if (!config?.sourceChatId) throw new Error('WECHAT_GROUP_RELAY_SOURCE_CHAT_ID is required');

	async function process(payload) {
		const input = validateWeChatRelayPayload(payload, config);
		const sender = input.sender || '未知发送者';
		// Match the existing Feishu archive convention: tag on its own line,
		// followed by source chat, source-local timestamp, and sender.
		const header = `${input.sourceChatName}-${formatCreateTime(input.sourceCreateTime)}${sender}:`;
		// Backfill: when the WeChat message is from an earlier day (Asia/Shanghai)
		// than today, prepend an explicit `YYYY-MM-DD HH:mm` line so downstream
		// extractImportContent files it under the ORIGINAL date, not the receipt
		// date. Same-day (live) messages omit it by design — receipt is provenance.
		const stamp = shanghaiDateParts(input.sourceCreateTime);
		const isBackfill = stamp.date !== shanghaiDateParts(Date.now()).date;
		const bodyText = `${isBackfill ? `${stamp.date} ${stamp.time}\n` : ''}${header}\n${input.text}`;
		const media = Array.isArray(payload?.media) ? payload.media.slice(0, 4) : [];
		for (const item of media) {
			if (!String(item?.media_type ?? '').startsWith('image/')) throw new Error('only image media is supported by the WeChat Feishu relay');
			if (!String(item?.data_base64 ?? '').trim()) throw new Error('media.data_base64 is required');
		}
		const message = {
			message_id: input.sourceMessageId,
			msg_type: 'text',
			create_time: String(input.sourceCreateTime),
			body: { content: JSON.stringify({ text: bodyText }) },
			source: 'wechat',
			message_type: media.length ? 'image' : input.messageType,
		};
		const record = {
			sourceMessageId: input.sourceMessageId,
			sourceKey: input.sourceKey,
			sourceChatId: input.sourceChatId,
			sourceCreateTime: input.sourceCreateTime,
			targetChatId: input.targetChatId,
			routeTag: input.routeTag,
			message,
		};
		const claimed = await ledger.claimRelayMessage(record);
		if (!claimed) return { status: 'duplicate', source_message_id: input.sourceMessageId };
		try {
			let result;
			let msgType = 'text';
			let content = { text: taggedText(input.routeTag, bodyText) };
			if (media.length) {
				const uploaded = [];
				for (const item of media) {
					const image = await larkClient.im.v1.image.create({ data: { image_type: 'message', image: Buffer.from(String(item.data_base64), 'base64') } });
					const imageKey = image?.image_key ?? image?.data?.image_key;
					if (!imageKey) throw new Error('飞书图片上传未返回 image_key');
					uploaded.push(imageKey);
				}
				msgType = 'post';
				content = { zh_cn: { title: '', content: [[{ tag: 'text', text: `#${input.routeTag}` }], [{ tag: 'text', text: bodyText }], ...uploaded.map((imageKey) => [{ tag: 'img', image_key: imageKey }]) ] } };
			}
			result = await larkClient.im.v1.message.create({
				params: { receive_id_type: 'chat_id' },
				data: {
					receive_id: input.targetChatId,
					msg_type: msgType,
					content: JSON.stringify(content),
					uuid: deterministicUuid(input.sourceMessageId, input.targetChatId),
				},
			});
			if (result?.code && result.code !== 0) throw new Error(`飞书发送失败：${result.msg ?? result.code}`);
			const targetMessageId = result?.data?.message_id ?? result?.message_id;
			if (!targetMessageId) throw new Error('飞书发送未返回 message_id');
			await ledger.markRelayMessage(input.sourceMessageId, { status: 'sent', targetMessageIds: [{ targetChatId: input.targetChatId, messageId: targetMessageId }], errorMessage: null });
			logger.info(`微信群消息已转发：${input.sourceMessageId}`);
			return { status: 'sent', source_message_id: input.sourceMessageId, target_message_id: targetMessageId };
		} catch (error) {
			const messageText = error instanceof Error ? error.message : String(error);
			await ledger.markRelayMessage(input.sourceMessageId, { status: 'failed', targetMessageIds: [], errorMessage: messageText });
			logger.error(`微信群消息转发失败：${input.sourceMessageId}：${messageText}`);
			throw error;
		}
	}

	return { process };
}
