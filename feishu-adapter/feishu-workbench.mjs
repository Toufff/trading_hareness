const FEISHU_API_BASE = 'https://open.feishu.cn/open-apis';
const TENANT_TOKEN_PATH = '/auth/v3/tenant_access_token/internal';
const ACTIONS = new Set(['research', 'focus', 'task', 'ignore', 'pin', 'urgent', 'digest', 'recall']);

function asText(value, limit = 1_500) {
	return String(value ?? '').replace(/[\u0000-\u001f]/g, ' ').trim().slice(0, limit);
}

function parseContent(value) {
	try { return typeof value === 'string' ? JSON.parse(value) : value ?? {}; } catch { return { text: String(value ?? '') }; }
}

function sourceExcerpt(message) {
	const content = parseContent(message?.body?.content);
	if (typeof content?.text === 'string') return asText(content.text, 700);
	if (typeof content?.title === 'string') return asText(content.title, 700);
	return `[${asText(message?.msg_type || '消息', 48)}]`;
}

function messageTimestamp(message) {
	const raw = Number(message?.create_time ?? Date.now());
	const value = raw < 100_000_000_000 ? raw * 1000 : raw;
	return new Date(value).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' });
}

export function buildActionCard({ sourceMessageId, routeTag, sourceName, message, workflowState = 'new', note = '' }) {
	const stateText = ({ new: '待处理', research: '已纳入研究', focus: '重点关注', task: '已创建任务', ignored: '已忽略', recalled: '源消息已撤回' })[workflowState] ?? workflowState;
	const action = (key, text, type = 'default') => ({ tag: 'button', text: { tag: 'plain_text', content: text }, type, value: { action: key, source_message_id: sourceMessageId } });
	return {
		config: { wide_screen_mode: true },
		header: { title: { tag: 'plain_text', content: `#${routeTag} · ${stateText}` }, template: workflowState === 'focus' ? 'orange' : workflowState === 'ignored' ? 'grey' : 'blue' },
		elements: [
			{ tag: 'div', fields: [
				{ is_short: true, text: { tag: 'lark_md', content: `**来源**\n${asText(sourceName || routeTag, 120)}` } },
				{ is_short: true, text: { tag: 'lark_md', content: `**时间**\n${messageTimestamp(message)}` } },
			] },
			{ tag: 'div', text: { tag: 'lark_md', content: `**原始消息**\n${sourceExcerpt(message) || '（媒体或空消息）'}` } },
			...(note ? [{ tag: 'note', elements: [{ tag: 'plain_text', content: asText(note, 250) }] }] : []),
			{ tag: 'action', actions: [
				action('research', '纳入研究', 'primary'), action('focus', '重点关注'), action('task', '创建任务'), action('ignore', '忽略'),
				...(workflowState === 'focus' ? [action('pin', '置顶'), action('urgent', '加急')] : []),
			] },
		],
	};
}

