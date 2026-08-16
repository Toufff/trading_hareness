import { createHash } from 'node:crypto';

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

function deterministicUuid(messageId, component) {
	const value = createHash('sha256').update(`feishu-group-relay:${messageId}:${component}`).digest('hex');
	return `${value.slice(0, 8)}-${value.slice(8, 12)}-4${value.slice(13, 16)}-a${value.slice(17, 20)}-${value.slice(20, 32)}`;
}

function messageText(content) {
	const value = parseJson(content, { raw: String(content ?? '') });
	return typeof value.text === 'string' ? value.text : typeof value.raw === 'string' ? value.raw : '';
}

function taggedText(tag, text = '') {
	const prefix = `#${tag}`;
	const normalized = String(text ?? '');
	return normalized === prefix || normalized.startsWith(`${prefix}\n`) ? normalized : `${prefix}${normalized ? `\n${normalized}` : ''}`;
}

function taggedFilename(tag, filename = 'file') {
	return `#${tag} ${String(filename || 'file')}`;
}

function headerValue(headers, name) {
	return headers?.[name] ?? headers?.[name.toLowerCase()] ?? headers?.[name.toUpperCase()] ?? '';
}

function filenameFromHeaders(headers, fallback) {
	const contentDisposition = String(headerValue(headers, 'content-disposition'));
	const match = contentDisposition.match(/filename\*?=(?:UTF-8''|\")?([^;\"]+)/i);
	return match ? decodeURIComponent(match[1].replace(/\"/g, '')).replace(/[^\w.\-()\u4e00-\u9fff]+/g, '_') : fallback;
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
	if (Array.isArray(output.content)) {
		output.content.unshift(line);
		return output;
	}
	if (Array.isArray(output.content_v2)) {
		output.content_v2.unshift(line);
		return output;
	}
	for (const localized of Object.values(output)) {
		if (localized && typeof localized === 'object' && Array.isArray(localized.content)) {
			localized.content.unshift(line);
			return output;
		}
	}
	return { title: '', content: [line, [{ tag: 'text', text: JSON.stringify(output) }]] };
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

export function createGroupRelay({ larkClient, sourceApi, ledger, workbench = null, config, logger = console }) {
	let running = false;
	let lastUnavailableLogAt = 0;
	let lastTickStartedAt = null;
	let lastTickCompletedAt = null;
	let lastTickError = null;
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
			data: { receive_id: targetChatId, msg_type: msgType, content: JSON.stringify(content), uuid: deterministicUuid(messageId, component) },
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
		const bytes = await readableToBuffer(response.getReadableStream(), MAX_SOURCE_FILE_BYTES);
		if (descriptor.kind === 'image' && bytes.length <= MAX_SOURCE_IMAGE_BYTES) {
			const uploaded = await larkClient.im.v1.image.create({ data: { image_type: 'message', image: bytes } });
			if (!uploaded?.image_key) throw new Error('飞书图片上传未返回 image_key');
			return { kind: 'image', key: uploaded.image_key, filename };
		}
		const uploaded = await larkClient.im.v1.file.create({
			data: { file_type: fileType(filename, contentType), file_name: filename, file: bytes },
		});
		if (!uploaded?.file_key) throw new Error('飞书文件上传未返回 file_key');
		return { kind: 'file', key: uploaded.file_key, filename };
	}

	async function relayPost(message, source) {
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
		return [await sendMessage({ targetChatId: source.targetChatId, messageId: message.message_id, component: 'post', msgType: 'post', content })];
	}

	async function relayDirectResource(message, source, descriptor) {
		const uploaded = await downloadAndUpload(message, descriptor);
		if (message.msg_type !== 'media' || uploaded.kind !== 'file') {
			if (uploaded.kind === 'image') {
				return [await sendMessage({
					targetChatId: source.targetChatId, messageId: message.message_id, component: 'image-post', msgType: 'post',
					content: { title: '', content: [[{ tag: 'text', text: `#${source.tag}` }], [{ tag: 'img', image_key: uploaded.key }]] },
				})];
			}
			return [await sendMessage({
				targetChatId: source.targetChatId, messageId: message.message_id, component: 'file', msgType: 'file',
				content: { file_key: uploaded.key, file_name: taggedFilename(source.tag, uploaded.filename) },
			})];
		}
		return [await sendMessage({
			targetChatId: source.targetChatId, messageId: message.message_id, component: 'resource-post', msgType: 'post',
			content: {
				title: '',
				content: [[{ tag: 'text', text: `#${source.tag}` }], [{ tag: 'media', file_key: uploaded.key }]],
			},
		})];
	}

	async function relayOne(message, source) {
		if (!message?.message_id) throw new RelayUnsupportedError('源消息没有 message_id');
		if (message.msg_type === 'post') return relayPost(message, source);
		const directResource = resourceFromDirectMessage(message);
		if (directResource) return relayDirectResource(message, source, directResource);
		if (message.msg_type === 'text') {
			return [await sendMessage({
				targetChatId: source.targetChatId, messageId: message.message_id, component: 'text', msgType: 'text',
				content: { text: taggedText(source.tag, messageText(message?.body?.content)) },
			})];
		}
		// Interactive cards, stickers, shared cards and merged forwards are not
		// portable across tenants. Preserve their type and payload summary in one
		// tagged text bubble, while the following action card keeps a durable
		// source reference for analyst follow-up.
		if (['interactive', 'sticker', 'share_chat', 'share_user', 'merge_forward', 'audio', 'system'].includes(message.msg_type)) {
			return [await sendMessage({
				targetChatId: source.targetChatId, messageId: message.message_id, component: 'portable-summary', msgType: 'text',
				content: { text: taggedText(source.tag, `[${message.msg_type}]　${messageText(message?.body?.content).slice(0, 3_000) || '此消息类型无法跨租户保持原组件，已保留协作卡片。'}`) },
			})];
		}
		throw new RelayUnsupportedError(`暂不支持的飞书消息类型：${message.msg_type ?? 'unknown'}`);
	}

	async function processClaimed(message, source) {
		try {
			const targetMessageIds = await relayOne(message, source);
			await ledger.markRelayMessage(message.message_id, { status: 'sent', targetMessageIds, errorMessage: null });
			if (workbench) {
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
		const resolvedSource = { ...source, targetChatId: source.targetChatId ?? config.targetChatId };
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
				...record, sourceChatId: source.resolvedChatId, targetChatId: source.targetChatId,
				routeTag: source.tag, message: sourceFromRecord(record),
			});
			if (claimed) await processClaimed(sourceFromRecord(record), source);
		}
	}

	async function pollSource(source) {
		if (source.resolvedChatId === source.targetChatId) {
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
					sourceCreateTime: createTime, sourceUpdateTime, targetChatId: source.targetChatId, routeTag: source.tag, message,
				};
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
					const changed = await ledger.updateRelaySourceMessage(message.message_id, { message, sourceUpdateTime, sourceDeleted: false });
					await workbench?.syncSourceChange(changed, { deleted: false });
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
		if (!config.enabled || running) return;
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
			sources: [...sourceDefinitions.values()].map((source) => ({ key: source.key, tag: source.tag, chat_name: source.chatName ?? source.key, ...(sourceRuntime.get(source.key) ?? { state: 'starting', last_success_at: null, last_error: null }) })),
		}),
	};
}
