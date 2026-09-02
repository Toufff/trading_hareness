import { createHash } from 'node:crypto';
import { isSystemMessage } from './message-filter.mjs';
import { filenameFromHeaders, headerValue } from './filename-utils.mjs';

const DEFAULT_HISTORY_LOOKBACK_SECONDS = 5 * 60;
const MAX_HISTORY_PAGES = 20;
const MAX_SOURCE_FILE_BYTES = 30 * 1024 * 1024;
const MAX_SOURCE_IMAGE_BYTES = 10 * 1024 * 1024;

export class RelayUnsupportedError extends Error {}

function parseJson(value, fallback = {}) {
	try { return JSON.parse(value ?? '{}'); } catch { return fallback; }
}

function cloneJson(value) {
	return JSON.parse(JSON.stringify(value ?? {}));
}

function asCreateTimeMs(value, fallback = Date.now()) {
	const parsed = Number(value);
	if (!Number.isFinite(parsed) || parsed <= 0) return fallback;
	return parsed < 100_000_000_000 ? parsed * 1000 : parsed;
}

function asEpochSeconds(value) {
	return String(Math.max(0, Math.floor(asCreateTimeMs(value) / 1000)));
}

function deterministicUuid(messageId, component, targetChatId) {
	const value = createHash('sha256').update(`feishu-group-relay:${messageId}:${component}:${targetChatId}`).digest('hex');
	return `${value.slice(0, 8)}-${value.slice(8, 12)}-4${value.slice(13, 16)}-a${value.slice(17, 20)}-${value.slice(20, 32)}`;
}

function messageText(content) {
	const value = parseJson(content, { raw: String(content ?? '') });
	return typeof value.text === 'string' ? value.text : typeof value.raw === 'string' ? value.raw : '';
}

function interactiveCardText(content) {
	const card = typeof content === 'string' ? parseJson(content, { raw: content }) : content;
	const chunks = [];
	const append = (value) => {
		if (typeof value !== 'string') return;
		const text = value.trim();
		if (text && !chunks.includes(text)) chunks.push(text);
	};
	const walk = (value) => {
		if (Array.isArray(value)) {
			for (const item of value) walk(item);
			return;
		}
		if (!value || typeof value !== 'object') return;
		const tag = String(value.tag ?? '').toLowerCase();
		if (tag === 'text' || tag === 'markdown' || tag === 'plain_text') {
			append(value.text ?? value.content);
			return;
		}
		if (tag === 'a') {
			append(value.text ?? value.content);
			append(value.href ?? value.url);
			return;
		}
		if (tag === 'button') {
			append(typeof value.text === 'object' ? value.text?.content : value.text ?? value.content);
			append(value.url ?? value.multi_url?.url ?? value.action?.url);
			return;
		}
		if (tag === 'img') {
			append(typeof value.alt === 'object' ? value.alt?.content : value.alt);
			return;
		}
		if (Object.hasOwn(value, 'title')) append(typeof value.title === 'object' ? value.title?.content : value.title);
		for (const key of ['header', 'body', 'elements', 'columns', 'fields', 'content', 'content_v2', 'note']) walk(value[key]);
	};
	walk(card);
	return chunks.join('\n');
}

function taggedText(tag, text = '') {
	const prefix = `#${tag}`;
	const normalized = String(text ?? '');
	return normalized === prefix || normalized.startsWith(`${prefix}\n`) ? normalized : `${prefix}${normalized ? `\n${normalized}` : ''}`;
}

function taggedFilename(tag, filename = 'file') {
	return `#${tag} ${String(filename || 'file')}`;
}

function fileType(filename, contentType) {
	const value = `${filename} ${contentType}`.toLowerCase();
	if (value.includes('.opus') || value.includes('audio/opus')) return 'opus';
	if (value.includes('.mp4') || value.includes('video/mp4')) return 'mp4';
	if (value.includes('.pdf') || value.includes('application/pdf')) return 'pdf';
	if (/\.(doc|docx)\b/.test(value) || value.includes('word')) return 'doc';
	if (/\.(xls|xlsx)\b/.test(value) || value.includes('spreadsheet')) return 'xls';
	if (/\.(ppt|pptx)\b/.test(value) || value.includes('presentation')) return 'ppt';
	return 'stream';
}