export function capabilityCatalog(config) {
	const configured = (value) => Boolean(String(value ?? '').trim());
	const item = (key, label, options = {}) => ({ key, label, category: options.category ?? '协作', enabled: options.enabled !== false, configured: options.configured ?? true, requires: options.requires ?? [], note: options.note ?? '' });
	return [
		item('action_cards', '汇总群行动卡片与线程回复', { category: '消息闭环', requires: ['im:message', 'cardkit:card:write'], note: '按钮、回复、更新与撤回均只作用于机器人已加入的汇总群。' }),
		item('reactions', '表情状态、已读、置顶与加急', { category: '消息闭环', requires: ['im:message.reaction:write', 'im:message:readonly', 'im:chat:write'], note: '仅对机器人可见/发出的汇总群消息可用。' }),
		item('source_reconciliation', '源群编辑/撤回周期对账', { category: '可靠性', requires: ['im:message.group_msg'], note: '外部源群由用户 token 轮询，无法依赖机器人事件。' }),
		item('message_search', '消息全文检索', { category: '智能处理', requires: ['search:message'], note: '使用用户身份搜索自己可访问的消息。' }),
		item('ocr', '图片 OCR', { category: '智能处理', requires: ['ocr:ocr'], note: '需在开发者后台申请 OCR 权限。' }),
		item('speech', '语音/视频转写', { category: '智能处理', requires: ['ai:speech_to_text'], note: '飞书文件转写能力受时长和媒体格式限制。' }),
		item('translation', '消息翻译', { category: '智能处理', requires: ['translation:translate'], note: '按需调用，原始消息仍保持不变。' }),
		item('drive', '大文件转云文档/Drive 链接', { category: '内容沉淀', configured: configured(config.driveFolderToken), requires: ['drive:drive'], note: configured(config.driveFolderToken) ? '已配置目标文件夹。' : '需要配置 FEISHU_WORKBENCH_DRIVE_FOLDER_TOKEN。' }),
		item('docs_wiki', '文档与知识库沉淀', { category: '内容沉淀', configured: configured(config.wikiSpaceId), requires: ['docx:document', 'wiki:wiki'], note: configured(config.wikiSpaceId) ? '已配置知识空间。' : '文档可直接创建；写入 Wiki 还需配置空间 token。' }),
		item('tasks', '任务创建/跟踪', { category: '协作', configured: configured(config.tasklistGuid), requires: ['task:task:write'], note: configured(config.tasklistGuid) ? '已配置任务清单。' : '需配置 FEISHU_WORKBENCH_TASKLIST_GUID。' }),
		item('base', 'Base 研究台账', { category: '内容沉淀', configured: configured(config.baseAppToken && config.baseTableId), requires: ['bitable:app'], note: configured(config.baseAppToken && config.baseTableId) ? '已配置多维表格。' : '需配置 app token 与 table id。' }),
		item('calendar', '日历提醒', { category: '协作', configured: configured(config.calendarId), requires: ['calendar:calendar'], note: configured(config.calendarId) ? '已配置目标日历。' : '需配置 FEISHU_WORKBENCH_CALENDAR_ID。' }),
		item('approval', '审批流发起', { category: '协作', configured: configured(config.approvalCode), requires: ['approval:approval'], note: configured(config.approvalCode) ? '已配置审批定义。' : '需配置 FEISHU_WORKBENCH_APPROVAL_CODE。' }),
		item('feed', 'Feed 卡片/即时提醒', { category: '协作', requires: ['im:message'], note: '可由摘要任务发送到汇总群；Feed 产品权限另行开通。' }),
		item('h5', '飞书 H5 工作台与机器人菜单', { category: '入口', configured: configured(config.publicBaseUrl), requires: ['应用网页', '机器人自定义菜单'], note: configured(config.publicBaseUrl) ? '已配置外网 HTTPS 地址。' : '需要配置 FEISHU_WORKBENCH_PUBLIC_BASE_URL，并在开发者后台登记 H5 域名和菜单。' }),
		item('group_tabs', '群菜单、群 Tab 与公告', { category: '入口', requires: ['im:chat'], note: '由开发者后台和群管理权限共同控制。' }),
		item('open_search', '企业开放搜索索引', { category: '扩展', requires: ['search:open_search'], note: '企业套餐与管理员授权可能限制此能力。' }),
		item('aily', '智能伙伴知识问答', { category: '扩展', configured: configured(config.ailyAppId), requires: ['aily:data_knowledge'], note: configured(config.ailyAppId) ? '已配置 Aily 应用。' : '需配置 FEISHU_WORKBENCH_AILY_APP_ID。' }),
	];
}

