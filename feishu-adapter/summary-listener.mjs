const MAX_HISTORY_PAGES = 20;

function asCreateTimeMs(value, fallback = Date.now()) {
	const parsed = Number(value);
	if (!Number.isFinite(parsed) || parsed <= 0) return fallback;
	return parsed < 100_000_000_000 ? parsed * 1000 : parsed;
}

function asEpochSeconds(value) {
	return String(Math.max(0, Math.floor(asCreateTimeMs(value) / 1000)));
}

function normalizeMessage(item, chatId, sourceLabel) {
	return {
		event_id: `summary-history:${item.message_id}`,
		event_type: 'im.message.history_v1',
		source: 'summary-group-poll',
		source_label: sourceLabel,
		message: {
			message_id: item.message_id,
			chat_id: item.chat_id ?? chatId,
			chat_type: item.chat_type ?? 'group',
			message_type: item.message_type ?? item.msg_type ?? '',
			content: item.content ?? item.body?.content ?? '',
			create_time: item.create_time,
			update_time: item.update_time,
			deleted: Boolean(item.deleted),
		},
		sender: item.sender ?? {},
	};
}

/**
 * Observes the summary group with a user token.  It deliberately does not use
 * im.message.receive_v1: Feishu excludes a bot's own group messages from that
 * event, while this listener must see both human and bot-authored messages.
 */
export function createSummaryListener({ sourceApi, ledger, processMessage, config, canWrite = null, logger = console }) {
	let running = false;
	let lastTickStartedAt = null;
	let lastTickCompletedAt = null;
	let lastSuccessAt = null;
	let lastError = null;
	let lastMessageAt = null;
	let processedCount = 0;
	let duplicateCount = 0;
	let ignoredCount = 0;
	let writerState = canWrite ? 'starting' : 'not_configured';

	async function tick() {
		if (!config.enabled || running) return;
		if (canWrite) {
			try {
				const fence = await canWrite();
				writerState = fence?.allowed ? 'writer' : 'fenced';
				if (!fence?.allowed) {
					lastError = `relay 写入权归属 ${fence?.writer_id ?? '未知'}，当前实例仅观察`;
					lastTickCompletedAt = new Date().toISOString();
					return;
				}
			} catch (error) {
				writerState = 'error';
				lastError = `relay 写入围栏校验失败：${error instanceof Error ? error.message : String(error)}`;
				lastTickCompletedAt = new Date().toISOString();
				logger.error(lastError);
				return;
			}
		}
		if (!config.chatId) {
			lastError = '缺少 FEISHU_SUMMARY_LISTENER_CHAT_ID';
			logger.error(`汇总群监听未启动：${lastError}`);
			return;
		}
		running = true;
		lastTickStartedAt = new Date().toISOString();
		lastError = null;
		try {
			const state = await ledger.summaryListenerState(config.key);
			if (!lastMessageAt && state?.last_source_create_time) lastMessageAt = new Date(asCreateTimeMs(state.last_source_create_time)).toISOString();
			const now = Date.now();
			const bootstrap = !state || state.chat_id !== config.chatId;
			// Existing installations did not persist this display value. Re-read the
			// bounded window once to populate it; message-id idempotency prevents a
			// second delivery while restoring the health dashboard after upgrade.
			const needsSourceTimestamp = !state?.last_source_create_time;
			const from = bootstrap || needsSourceTimestamp
				? now - config.historyLookbackSeconds * 1000
				: Math.max(0, Number(state.cursor_create_time) - config.overlapSeconds * 1000);
			let pageToken;
			let newestCreateTime = now;
			let newestSourceCreateTime = Number(state?.last_source_create_time) || 0;
			for (let page = 0; page < MAX_HISTORY_PAGES; page++) {
				const result = await sourceApi.messageList({
					container_id_type: 'chat', container_id: config.chatId, start_time: asEpochSeconds(from),
					sort_type: 'ByCreateTimeAsc', page_size: 50, with_sender_name: true, ...(pageToken ? { page_token: pageToken } : {}),
				});
				if (result.code && result.code !== 0) throw new Error(`读取汇总群历史消息失败：${result.msg ?? result.code}`);
				for (const item of result.data?.items ?? []) {
					if (!item?.message_id) continue;
					const createTime = asCreateTimeMs(item.create_time, now);
					newestCreateTime = Math.max(newestCreateTime, createTime);
					newestSourceCreateTime = Math.max(newestSourceCreateTime, createTime);
					lastMessageAt = new Date(createTime).toISOString();
					if (bootstrap && config.bootstrapMode === 'skip_existing') {
						ignoredCount += 1;
						continue;
					}
					const outcome = await processMessage(normalizeMessage(item, config.chatId, config.sourceLabel));
					if (outcome?.ignored) ignoredCount += 1;
					else if (outcome?.duplicate) duplicateCount += 1;
					else processedCount += 1;
				}
				if (!result.data?.has_more) break;
				pageToken = result.data?.page_token;
				if (!pageToken) break;
			}
			await ledger.saveSummaryListenerCursor({ listenerKey: config.key, chatId: config.chatId, cursorCreateTime: newestCreateTime, lastSourceCreateTime: newestSourceCreateTime || null });
			lastSuccessAt = new Date().toISOString();
		} catch (error) {
			lastError = error instanceof Error ? error.message : String(error);
			logger.error(`汇总群监听失败：${lastError}`);
		} finally {
			running = false;
			lastTickCompletedAt = new Date().toISOString();
		}
	}

	return {
		tick,
		status: () => ({
			enabled: config.enabled, chat_configured: Boolean(config.chatId), state: !config.enabled ? 'disabled' : lastError ? 'error' : lastSuccessAt ? 'healthy' : 'starting',
			interval_seconds: config.intervalSeconds, history_lookback_seconds: config.historyLookbackSeconds,
			last_tick_started_at: lastTickStartedAt, last_tick_completed_at: lastTickCompletedAt,
			last_success_at: lastSuccessAt, last_error: lastError, last_source_message_at: lastMessageAt,
			processed_count: processedCount, duplicate_count: duplicateCount, ignored_count: ignoredCount,
			writer_state: writerState,
		}),
	};
}