async function readableToBuffer(readable, maxBytes) {
	const chunks = [];
	let bytes = 0;
	for await (const chunk of readable) {
		const value = Buffer.from(chunk);
		bytes += value.length;
		if (bytes > maxBytes) throw new RelayUnsupportedError(`消息资源超过 ${Math.floor(maxBytes / 1024 / 1024)} MiB 转发上限`);
		chunks.push(value);
	}
	if (!bytes) throw new RelayUnsupportedError('消息资源为空，无法转发');
	return Buffer.concat(chunks, bytes);
}

function collectPostResources(value, found = []) {
	if (Array.isArray(value)) {
		for (const item of value) collectPostResources(item, found);
		return found;
	}
	if (!value || typeof value !== 'object') return found;
	if (typeof value.image_key === 'string') found.push({ key: value.image_key, kind: 'image' });
	if (typeof value.file_key === 'string') found.push({ key: value.file_key, kind: 'file' });
	for (const child of Object.values(value)) collectPostResources(child, found);
	return found;
}

function rewritePostResourceKeys(value, replacements) {
	if (Array.isArray(value)) return value.map((item) => rewritePostResourceKeys(item, replacements));
	if (!value || typeof value !== 'object') return value;
	const output = {};
	for (const [key, child] of Object.entries(value)) {
		if (key === 'image_key' && replacements.image.has(child)) output[key] = replacements.image.get(child);
		else if (key === 'file_key' && replacements.file.has(child)) output[key] = replacements.file.get(child);
		else output[key] = rewritePostResourceKeys(child, replacements);
	}
	return output;
}

function prependTagToPost(content, tag) {
	const output = cloneJson(content);
	const line = [{ tag: 'text', text: `#${tag}` }];
	if (output.zh_cn && typeof output.zh_cn === 'object' && Array.isArray(output.zh_cn.content)) {
		output.zh_cn.content.unshift(line);
		return output;
	}
	if (output.en_us && typeof output.en_us === 'object' && Array.isArray(output.en_us.content)) {
		output.en_us.content.unshift(line);
		return output;
	}
	if (Array.isArray(output.content)) {
		return { zh_cn: { title: typeof output.title === 'string' ? output.title : '', content: [line, ...output.content] } };
	}
	if (Array.isArray(output.content_v2)) {
		return { zh_cn: { title: typeof output.title === 'string' ? output.title : '', content: [line, ...output.content_v2] } };
	}
	for (const localized of Object.values(output)) {
		if (localized && typeof localized === 'object' && Array.isArray(localized.content)) {
			localized.content.unshift(line);
			return output;
		}
	}
	return { zh_cn: { title: '', content: [line, [{ tag: 'text', text: JSON.stringify(output) }]] } };
}

function resourceFromDirectMessage(message) {
	const content = parseJson(message?.body?.content);
	if (typeof content.image_key === 'string') return { key: content.image_key, kind: 'image' };
	if (typeof content.file_key === 'string') return { key: content.file_key, kind: 'file' };
	return null;
}

function sourceFromRecord(record) {
	return typeof record.message === 'string' ? parseJson(record.message) : record.message;
}

function uploadErrorMessage(resource, error) {
	const payload = error?.response?.data;
	const apiMessage = String(payload?.msg ?? '');
	// Do not persist the platform's authorization URL: it contains application
	// metadata and does not help an operator fix the relay.  Keep the actionable
	// permission name instead.
	if (apiMessage.includes('im:resource:upload') || apiMessage.includes('im:resource')) {
		return `${resource}失败：机器人应用缺少 im:resource:upload（或 im:resource）应用身份权限`;
	}
	const detail = apiMessage || String(error?.message ?? '未知错误');
	const code = payload?.code ? `（飞书错误 ${payload.code}）` : '';
	return `${resource}失败${code}：${detail}`;
}