export function createFeishuWorkbench({ appId, appSecret, larkClient, ledger, userRequest, config = {}, fetchImpl = fetch, logger = console }) {
	let cachedTenantToken = null;
	let tenantTokenExpiresAt = 0;
	const targetChatId = String(config.targetChatId ?? '').trim();

	async function tenantAccessToken() {
		if (cachedTenantToken && tenantTokenExpiresAt > Date.now() + 60_000) return cachedTenantToken;
		const response = await fetchImpl(`${FEISHU_API_BASE}${TENANT_TOKEN_PATH}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ app_id: appId, app_secret: appSecret }), signal: AbortSignal.timeout(15_000) });
		let body; try { body = await response.json(); } catch { throw new Error(`飞书租户令牌响应无效（HTTP ${response.status}）`); }
		if (!response.ok || body?.code || !body?.tenant_access_token) throw new Error(`飞书租户令牌获取失败：${body?.msg ?? `HTTP ${response.status}`}`);
		cachedTenantToken = body.tenant_access_token;
		tenantTokenExpiresAt = Date.now() + Math.max(60, Number(body.expire ?? 7200) - 60) * 1000;
		return cachedTenantToken;
	}

	async function tenantRequest(path, { method = 'GET', params = {}, body } = {}) {
		const query = new URLSearchParams(Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== '').map(([key, value]) => [key, String(value)]));
		const send = async (token) => fetchImpl(`${FEISHU_API_BASE}${path}${query.size ? `?${query}` : ''}`, {
			method, headers: { authorization: `Bearer ${token}`, accept: 'application/json', ...(body === undefined ? {} : { 'content-type': 'application/json' }) },
			...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: AbortSignal.timeout(30_000),
		});
		let response = await send(await tenantAccessToken());
		if (response.status === 401 || response.status === 403) { cachedTenantToken = null; response = await send(await tenantAccessToken()); }
		let payload; try { payload = await response.json(); } catch { throw new Error(`飞书 API 响应无效（HTTP ${response.status}）`); }
		if (!response.ok || payload?.code) throw new Error(`飞书 API 请求失败：${payload?.msg ?? `HTTP ${response.status}`}`);
		return payload.data ?? payload;
	}

	async function sendMessage({ msgType, content, uuid }) {
		if (!targetChatId) throw new Error('未配置汇总群 chat_id');
		const result = await larkClient.im.v1.message.create({ params: { receive_id_type: 'chat_id' }, data: { receive_id: targetChatId, msg_type: msgType, content: JSON.stringify(content), ...(uuid ? { uuid } : {}) } });
		if (result.code && result.code !== 0) throw new Error(`发送汇总群消息失败：${result.msg ?? result.code}`);
		return result.data?.message_id ?? null;
	}

	async function publishActionCard(record, source) {
		if (!record || !targetChatId) return null;
		const card = buildActionCard({ sourceMessageId: record.source_message_id ?? record.sourceMessageId, routeTag: source?.tag ?? record.route_tag, sourceName: source?.chatName, message: record.message, workflowState: record.workflow_state ?? 'new', note: record.workflow_note ?? '' });
		const messageId = await sendMessage({ msgType: 'interactive', content: card });
		if (messageId) await ledger.setRelayActionCard(record.source_message_id, messageId);
		return messageId;
	}

	async function updateActionCard(record) {
		if (!record?.action_card_message_id) return null;
		const card = buildActionCard({ sourceMessageId: record.source_message_id, routeTag: record.route_tag, sourceName: record.source_chat_name, message: record.message, workflowState: record.workflow_state ?? 'new', note: record.workflow_note ?? '' });
		return tenantRequest(`/im/v1/messages/${encodeURIComponent(record.action_card_message_id)}`, { method: 'PATCH', body: { content: JSON.stringify(card) } });
	}

	async function replyToAction(record, text) {
		if (!record?.action_card_message_id) return null;
		return tenantRequest(`/im/v1/messages/${encodeURIComponent(record.action_card_message_id)}/reply`, { method: 'POST', body: { msg_type: 'text', content: JSON.stringify({ text: asText(text, 3500) }) } });
	}

	async function createTask(record, operatorOpenId) {
		if (!config.tasklistGuid) throw new Error('尚未配置 FEISHU_WORKBENCH_TASKLIST_GUID');
		if (record.workflow_state === 'task') throw new Error('该消息已经创建过飞书任务');
		const summary = `#${record.route_tag} ${sourceExcerpt(record.message).slice(0, 80) || '群消息跟进'}`;
		const description = `来源群：${record.source_chat_name ?? record.route_tag}\n源消息 ID：${record.source_message_id}\n\n${sourceExcerpt(record.message)}`;
		const body = { summary, description, tasklists: [{ guid: config.tasklistGuid }], ...(operatorOpenId ? { members: [{ id: operatorOpenId, type: 'user', role: 'assignee' }] } : {}) };
		return tenantRequest('/task/v2/tasks', { method: 'POST', params: operatorOpenId ? { user_id_type: 'open_id' } : {}, body });
	}

	async function performAction({ sourceMessageId, action, operatorOpenId, operatorName = '' }) {
		if (!ACTIONS.has(action)) throw new Error('不支持的协作动作');
		const record = await ledger.getRelayMessage(sourceMessageId);
		if (!record) throw new Error('未找到对应的源消息记录');
		let workflowState = record.workflow_state ?? 'new';
		let note = '';
		let external = null;
		if (action === 'research') { workflowState = 'research'; note = operatorName ? `${operatorName} 已纳入研究` : '已纳入研究'; }
		if (action === 'focus') { workflowState = 'focus'; note = operatorName ? `${operatorName} 标记为重点关注` : '已标记为重点关注'; }
		if (action === 'ignore') { workflowState = 'ignored'; note = operatorName ? `${operatorName} 已忽略` : '已忽略'; }
		if (action === 'task') { external = await createTask(record, operatorOpenId); workflowState = 'task'; note = '已创建飞书任务'; }
		if (action === 'pin') { if (!record.action_card_message_id) throw new Error('行动卡片尚未创建'); external = await tenantRequest('/im/v1/pins', { method: 'POST', body: { message_id: record.action_card_message_id } }); note = '已置顶行动卡片'; }
		if (action === 'urgent') { if (!record.action_card_message_id) throw new Error('行动卡片尚未创建'); external = await tenantRequest(`/im/v1/messages/${encodeURIComponent(record.action_card_message_id)}/urgent_app`, { method: 'PATCH', body: { user_id_list: operatorOpenId ? [operatorOpenId] : [] } }); note = '已发送加急提醒'; }
		if (action === 'recall') { if (!record.action_card_message_id) throw new Error('行动卡片尚未创建'); external = await tenantRequest(`/im/v1/messages/${encodeURIComponent(record.action_card_message_id)}`, { method: 'DELETE' }); workflowState = 'recalled'; note = '已撤回汇总群行动卡片'; }
		if (action === 'digest') { external = await replyToAction(record, `#${record.route_tag} 已加入本轮摘要。`); note = '已加入摘要'; }
		const updated = await ledger.updateRelayWorkflow(sourceMessageId, { workflowState, workflowNote: note, actorOpenId: operatorOpenId || null, action });
		if (action !== 'recall') await updateActionCard(updated).catch((error) => logger.warn(`更新行动卡片失败：${error.message}`));
		if (['research', 'focus', 'task', 'ignore'].includes(action)) await replyToAction(updated, `#${updated.route_tag} ${note}`).catch((error) => logger.warn(`发送行动线程回复失败：${error.message}`));
		return { record: updated, external };
	}

	async function createDocument({ title }) {
		return tenantRequest('/docx/v1/documents', { method: 'POST', body: { title: asText(title, 250), ...(config.driveFolderToken ? { folder_token: config.driveFolderToken } : {}) } });
	}

	async function createBaseRecord({ fields }) {
		if (!config.baseAppToken || !config.baseTableId) throw new Error('尚未配置 Base app token 与 table id');
		return tenantRequest(`/bitable/v1/apps/${encodeURIComponent(config.baseAppToken)}/tables/${encodeURIComponent(config.baseTableId)}/records`, { method: 'POST', body: { fields: fields ?? {} } });
	}

	async function createCalendarEvent({ summary, description = '', startTime, endTime }) {
		if (!config.calendarId) throw new Error('尚未配置 FEISHU_WORKBENCH_CALENDAR_ID');
		return tenantRequest(`/calendar/v4/calendars/${encodeURIComponent(config.calendarId)}/events`, { method: 'POST', body: { summary: asText(summary, 250), description: asText(description, 3000), start_time: { timestamp: String(Math.floor(new Date(startTime).getTime() / 1000)) }, end_time: { timestamp: String(Math.floor(new Date(endTime).getTime() / 1000)) } } });
	}

	async function createApproval({ form }) {
		if (!config.approvalCode) throw new Error('尚未配置 FEISHU_WORKBENCH_APPROVAL_CODE');
		return tenantRequest('/approval/v4/instances', { method: 'POST', body: { approval_code: config.approvalCode, form: JSON.stringify(form ?? {}) } });
	}

	async function searchMessages({ query, pageToken }) {
		if (!userRequest) throw new Error('用户 OAuth 不可用');
		const text = asText(query, 200);
		if (!text) throw new Error('请输入检索关键词');
		return userRequest('/search/v2/message', { method: 'POST', body: { query: text, page_size: 20, ...(pageToken ? { page_token: pageToken } : {}) } });
	}

	async function syncSourceChange(record, { deleted = false } = {}) {
		if (!record) return null;
		if (deleted) {
			const targetIds = [...new Set([...(Array.isArray(record.target_message_ids) ? record.target_message_ids : []), record.action_card_message_id].filter(Boolean))];
			if (!targetIds.length) return record;
			for (const messageId of targetIds) {
				await tenantRequest(`/im/v1/messages/${encodeURIComponent(messageId)}`, { method: 'DELETE' }).catch((error) => logger.warn(`撤回汇总群同步消息失败：${error.message}`));
			}
			return ledger.updateRelayWorkflow(record.source_message_id, { workflowState: 'recalled', workflowNote: '源群消息已撤回，汇总群副本已同步撤回', actorOpenId: null, action: 'source_recalled' });
		}
		await updateActionCard(record).catch((error) => logger.warn(`同步源消息编辑到行动卡片失败：${error.message}`));
		await replyToAction(record, `#${record.route_tag} 源群消息已编辑；行动卡片中的原始消息已更新。`).catch((error) => logger.warn(`通知源消息编辑失败：${error.message}`));
		return record;
	}

	return {
		status: async () => ({ target_configured: Boolean(targetChatId), public_h5_url: config.publicBaseUrl ? `${String(config.publicBaseUrl).replace(/\/$/, '')}/workbench` : null, capabilities: capabilityCatalog(config) }),
		publishActionCard, performAction, createDocument, createBaseRecord, createCalendarEvent, createApproval, searchMessages, syncSourceChange,
		buildActionCard: (record) => buildActionCard({ sourceMessageId: record.source_message_id, routeTag: record.route_tag, sourceName: record.source_chat_name, message: record.message, workflowState: record.workflow_state, note: record.workflow_note }),
	};
}