export function createGroupRelay({ larkClient, sourceApi, ledger, workbench = null, config, canWrite = null, logger = console }) {
	let running = false;
	let lastUnavailableLogAt = 0;
	let lastTickStartedAt = null;
	let lastTickCompletedAt = null;
	let lastTickError = null;
	let writerState = canWrite ? 'starting' : 'not_configured';
	// A single hung larkClient/sourceApi call (network stall, no response, no
	// rejection) used to leave `running` true forever: every later tick() call
	// returns immediately at the top and polling stops for good. If a tick has
	// been "running" longer than this watchdog window, force it back to a
	// resumable state and log loudly instead of silently going dark.
	const tickWatchdogMs = Math.max(1, Number(config.tickWatchdogSeconds ?? 15 * 60) * 1000);
	const reconcileEveryMs = Math.max(60 * 60_000, Number(config.reconcileEverySeconds ?? 6 * 3600) * 1000);
	const reconcileLookbackMs = Math.max(5 * 60_000, Number(config.reconcileLookbackSeconds ?? 24 * 3600) * 1000);
	const sourceRuntime = new Map(config.sources.map((source) => [source.key, { state: 'starting', last_success_at: null, last_error: null, last_reconciled_at: null }]));
	const sourceDefinitions = new Map(config.sources.map((source) => [source.key, source]));

	function rememberSources(sources) {
		const sourceKeys = new Set(sources.map((source) => source.key));
		for (const key of sourceDefinitions.keys()) {
			if (!sourceKeys.has(key)) {
				sourceDefinitions.delete(key);
				sourceRuntime.delete(key);
			}
		}
		for (const source of sources) {
			sourceDefinitions.set(source.key, source);
			if (!sourceRuntime.has(source.key)) sourceRuntime.set(source.key, { state: source.enabled === false ? 'disabled' : 'starting', last_success_at: null, last_error: null, last_reconciled_at: null });
		}
	}

	async function configuredSources() {
		const sources = config.sourcesProvider ? await config.sourcesProvider() : config.sources;
		rememberSources(sources);
		return sources;
	}

	async function sendMessage({ targetChatId, messageId, component, msgType, content }) {
		const result = await larkClient.im.v1.message.create({
			params: { receive_id_type: 'chat_id' },
			data: { receive_id: targetChatId, msg_type: msgType, content: JSON.stringify(content), uuid: deterministicUuid(messageId, component, targetChatId) },
		});
		if (result.code && result.code !== 0) throw new Error(`飞书发送失败：${result.msg ?? result.code}`);
		if (!result.data?.message_id) throw new Error('飞书发送未返回 message_id');
		return result.data.message_id;
	}

	async function downloadAndUpload(message, descriptor) {
		let response;
		try {
			response = await sourceApi.messageResourceGet({ messageId: message.message_id, fileKey: descriptor.key, type: descriptor.kind });
		} catch (error) {
			throw new Error(`无法读取源消息资源 ${descriptor.key}：${error?.response?.status ?? error?.message ?? 'unknown'}`);
		}
		const contentType = String(headerValue(response.headers, 'content-type')).split(';')[0];
		const filename = filenameFromHeaders(response.headers, `${descriptor.kind}-${descriptor.key}`);
		const declaredBytes = Number(headerValue(response.headers, 'content-length'));
		if (Number.isFinite(declaredBytes) && declaredBytes > MAX_SOURCE_FILE_BYTES) {
			if (!workbench?.uploadToCloud) throw new RelayUnsupportedError(`消息资源超过 ${Math.floor(MAX_SOURCE_FILE_BYTES / 1024 / 1024)} MiB 转发上限；未配置云空间归档`);
			const archived = await workbench.uploadToCloud({ readable: response.getReadableStream(), fileName: filename, size: declaredBytes });
			return { ...archived, contentType };
		}
		const bytes = await readableToBuffer(response.getReadableStream(), MAX_SOURCE_FILE_BYTES);
		if (descriptor.kind === 'image' && bytes.length <= MAX_SOURCE_IMAGE_BYTES) {
			let uploaded;
			try {
				uploaded = await larkClient.im.v1.image.create({ data: { image_type: 'message', image: bytes } });
			} catch (error) {
				throw new Error(uploadErrorMessage('飞书图片上传', error));
			}
			const imageKey = uploaded?.image_key ?? uploaded?.data?.image_key;
			if (!imageKey) throw new Error('飞书图片上传未返回 image_key');
			return { kind: 'image', key: imageKey, filename };
		}
		let uploaded;
		try {
			uploaded = await larkClient.im.v1.file.create({
				data: { file_type: fileType(filename, contentType), file_name: filename, file: bytes },
			});
		} catch (error) {
			throw new Error(uploadErrorMessage('飞书文件上传', error));
		}
		const fileKey = uploaded?.file_key ?? uploaded?.data?.file_key;
		if (!fileKey) throw new Error('飞书文件上传未返回 file_key');
		return { kind: 'file', key: fileKey, filename };
	}

	async function relayPostContent(message, source) {
		const sourceContent = parseJson(message?.body?.content);
		const resources = collectPostResources(sourceContent);
		const replacements = { image: new Map(), file: new Map() };
		for (const resource of resources) {
			if (replacements[resource.kind].has(resource.key)) continue;
			const uploaded = await downloadAndUpload(message, resource);
			if (resource.kind === 'image' && uploaded.kind !== 'image') {
				throw new RelayUnsupportedError('超过 10 MiB 的富文本图片不能保留为富文本图片');
			}
			replacements[resource.kind].set(resource.key, uploaded.key);
		}
		const content = prependTagToPost(rewritePostResourceKeys(sourceContent, replacements), source.tag);
		return { component: 'post', msgType: 'post', content };
	}

	async function relayDirectResourceContent(message, source, descriptor) {
		const uploaded = await downloadAndUpload(message, descriptor);
		if (uploaded.kind === 'drive') {
			return { component: 'drive-archive', msgType: 'text', content: { text: taggedText(source.tag, `大文件已归档至配置的飞书云空间文件夹：${uploaded.filename}\n文件 token：${uploaded.fileToken}`) } };
		}
		if (uploaded.kind === 'baidu_pan') {
			return { component: 'baidu-pan-archive', msgType: 'text', content: { text: taggedText(source.tag, `大文件已归档至百度网盘：${uploaded.path}\n文件 ID：${uploaded.fsId ?? 'rapid-upload'}`) } };
		}
		if (message.msg_type !== 'media' || uploaded.kind !== 'file') {
			if (uploaded.kind === 'image') {
				return { component: 'image-post', msgType: 'post', content: { zh_cn: { title: '', content: [[{ tag: 'text', text: `#${source.tag}` }], [{ tag: 'img', image_key: uploaded.key }]] } } };
			}
			return { component: 'file', msgType: 'file', content: { file_key: uploaded.key, file_name: taggedFilename(source.tag, uploaded.filename) } };
		}
		return {
			component: 'resource-post', msgType: 'post',
			content: { zh_cn: { title: '', content: [[{ tag: 'text', text: `#${source.tag}` }], [{ tag: 'media', file_key: uploaded.key }]] } },
		};
	}

	async function relayPayload(message, source) {
		if (!message?.message_id) throw new RelayUnsupportedError('源消息没有 message_id');
		if (message.msg_type === 'post') return relayPostContent(message, source);
		const directResource = resourceFromDirectMessage(message);
		if (directResource) return relayDirectResourceContent(message, source, directResource);
		if (message.msg_type === 'text') {
			return { component: 'text', msgType: 'text', content: { text: taggedText(source.tag, messageText(message?.body?.content)) } };
		}
		// Card components and callback values are tenant-bound, but their human
		// readable text and outbound URLs are portable.  Preserve those in a
		// normal text message so every configured source group has the same relay
		// behavior without copying unusable actions or resource keys.
		if (message.msg_type === 'interactive') {
			const summary = interactiveCardText(message?.body?.content).slice(0, 3_000);
			return { component: 'interactive-text-summary-v1', msgType: 'text', content: { text: taggedText(source.tag, `[interactive]\n${summary || '卡片未提供可转发文字内容。'}`) } };
		}
		if (['sticker', 'share_chat', 'share_user', 'merge_forward', 'audio', 'system'].includes(message.msg_type)) {
			return { component: 'portable-summary', msgType: 'text', content: { text: taggedText(source.tag, `[${message.msg_type}]　${messageText(message?.body?.content).slice(0, 3_000) || '此消息类型无法跨租户保持原组件。'}`) } };
		}
		throw new RelayUnsupportedError(`暂不支持的飞书消息类型：${message.msg_type ?? 'unknown'}`);
	}

	async function relayOne(message, source) {
		const payload = await relayPayload(message, source);
		return Promise.all(source.targetChatIds.map(async (targetChatId) => ({
			targetChatId,
			messageId: await sendMessage({ targetChatId, messageId: message.message_id, ...payload }),
		})));
	}

	async function updateRelayedMessage(message, source, record) {
		const targetIds = Array.isArray(record?.target_message_ids) ? record.target_message_ids : record?.targetMessageIds;
		const targetMessages = Array.isArray(targetIds) ? targetIds : [];
		if (!targetMessages.length) return false;
		const payload = await relayPayload(message, source);
		if (!['text', 'post'].includes(payload.msgType)) return false;
		await Promise.all(targetMessages.map(async (entry) => {
			const targetMessageId = typeof entry === 'string' ? entry : entry?.messageId;
			if (!targetMessageId) return;
			const result = await larkClient.im.v1.message.update({
				path: { message_id: targetMessageId }, data: { msg_type: payload.msgType, content: JSON.stringify(payload.content) },
			});
			if (result?.code && result.code !== 0) throw new Error(`更新目标群消息失败：${result.msg ?? result.code}`);
		}));
		return true;
	}

	async function processClaimed(message, source) {
		try {
			const targetMessageIds = await relayOne(message, source);
			await ledger.markRelayMessage(message.message_id, { status: 'sent', targetMessageIds, errorMessage: null });
			if (message.msg_type === 'interactive') await ledger.markPortableSummaryVersion?.(message.message_id, 'interactive-text-summary-v1');
			// The relay contract is one source message -> one summary bubble.  An
			// action card is useful, but is deliberately opt-in so it never splits
			// the tagged original message by default.
			if (workbench && config.actionCardsEnabled === true) {
				const record = await ledger.getRelayMessage(message.message_id);
				await workbench.publishActionCard(record, source).catch((error) => logger.warn(`行动卡片创建失败：${source.key} ${message.message_id}：${error.message}`));
			}
			logger.info(`群消息已转发：${source.key} ${message.message_id}`);
		} catch (error) {
			const unsupported = error instanceof RelayUnsupportedError;
			await ledger.markRelayMessage(message.message_id, {
				status: unsupported ? 'unsupported' : 'failed', targetMessageIds: [], errorMessage: error instanceof Error ? error.message : String(error),
			});
			logger.error(`群消息转发${unsupported ? '不支持' : '失败'}：${source.key} ${message.message_id}：${error instanceof Error ? error.message : String(error)}`);
		}
	}

	async function resolveSource(source) {
		// Every route retains the main summary group; route-specific targets are
		// additional fan-out destinations (for example, the dedicated #liwei group).
		const requestedTargets = [config.targetChatId, ...(Array.isArray(source.targetChatIds) ? source.targetChatIds : []), source.targetChatId].filter(Boolean);
		const targetChatIds = [...new Set(requestedTargets.map((value) => String(value ?? '').trim()).filter(Boolean))];
		const resolvedSource = { ...source, targetChatIds, targetChatId: targetChatIds[0] };
		if (resolvedSource.chatId) return { ...resolvedSource, resolvedChatId: resolvedSource.chatId };
		if (!resolvedSource.chatName) return null;
		const result = await sourceApi.chatSearch(resolvedSource.chatName);
		if (result.code && result.code !== 0) throw new Error(`无法搜索源群 ${resolvedSource.chatName}：${result.msg ?? result.code}`);
		const chat = (result.data?.items ?? []).find((item) => item.name === resolvedSource.chatName);
		return chat?.chat_id ? { ...resolvedSource, resolvedChatId: chat.chat_id } : null;
	}

	async function retryFailed(sourcesByKey) {
		for (const record of await ledger.relayRetryQueue(20)) {
			const source = sourcesByKey.get(record.source_key);
			if (!source || !source.resolvedChatId || source.resolvedChatId !== record.source_chat_id) continue;
			const claimed = await ledger.claimRelayMessage({
				...record, sourceChatId: source.resolvedChatId, targetChatId: source.targetChatId, targetChatIds: source.targetChatIds,
				routeTag: source.tag, message: sourceFromRecord(record),
			});
			if (claimed) await processClaimed(sourceFromRecord(record), source);
		}
	}

	async function upgradePortableInteractiveSummaries(sourcesByKey) {
		if (!ledger.portableInteractiveSummaryUpgradeQueue || !ledger.markPortableSummaryVersion) return;
		for (const record of await ledger.portableInteractiveSummaryUpgradeQueue(20)) {
			const source = sourcesByKey.get(record.source_key);
			if (!source || source.resolvedChatId !== record.source_chat_id) continue;
			try {
				const updated = await updateRelayedMessage(sourceFromRecord(record), source, record);
				if (!updated) throw new Error('原转发消息缺少可更新的文本目标');
				await ledger.markPortableSummaryVersion(record.source_message_id, 'interactive-text-summary-v1');
				logger.info(`互动卡片摘要已升级：${source.key} ${record.source_message_id}`);
			} catch (error) {
				logger.warn(`互动卡片摘要升级失败：${source.key} ${record.source_message_id}：${error instanceof Error ? error.message : String(error)}`);
			}
		}
	}

	async function pollSource(source) {
		if (source.targetChatIds.includes(source.resolvedChatId)) {
			logger.error(`群消息转发已跳过：源群 ${source.key} 与汇集群相同，避免循环`);
			return;
		}
		const state = await ledger.relaySourceState(source.key);
		const now = Date.now();
		const bootstrap = !state || state.chat_id !== source.resolvedChatId;
		const normalFrom = bootstrap
			? now - config.historyLookbackSeconds * 1000
			: Math.max(0, Number(state.cursor_create_time) - config.overlapSeconds * 1000);
		const runtime = sourceRuntime.get(source.key);
		const reconciling = !bootstrap && (!runtime?.last_reconciled_at || Date.now() - Date.parse(runtime.last_reconciled_at) >= reconcileEveryMs);
		const from = reconciling ? Math.max(0, now - reconcileLookbackMs) : normalFrom;
		let pageToken;
		let newestCreateTime = now;
		for (let page = 0; page < MAX_HISTORY_PAGES; page++) {
			const result = await sourceApi.messageList({
				container_id_type: 'chat', container_id: source.resolvedChatId, start_time: asEpochSeconds(from),
				sort_type: 'ByCreateTimeAsc', page_size: 50, with_sender_name: true, ...(pageToken ? { page_token: pageToken } : {}),
			});
			if (result.code && result.code !== 0) throw new Error(`读取源群 ${source.key} 历史消息失败：${result.msg ?? result.code}`);
			for (const message of result.data?.items ?? []) {
				if (!message?.message_id) continue;
				const createTime = asCreateTimeMs(message.create_time, now);
				newestCreateTime = Math.max(newestCreateTime, createTime);
				const sourceUpdateTime = message.update_time ? asCreateTimeMs(message.update_time, createTime) : null;
				const record = {
					sourceMessageId: message.message_id, sourceKey: source.key, sourceChatId: source.resolvedChatId,
					sourceCreateTime: createTime, sourceUpdateTime, targetChatId: source.targetChatId, targetChatIds: source.targetChatIds, routeTag: source.tag, message,
				};
				// Group membership, join/leave and other system notices have no
				// analyst content. Never convert them into tagged placeholder text.
				if (isSystemMessage(message)) {
					await ledger.filterRelayMessage(record, '系统消息已过滤（如入群、退群或群设置变更）');
					continue;
				}
				const existing = await ledger.getRelayMessage(message.message_id);
				if (bootstrap && config.bootstrapMode === 'skip_existing') {
					await ledger.skipRelayMessage(record);
					continue;
				}
				if (message.deleted) {
					if (existing && !existing.source_deleted) {
						const changed = await ledger.updateRelaySourceMessage(message.message_id, { message, sourceUpdateTime, sourceDeleted: true });
						await workbench?.syncSourceChange(changed, { deleted: true });
					} else if (!existing) await ledger.skipRelayMessage(record);
					continue;
				}
				if (existing && (message.updated === true || (sourceUpdateTime && (!existing.source_update_time || sourceUpdateTime > Number(existing.source_update_time))))) {
					let originalSynced = false;
					try { originalSynced = await updateRelayedMessage(message, source, existing); }
					catch (error) { logger.warn(`同步源消息编辑失败：${source.key} ${message.message_id}：${error instanceof Error ? error.message : String(error)}`); }
					const changed = await ledger.updateRelaySourceMessage(message.message_id, { message, sourceUpdateTime, sourceDeleted: false });
					await workbench?.syncSourceChange(changed, { deleted: false, originalSynced });
					continue;
				}
				if (reconciling && !existing && createTime < normalFrom) {
					// Reconciliation must not backfill a day of old traffic after a
					// restart. It establishes durable history only for detecting later
					// edits and recalls to messages that were already forwarded.
					await ledger.skipRelayMessage(record);
					continue;
				}
				const claimed = await ledger.claimRelayMessage(record);
				if (claimed) await processClaimed(message, source);
			}
			if (!result.data?.has_more) break;
			pageToken = result.data?.page_token;
			if (!pageToken) break;
		}
		await ledger.saveRelaySourceCursor({ sourceKey: source.key, chatId: source.resolvedChatId, cursorCreateTime: newestCreateTime });
		if (reconciling) {
			const previous = sourceRuntime.get(source.key) ?? {};
			sourceRuntime.set(source.key, { ...previous, last_reconciled_at: new Date().toISOString() });
		}
	}

	async function tick() {
		if (!config.enabled) return;
		if (running) {
			const stuckForMs = lastTickStartedAt ? Date.now() - Date.parse(lastTickStartedAt) : 0;
			if (stuckForMs < tickWatchdogMs) return;
			logger.error(`群消息转发轮询看门狗复位：上一次 tick 已运行 ${Math.floor(stuckForMs / 1000)}s 仍未结束（阈值 ${Math.floor(tickWatchdogMs / 1000)}s），强制复位 running 标志并重试`);
			running = false;
		}
		if (canWrite) {
			try {
				const fence = await canWrite();
				writerState = fence?.allowed ? 'writer' : 'fenced';
				if (!fence?.allowed) {
					lastTickError = `relay 写入权归属 ${fence?.writer_id ?? '未知'}，当前实例仅观察`;
					lastTickCompletedAt = new Date().toISOString();
					return;
				}
			} catch (error) {
				writerState = 'error';
				lastTickError = `relay 写入围栏校验失败：${error instanceof Error ? error.message : String(error)}`;
				lastTickCompletedAt = new Date().toISOString();
				logger.error(lastTickError);
				return;
			}
		}
		if (!config.targetChatId) {
			lastTickError = '缺少 FEISHU_GROUP_RELAY_TARGET_CHAT_ID';
			logger.error('群消息转发未启动：缺少 FEISHU_GROUP_RELAY_TARGET_CHAT_ID');
			return;
		}
		running = true;
		lastTickStartedAt = new Date().toISOString();
		lastTickError = null;
		try {
			const sources = await configuredSources();
			const enabledSources = sources.filter((source) => source.enabled !== false);
			for (const source of sources) {
				if (source.enabled === false) sourceRuntime.set(source.key, { state: 'disabled', last_success_at: sourceRuntime.get(source.key)?.last_success_at ?? null, last_error: null, last_reconciled_at: sourceRuntime.get(source.key)?.last_reconciled_at ?? null });
			}
			const resolved = await Promise.all(enabledSources.map((source) => resolveSource(source)));
			const available = resolved.filter(Boolean);
			if (available.length !== enabledSources.length && Date.now() - lastUnavailableLogAt > 5 * 60 * 1000) {
				const missing = enabledSources.filter((source) => !available.some((item) => item.key === source.key)).map((source) => source.chatName ?? source.key);
				logger.warn(`群消息转发等待源群对机器人可见：${missing.join('、')}`);
				lastUnavailableLogAt = Date.now();
			}
			for (const source of enabledSources) {
				if (!available.some((item) => item.key === source.key)) sourceRuntime.set(source.key, { state: 'unavailable', last_success_at: null, last_error: '未找到或不可读取源群', last_reconciled_at: sourceRuntime.get(source.key)?.last_reconciled_at ?? null });
			}
			const sourcesByKey = new Map(available.map((source) => [source.key, source]));
			await retryFailed(sourcesByKey);
			await upgradePortableInteractiveSummaries(sourcesByKey);
			for (const source of available) {
				try {
					await pollSource(source);
					sourceRuntime.set(source.key, { ...sourceRuntime.get(source.key), state: 'healthy', last_success_at: new Date().toISOString(), last_error: null });
				} catch (error) {
					const message = error instanceof Error ? error.message : String(error);
					sourceRuntime.set(source.key, { ...sourceRuntime.get(source.key), state: 'error', last_success_at: sourceRuntime.get(source.key)?.last_success_at ?? null, last_error: message });
					logger.error(`群消息转发轮询失败：${source.key}：${message}`);
				}
			}
		} catch (error) {
			lastTickError = error instanceof Error ? error.message : String(error);
			logger.error(`群消息转发轮询失败：${lastTickError}`);
		} finally {
			running = false;
			lastTickCompletedAt = new Date().toISOString();
		}
	}

	return {
		tick,
		status: () => ({
			running, last_tick_started_at: lastTickStartedAt, last_tick_completed_at: lastTickCompletedAt, last_tick_error: lastTickError,
			writer_state: writerState,
			sources: [...sourceDefinitions.values()].map((source) => ({ key: source.key, tag: source.tag, chat_name: source.chatName ?? source.key, ...(sourceRuntime.get(source.key) ?? { state: 'starting', last_success_at: null, last_error: null }) })),
		}),
	};
}
