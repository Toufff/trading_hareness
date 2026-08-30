import { createHash } from 'node:crypto';

const FEISHU_API_BASE = 'https://open.feishu.cn/open-apis';
const TENANT_TOKEN_PATH = '/auth/v3/tenant_access_token/internal';
const ACTIONS = new Set(['research', 'focus', 'task', 'ignore', 'pin', 'urgent', 'digest', 'archive', 'recall', 'translate', 'ocr', 'transcribe']);
const MAX_OCR_BYTES = 5 * 1024 * 1024;
const MAX_ASR_BYTES = 30 * 1024 * 1024;

function asText(value, limit = 1_500) {
	return String(value ?? '').replace(/[\u0000-\u001f]/g, ' ').trim().slice(0, limit);
}

function documentContent(value, limit = 50_000) {
	return String(value ?? '').replace(/\u0000/g, '').trim().slice(0, limit);
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

function collectMessageResources(value, found = []) {
	if (Array.isArray(value)) { for (const child of value) collectMessageResources(child, found); return found; }
	if (!value || typeof value !== 'object') return found;
	if (typeof value.image_key === 'string') found.push({ kind: 'image', key: value.image_key });
	if (typeof value.file_key === 'string') found.push({ kind: 'file', key: value.file_key });
	for (const child of Object.values(value)) collectMessageResources(child, found);
	return found;
}

function messageResources(message) {
	return collectMessageResources(parseContent(message?.body?.content));
}

function messagePlainText(message) {
	const content = parseContent(message?.body?.content);
	if (typeof content?.text === 'string') return asText(content.text, 8_000);
	const values = [];
	const walk = (node) => {
		if (Array.isArray(node)) { node.forEach(walk); return; }
		if (!node || typeof node !== 'object') return;
		if (typeof node.text === 'string') values.push(node.text);
		for (const value of Object.values(node)) walk(value);
	};
	walk(content);
	return asText(values.join(''), 8_000);
}

async function readableToBuffer(readable, maxBytes) {
	const chunks = []; let total = 0;
	for await (const chunk of readable) {
		const value = Buffer.from(chunk); total += value.length;
		if (total > maxBytes) throw new Error(`媒体超过 ${Math.floor(maxBytes / 1024 / 1024)} MiB 智能处理上限`);
		chunks.push(value);
	}
	if (!total) throw new Error('媒体为空，无法处理');
	return Buffer.concat(chunks, total);
}

function mediaFormat(filename, contentType = '') {
	const value = `${filename ?? ''} ${contentType}`.toLowerCase();
	if (value.includes('.opus') || value.includes('audio/opus')) return 'opus';
	if (value.includes('.mp3') || value.includes('audio/mpeg')) return 'mp3';
	if (value.includes('.wav') || value.includes('audio/wav')) return 'wav';
	if (value.includes('.m4a') || value.includes('audio/mp4')) return 'm4a';
	return null;
}

function feishuSdkErrorMessage(error, operation) {
	const response = error?.response;
	const body = response?.data ?? {};
	const code = Number(body?.code);
	const detail = asText(body?.msg || error?.message || '未知错误', 300);
	if (code === 99991400) {
		const retryAfter = Math.max(1, Number(response?.headers?.['x-ogw-ratelimit-reset'] ?? 0) || 1);
		return `飞书${operation}触发频控（99991400），请在约 ${retryAfter} 秒后重试`;
	}
	return `飞书${operation}失败${Number.isFinite(code) && code ? `（${code}）` : ''}：${detail}`;
}

async function callFeishuSdk(operation, callback) {
	try {
		return await callback();
	} catch (error) {
		throw new Error(feishuSdkErrorMessage(error, operation));
	}
}

function taggedText(tag, text = '') {
	const prefix = `#${tag}`;
	const normalized = String(text ?? '');
	return normalized === prefix || normalized.startsWith(`${prefix}\n`) ? normalized : `${prefix}${normalized ? `\n${normalized}` : ''}`;
}

function dropReadonlyDocumentFields(value) {
	if (Array.isArray(value)) return value.map(dropReadonlyDocumentFields);
	if (!value || typeof value !== 'object') return value;
	const output = {};
	for (const [key, child] of Object.entries(value)) {
		if (key !== 'merge_info') output[key] = dropReadonlyDocumentFields(child);
	}
	return output;
}

function applicationScopeNames(value, found = new Set()) {
	if (typeof value === 'string') { if (value) found.add(value); return found; }
	if (Array.isArray(value)) { for (const item of value) applicationScopeNames(item, found); return found; }
	if (!value || typeof value !== 'object') return found;
	for (const key of ['scope', 'scope_name', 'scope_id', 'name']) {
		if (typeof value[key] === 'string' && value[key]) found.add(value[key]);
	}
	return found;
}

function recordArchiveMarkdown(record) {
	const intelligence = record?.intelligence && typeof record.intelligence === 'object' ? record.intelligence : {};
	const sections = [
		`# ${asText(record?.route_tag || 'relay', 80)} 群消息`,
		`- 来源群：${asText(record?.source_chat_name || record?.route_tag, 180)}`,
		`- 源消息 ID：${asText(record?.source_message_id, 180)}`,
		`- 接收时间：${asText(record?.created_at, 80)}`,
		'',
		'## 原始内容',
		messagePlainText(record?.message) || sourceExcerpt(record?.message),
	];
	if (intelligence.translate?.translated_text) sections.push('', '## 翻译', asText(intelligence.translate.translated_text, 8_000));
	if (intelligence.ocr?.text) sections.push('', '## 图片 OCR', asText(intelligence.ocr.text, 8_000));
	if (intelligence.transcribe?.text) sections.push('', '## 音频转写', asText(intelligence.transcribe.text, 8_000));
	return sections.join('\n');
}

function primaryTargetMessageId(record) {
	if (record?.action_card_message_id) return record.action_card_message_id;
	const values = Array.isArray(record?.target_message_ids) ? record.target_message_ids : [];
	return values.find(Boolean) ?? null;
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
			{ tag: 'action', actions: [action('translate', '翻译'), action('ocr', '图片 OCR'), action('transcribe', '音频转写'), action('archive', '沉淀文档')] },
		],
	};
}

export function capabilityCatalog(config) {
	const configured = (value) => Boolean(String(value ?? '').trim());
	const item = (key, label, options = {}) => {
		const resourceConfigured = options.configured ?? true;
		return {
			key, label, category: options.category ?? '协作', enabled: options.enabled !== false,
			// `configured` remains for clients predating resource_configured.
			configured: resourceConfigured, resource_configured: resourceConfigured,
			implementation_ready: true,
			authorization_subject: options.authorizationSubject ?? 'tenant',
			authorization_status: options.authorizationStatus ?? (options.requires?.length ? 'awaiting_verification' : 'not_required'),
			requires: options.requires ?? [], note: options.note ?? '',
		};
	};
	return [
		item('application_inspection', '应用权限与事件配置复核', { category: '运维', requires: ['application:application:self_manage'], note: '只读读取当前应用权限、事件和发布状态；无需向任何群发送测试消息。' }),
		item('action_cards', '汇总群行动卡片与线程回复', { category: '消息闭环', configured: config.actionCardsEnabled === true, requires: ['im:message', 'cardkit:card:write'], note: config.actionCardsEnabled === true ? '已启用：每条转发消息后追加行动卡片。' : '默认关闭以保证每条源消息只占用一个汇总群气泡；设 FEISHU_GROUP_RELAY_ACTION_CARDS_ENABLED=true 后启用。' }),
		item('reactions', '表情状态与已读', { category: '消息闭环', configured: config.actionCardsEnabled === true, requires: ['im:message.reaction:write', 'im:message:readonly'], note: config.actionCardsEnabled === true ? '可对行动卡片使用表情驱动协作状态。' : '行动卡片关闭时不监听表情协作，避免新增第二个消息气泡。' }),
		item('top_notice', '汇总消息置顶与加急', { category: '消息闭环', requires: ['im:chat:write'], note: '前端可直接对机器人发送的原始汇总消息执行置顶或加急，不依赖行动卡片。' }),
		item('source_reconciliation', '源群编辑/撤回周期对账', { category: '可靠性', authorizationSubject: 'user', requires: ['im:message.group_msg'], note: '外部源群由用户 token 轮询，无法依赖机器人事件。' }),
		item('message_search', '消息全文检索', { category: '智能处理', authorizationSubject: 'user', requires: ['search:message'], note: '使用用户身份搜索自己可访问的消息。' }),
		item('ocr', '图片 OCR', { category: '智能处理', requires: ['optical_char_recognition:image'], note: '需在开发者后台申请图片识别权限。' }),
		item('speech', '音频转写', { category: '智能处理', requires: ['speech_to_text:speech'], note: '只接收 Opus、MP3、WAV、M4A 音频；飞书文件 ASR 适用于不超过 60 秒的音频。视频仍会正常转发，但不在此接口内转写。' }),
		item('translation', '消息翻译', { category: '智能处理', requires: ['translation:text'], note: '按需调用，原始消息仍保持不变。' }),
		item('drive', '大文件分片归档到 Drive', { category: '内容沉淀', configured: configured(config.driveFolderToken), requires: ['drive:file:upload'], note: configured(config.driveFolderToken) ? '超过 IM 30 MiB 限制时会流式归档到目标文件夹。' : '需要配置 FEISHU_WORKBENCH_DRIVE_FOLDER_TOKEN。' }),
		item('baidu_pan', '大文件分片归档到百度网盘', { category: '内容沉淀', configured: config.baiduPanEnabled === true, authorizationSubject: 'user', requires: ['basic', 'netdisk'], note: config.baiduPanEnabled === true ? '可在飞书 IM 限制之外，将资源分片归档到百度网盘；需要完成一次百度 OAuth 授权。' : '设置 BAIDU_PAN_ENABLED=true 并完成百度网盘 OAuth 授权后启用。' }),
		item('docs', '富文本/Markdown 文档沉淀', { category: '内容沉淀', requires: ['创建及编辑新版文档'], note: '工作台和行动卡片可创建文档，并将 Markdown/HTML 转换为飞书块。' }),
		item('wiki', '知识库归档', { category: '内容沉淀', configured: configured(config.wikiSpaceId), requires: ['wiki:wiki'], note: configured(config.wikiSpaceId) ? '已配置知识空间，可把新建文档移入 Wiki。' : '配置 FEISHU_WORKBENCH_WIKI_SPACE_ID 后可将文档移入 Wiki。' }),
		item('tasks', '任务创建/跟踪', { category: '协作', configured: configured(config.tasklistGuid), requires: ['task:task:write'], note: configured(config.tasklistGuid) ? '已配置任务清单。' : '需配置 FEISHU_WORKBENCH_TASKLIST_GUID。' }),
		item('base', 'Base 研究台账', { category: '内容沉淀', configured: configured(config.baseAppToken && config.baseTableId), requires: ['base:record:create', 'base:record:retrieve', 'base:record:update', 'base:record:delete'], note: configured(config.baseAppToken && config.baseTableId) ? '已配置多维表格。' : '需配置 app token 与 table id。' }),
		item('calendar', '日历提醒', { category: '协作', configured: configured(config.calendarId), requires: ['calendar:calendar'], note: configured(config.calendarId) ? '已配置目标日历。' : '需配置 FEISHU_WORKBENCH_CALENDAR_ID。' }),
		item('approval', '审批流发起', { category: '协作', configured: configured(config.approvalCode), requires: ['approval:approval'], note: configured(config.approvalCode) ? '已配置审批定义。' : '需配置 FEISHU_WORKBENCH_APPROVAL_CODE。' }),
		item('feed', 'Feed 卡片/即时提醒', { category: '协作', requires: ['im:message'], note: '可由摘要任务发送到汇总群；Feed 产品权限另行开通。' }),
		item('h5', '飞书 H5 工作台与机器人菜单', { category: '入口', configured: configured(config.publicBaseUrl), requires: ['应用网页', '机器人自定义菜单'], note: configured(config.publicBaseUrl) ? '已配置外网 HTTPS 地址。' : '需要配置 FEISHU_WORKBENCH_PUBLIC_BASE_URL，并在开发者后台登记 H5 域名和菜单。' }),
		item('group_tabs', '汇总群工作台 Tab', { category: '入口', configured: configured(config.publicBaseUrl), requires: ['im:chat'], note: configured(config.publicBaseUrl) ? '可在工作台中为汇总群创建 URL Tab。' : '需先配置并登记公网 HTTPS 工作台地址。' }),
		item('open_search', '企业开放搜索索引', { category: '扩展', requires: ['search:open_search'], note: '企业套餐与管理员授权可能限制此能力。' }),
		item('aily', '智能伙伴知识问答', { category: '扩展', configured: configured(config.ailyAppId), requires: ['aily:data_knowledge'], note: configured(config.ailyAppId) ? '已配置 Aily 应用。' : '需配置 FEISHU_WORKBENCH_AILY_APP_ID。' }),
	];
}

export function createFeishuWorkbench({ appId, appSecret, larkClient, ledger, userRequest, sourceApi = null, baiduPan = null, config = {}, fetchImpl = fetch, logger = console }) {
	let cachedTenantToken = null;
	let tenantTokenExpiresAt = 0;
	let cachedOAuthOpenId = null;
	let applicationInspection = { status: 'not_checked', checked_at: null, scopes: [], app_status: null, target_chat: { status: 'not_checked', message: '尚未检查汇总群访问' }, message: '尚未执行只读应用配置复核' };
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

	async function inspectApplication() {
		const checkedAt = new Date().toISOString();
		let result = { status: 'not_checked', checked_at: checkedAt, scopes: [], app_status: null, message: '尚未执行只读应用配置复核' };
		try {
			const response = await tenantRequest('/application/v6/applications/me', { params: { lang: 'zh_cn' } });
			const app = response?.app ?? response ?? {};
			result = {
				status: 'verified', checked_at: checkedAt,
				scopes: [...applicationScopeNames(app.scopes)].sort(), app_status: app.status ?? null,
				message: '已读取飞书应用当前权限与发布状态。',
			};
		} catch (error) {
			const raw = error instanceof Error ? error.message : String(error);
			const missingSelfManage = /application:application:self_manage|admin:app\.info:readonly/.test(raw);
			result = {
				status: missingSelfManage ? 'missing_inspection_scope' : 'error', checked_at: checkedAt, scopes: [], app_status: null,
				message: missingSelfManage
					? '缺少 application:application:self_manage（或 admin:app.info:readonly），无法只读核验应用权限与事件配置。'
					: `应用配置复核失败：${asText(raw, 300)}`,
			};
		}
		let targetChat = { status: 'not_configured', message: '未配置汇总群 chat_id' };
		if (targetChatId) {
			try {
				await tenantRequest(`/im/v1/chats/${encodeURIComponent(targetChatId)}`);
				targetChat = { status: 'verified', message: '机器人应用可读取汇总群。' };
			} catch (error) {
				targetChat = { status: 'error', message: `无法读取汇总群：${asText(error instanceof Error ? error.message : String(error), 220)}` };
			}
		}
		applicationInspection = { ...result, target_chat: targetChat };
		return applicationInspection;
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

	async function sourceResource(record, descriptor, limit) {
		if (!sourceApi?.messageResourceGet) throw new Error('用户 OAuth 消息资源读取不可用');
		const resource = await sourceApi.messageResourceGet({ messageId: record.source_message_id, fileKey: descriptor.key, type: descriptor.kind });
		const headers = resource.headers ?? {};
		return { bytes: await readableToBuffer(resource.getReadableStream(), limit), contentType: String(headers['content-type'] ?? headers['Content-Type'] ?? ''), filename: String(headers['content-disposition'] ?? descriptor.key) };
	}

	async function translateRecord(record, { targetLanguage = 'zh' } = {}) {
		const text = messagePlainText(record.message);
		if (!text) throw new Error('该消息没有可翻译的文本');
		const detected = await callFeishuSdk('文本语种识别', () => larkClient.translation.v1.text.detect({ data: { text } }));
		if (detected?.code && detected.code !== 0) throw new Error(`识别消息语言失败：${detected.msg ?? detected.code}`);
		const sourceLanguage = detected?.data?.language ?? 'auto';
		if (sourceLanguage === targetLanguage) return { source_language: sourceLanguage, target_language: targetLanguage, text, translated_text: text, skipped: true };
		const translated = await callFeishuSdk('文本翻译', () => larkClient.translation.v1.text.translate({ data: { source_language: sourceLanguage, target_language: targetLanguage, text } }));
		if (translated?.code && translated.code !== 0) throw new Error(`翻译失败：${translated.msg ?? translated.code}`);
		return { source_language: sourceLanguage, target_language: targetLanguage, text, translated_text: translated?.data?.text ?? '' };
	}

	async function ocrRecord(record) {
		const image = messageResources(record.message).find((item) => item.kind === 'image');
		if (!image) throw new Error('该消息没有可 OCR 的图片');
		const resource = await sourceResource(record, image, MAX_OCR_BYTES);
		const result = await callFeishuSdk('图片 OCR', () => larkClient.optical_char_recognition.v1.image.basicRecognize({ data: { image: resource.bytes.toString('base64') } }));
		if (result?.code && result.code !== 0) throw new Error(`图片 OCR 失败：${result.msg ?? result.code}`);
		return { text_list: result?.data?.text_list ?? [], text: (result?.data?.text_list ?? []).join('\n') };
	}

	async function transcribeRecord(record) {
		const media = messageResources(record.message).find((item) => item.kind === 'file');
		if (!media) throw new Error('该消息没有可转写的音频文件');
		const resource = await sourceResource(record, media, MAX_ASR_BYTES);
		const format = mediaFormat(resource.filename, resource.contentType);
		if (!format) throw new Error('当前飞书文件 ASR 仅支持 Opus、MP3、WAV、M4A 音频；视频已正常转发，如需转写请先提取不超过 60 秒的音轨');
		const result = await callFeishuSdk('音频转写', () => larkClient.speech_to_text.v1.speech.fileRecognize({ data: {
			speech: { speech: resource.bytes.toString('base64') },
			config: { file_id: `${record.source_message_id}:${media.key}`.slice(0, 128), format, engine_type: String(config.asrEngineType ?? '16k_auto') },
		} }));
		if (result?.code && result.code !== 0) throw new Error(`音频转写失败：${result.msg ?? result.code}`);
		return { text: result?.data?.recognition_text ?? '' };
	}

	async function createTask(record, operatorOpenId) {
		if (!config.tasklistGuid) throw new Error('尚未配置 FEISHU_WORKBENCH_TASKLIST_GUID');
		if (record.workflow_state === 'task') throw new Error('该消息已经创建过飞书任务');
		const summary = `#${record.route_tag} ${sourceExcerpt(record.message).slice(0, 80) || '群消息跟进'}`;
		const description = `来源群：${record.source_chat_name ?? record.route_tag}\n源消息 ID：${record.source_message_id}\n\n${sourceExcerpt(record.message)}`;
		const body = { summary, description, tasklists: [{ guid: config.tasklistGuid }], ...(operatorOpenId ? { members: [{ id: operatorOpenId, type: 'user', role: 'assignee' }] } : {}) };
		return tenantRequest('/task/v2/tasks', { method: 'POST', params: operatorOpenId ? { user_id_type: 'open_id' } : {}, body });
	}

	async function currentOAuthOpenId() {
		if (cachedOAuthOpenId) return cachedOAuthOpenId;
		if (!userRequest) throw new Error('未提供操作人 open_id，且用户 OAuth 不可用');
		const result = await userRequest('/authen/v1/user_info');
		const openId = String(result?.data?.open_id ?? result?.open_id ?? '').trim();
		if (!openId) throw new Error('用户 OAuth 未返回 open_id，请重新授权 auth:user.id:read');
		cachedOAuthOpenId = openId;
		return openId;
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
		if (action === 'task') { external = await createTask(record, operatorOpenId || await currentOAuthOpenId()); workflowState = 'task'; note = '已创建并指派飞书任务'; }
		if (action === 'pin') { const messageId = primaryTargetMessageId(record); if (!messageId) throw new Error('汇总消息尚未创建'); external = await tenantRequest('/im/v1/pins', { method: 'POST', body: { message_id: messageId } }); note = '已置顶汇总消息'; }
		if (action === 'urgent') { const messageId = primaryTargetMessageId(record); if (!messageId) throw new Error('汇总消息尚未创建'); const targetOpenId = operatorOpenId || await currentOAuthOpenId(); external = await tenantRequest(`/im/v1/messages/${encodeURIComponent(messageId)}/urgent_app`, { method: 'PATCH', params: { user_id_type: 'open_id' }, body: { user_id_list: [targetOpenId] } }); note = '已发送应用内加急提醒'; }
		if (action === 'recall') { const messageId = primaryTargetMessageId(record); if (!messageId) throw new Error('汇总消息尚未创建'); external = await tenantRequest(`/im/v1/messages/${encodeURIComponent(messageId)}`, { method: 'DELETE' }); workflowState = 'recalled'; note = '已撤回汇总群消息'; }
		if (action === 'digest') { external = await replyToAction(record, `#${record.route_tag} 已加入本轮摘要。`); note = '已加入摘要'; }
		if (action === 'archive') { external = await archiveRecord(record); note = external.wiki?.wiki_token ? '已沉淀到飞书 Wiki' : '已沉淀为飞书文档'; }
		if (action === 'translate') { external = await translateRecord(record); note = '已生成消息翻译'; }
		if (action === 'ocr') { external = await ocrRecord(record); note = '已完成图片 OCR'; }
		if (action === 'transcribe') { external = await transcribeRecord(record); note = '已完成音频转写'; }
		const updated = await ledger.updateRelayWorkflow(sourceMessageId, { workflowState, workflowNote: note, actorOpenId: operatorOpenId || null, action });
		if (['translate', 'ocr', 'transcribe', 'archive'].includes(action)) await ledger.recordRelayIntelligence(sourceMessageId, action, external);
		if (action !== 'recall') await updateActionCard(updated).catch((error) => logger.warn(`更新行动卡片失败：${error.message}`));
		if (['research', 'focus', 'task', 'ignore'].includes(action)) await replyToAction(updated, `#${updated.route_tag} ${note}`).catch((error) => logger.warn(`发送行动线程回复失败：${error.message}`));
		if (action === 'translate') await replyToAction(updated, `#${updated.route_tag} 翻译（${external.source_language} → ${external.target_language}）\n${asText(external.translated_text, 3_000)}`).catch((error) => logger.warn(`发送翻译线程回复失败：${error.message}`));
		if (action === 'ocr') await replyToAction(updated, `#${updated.route_tag} 图片 OCR\n${asText(external.text, 3_000) || '未识别到文字'}`).catch((error) => logger.warn(`发送 OCR 线程回复失败：${error.message}`));
		if (action === 'transcribe') await replyToAction(updated, `#${updated.route_tag} 音频转写\n${asText(external.text, 3_000) || '未识别到语音内容'}`).catch((error) => logger.warn(`发送转写线程回复失败：${error.message}`));
		if (action === 'archive') await replyToAction(updated, `#${updated.route_tag} 已沉淀为飞书文档${external.wiki?.wiki_token ? '并移入 Wiki' : ''}（${asText(external.document_id, 180)}）`).catch((error) => logger.warn(`发送文档沉淀线程回复失败：${error.message}`));
		return { record: updated, external };
	}

	async function createDocument({ title, content = '', contentType = 'markdown' }) {
		const created = await larkClient.docx.v1.document.create({ data: { title: asText(title, 250), ...(config.driveFolderToken ? { folder_token: config.driveFolderToken } : {}) } });
		if (created?.code && created.code !== 0) throw new Error(`创建飞书文档失败：${created.msg ?? created.code}`);
		const document = created?.data?.document;
		const documentId = document?.document_id;
		if (!documentId) throw new Error('创建飞书文档未返回 document_id');
		const formattedContent = documentContent(content);
		if (formattedContent) {
			if (!['markdown', 'html'].includes(contentType)) throw new Error('文档内容类型仅支持 markdown 或 html');
			const converted = await larkClient.docx.v1.document.convert({ data: { content_type: contentType, content: formattedContent } });
			if (converted?.code && converted.code !== 0) throw new Error(`文档内容转换失败：${converted.msg ?? converted.code}`);
			const blocks = dropReadonlyDocumentFields(converted?.data?.blocks ?? []);
			const childrenId = converted?.data?.first_level_block_ids ?? [];
			if (blocks.length && childrenId.length) {
				const inserted = await larkClient.docx.v1.documentBlockDescendant.create({ path: { document_id: documentId, block_id: documentId }, data: { children_id: childrenId, descendants: blocks } });
				if (inserted?.code && inserted.code !== 0) throw new Error(`写入飞书文档失败：${inserted.msg ?? inserted.code}`);
			}
		}
		return { document, document_id: documentId, content_written: Boolean(formattedContent) };
	}

	async function moveDocumentToWiki({ documentId, title, parentNodeToken = '' }) {
		if (!config.wikiSpaceId) throw new Error('尚未配置 FEISHU_WORKBENCH_WIKI_SPACE_ID');
		if (!documentId) throw new Error('缺少要归档的飞书文档 ID');
		const result = await larkClient.wiki.v2.spaceNode.moveDocsToWiki({
			path: { space_id: config.wikiSpaceId },
			data: {
				obj_type: 'docx', obj_token: documentId, apply: true,
				...(asText(parentNodeToken || config.wikiParentNodeToken, 180) ? { parent_wiki_token: asText(parentNodeToken || config.wikiParentNodeToken, 180) } : {}),
			},
		});
		if (result?.code && result.code !== 0) throw new Error(`移入飞书 Wiki 失败：${result.msg ?? result.code}`);
		return result?.data ?? {};
	}

	async function createWikiDocument({ title, content = '', contentType = 'markdown', parentNodeToken = '' }) {
		const created = await createDocument({ title, content, contentType });
		const wiki = await moveDocumentToWiki({ documentId: created.document_id, title, parentNodeToken });
		return { ...created, wiki };
	}

	async function archiveRecord(record) {
		const title = `#${asText(record.route_tag, 80)} ${sourceExcerpt(record.message).slice(0, 100) || '群消息'}`;
		const payload = { title, content: recordArchiveMarkdown(record), contentType: 'markdown' };
		return config.wikiSpaceId ? createWikiDocument(payload) : createDocument(payload);
	}

	async function createBaseRecord({ fields }) {
		if (!config.baseAppToken || !config.baseTableId) throw new Error('尚未配置 Base app token 与 table id');
		return tenantRequest(`/bitable/v1/apps/${encodeURIComponent(config.baseAppToken)}/tables/${encodeURIComponent(config.baseTableId)}/records`, { method: 'POST', body: { fields: fields ?? {} } });
	}

	async function listBaseRecords({ pageSize = 20, pageToken = '' } = {}) {
		if (!config.baseAppToken || !config.baseTableId) throw new Error('尚未配置 Base app token 与 table id');
		return tenantRequest(`/bitable/v1/apps/${encodeURIComponent(config.baseAppToken)}/tables/${encodeURIComponent(config.baseTableId)}/records`, { params: { page_size: Math.min(100, Math.max(1, Number(pageSize) || 20)), ...(pageToken ? { page_token: pageToken } : {}) } });
	}

	async function updateBaseRecord({ recordId, fields }) {
		if (!config.baseAppToken || !config.baseTableId) throw new Error('尚未配置 Base app token 与 table id');
		if (!asText(recordId, 180)) throw new Error('缺少 Base record_id');
		return tenantRequest(`/bitable/v1/apps/${encodeURIComponent(config.baseAppToken)}/tables/${encodeURIComponent(config.baseTableId)}/records/${encodeURIComponent(asText(recordId, 180))}`, { method: 'PUT', body: { fields: fields ?? {} } });
	}

	async function deleteBaseRecord({ recordId }) {
		if (!config.baseAppToken || !config.baseTableId) throw new Error('尚未配置 Base app token 与 table id');
		if (!asText(recordId, 180)) throw new Error('缺少 Base record_id');
		return tenantRequest(`/bitable/v1/apps/${encodeURIComponent(config.baseAppToken)}/tables/${encodeURIComponent(config.baseTableId)}/records/${encodeURIComponent(asText(recordId, 180))}`, { method: 'DELETE' });
	}

	async function createCalendarEvent({ summary, description = '', startTime, endTime }) {
		if (!config.calendarId) throw new Error('尚未配置 FEISHU_WORKBENCH_CALENDAR_ID');
		return tenantRequest(`/calendar/v4/calendars/${encodeURIComponent(config.calendarId)}/events`, { method: 'POST', body: { summary: asText(summary, 250), description: asText(description, 3000), start_time: { timestamp: String(Math.floor(new Date(startTime).getTime() / 1000)) }, end_time: { timestamp: String(Math.floor(new Date(endTime).getTime() / 1000)) } } });
	}

	async function createApproval({ form }) {
		if (!config.approvalCode) throw new Error('尚未配置 FEISHU_WORKBENCH_APPROVAL_CODE');
		return tenantRequest('/approval/v4/instances', { method: 'POST', body: { approval_code: config.approvalCode, form: JSON.stringify(form ?? {}) } });
	}

	async function publishDigest({ limit = 12 } = {}) {
		if (!ledger?.recentRelayMessages) throw new Error('本地消息台账不可用');
		const records = (await ledger.recentRelayMessages(Math.min(50, Math.max(1, Number(limit) || 12))))
			.filter((record) => !record.source_deleted && record.workflow_state !== 'ignored' && record.workflow_state !== 'recalled');
		if (!records.length) throw new Error('没有可摘要的近期汇总消息');
		const body = records.map((record, index) => `${index + 1}. #${record.route_tag} ${sourceExcerpt(record.message).replace(/\s+/g, ' ').slice(0, 220)}`).join('\n');
		const messageId = await sendMessage({ msgType: 'text', content: { text: `#digest 最近 ${records.length} 条群消息\n${body}` } });
		return { message_id: messageId, count: records.length, source_message_ids: records.map((record) => record.source_message_id) };
	}

	async function ensureWorkbenchTab({ title = '分析师工作台' } = {}) {
		if (!targetChatId) throw new Error('未配置汇总群 chat_id');
		const url = config.publicBaseUrl ? `${String(config.publicBaseUrl).replace(/\/$/, '')}/workbench` : '';
		if (!url) throw new Error('尚未配置 FEISHU_WORKBENCH_PUBLIC_BASE_URL');
		const listed = await larkClient.im.v1.chatTab.listTabs({ path: { chat_id: targetChatId } });
		if (listed?.code && listed.code !== 0) throw new Error(`读取汇总群 Tab 失败：${listed.msg ?? listed.code}`);
		const existing = (listed?.data?.chat_tabs ?? []).find((tab) => tab.tab_type === 'url' && tab.tab_content?.url === url);
		if (existing) return { created: false, chat_tab: existing, url };
		const created = await larkClient.im.v1.chatTab.create({ path: { chat_id: targetChatId }, data: { chat_tabs: [{ tab_name: asText(title, 120) || '分析师工作台', tab_type: 'url', tab_content: { url } }] } });
		if (created?.code && created.code !== 0) throw new Error(`创建汇总群工作台 Tab 失败：${created.msg ?? created.code}`);
		return { created: true, chat_tab: created?.data?.chat_tabs?.[0] ?? null, url };
	}

	async function uploadToDrive({ readable, fileName, size }) {
		if (!config.driveFolderToken) throw new Error('尚未配置 FEISHU_WORKBENCH_DRIVE_FOLDER_TOKEN，无法归档大文件');
		const bytes = Number(size);
		const maxBytes = Math.max(30 * 1024 * 1024, Number(config.driveMaxFileBytes ?? 524_288_000));
		if (!Number.isFinite(bytes) || bytes <= 0) throw new Error('源文件未提供有效大小，无法启动飞书云空间分片上传');
		if (bytes > maxBytes) throw new Error(`源文件超过 ${Math.floor(maxBytes / 1024 / 1024)} MiB 云空间归档上限`);
		const prepared = await larkClient.drive.v1.file.uploadPrepare({ data: { file_name: asText(fileName, 250) || 'relay-file', parent_type: 'explorer', parent_node: config.driveFolderToken, size: bytes } });
		if (prepared?.code && prepared.code !== 0) throw new Error(`云空间预上传失败：${prepared.msg ?? prepared.code}`);
		const uploadId = prepared?.data?.upload_id;
		const blockSize = Number(prepared?.data?.block_size ?? 4 * 1024 * 1024);
		const blockNum = Number(prepared?.data?.block_num ?? Math.ceil(bytes / blockSize));
		if (!uploadId || !Number.isFinite(blockSize) || blockSize <= 0 || !Number.isFinite(blockNum) || blockNum <= 0) throw new Error('云空间未返回有效分片上传策略');
		let pending = Buffer.alloc(0); let sequence = 0;
		for await (const chunk of readable) {
			pending = pending.length ? Buffer.concat([pending, Buffer.from(chunk)]) : Buffer.from(chunk);
			while (pending.length >= blockSize) {
				const block = pending.subarray(0, blockSize); pending = pending.subarray(blockSize);
				const uploaded = await larkClient.drive.v1.file.uploadPart({ data: { upload_id: uploadId, seq: sequence++, size: block.length, checksum: createHash('sha256').update(block).digest('hex'), file: block } });
				if (uploaded?.code && uploaded.code !== 0) throw new Error(`云空间上传分片失败：${uploaded.msg ?? uploaded.code}`);
			}
		}
		if (pending.length) {
			const uploaded = await larkClient.drive.v1.file.uploadPart({ data: { upload_id: uploadId, seq: sequence++, size: pending.length, checksum: createHash('sha256').update(pending).digest('hex'), file: pending } });
			if (uploaded?.code && uploaded.code !== 0) throw new Error(`云空间上传分片失败：${uploaded.msg ?? uploaded.code}`);
		}
		if (sequence !== blockNum) throw new Error(`云空间分片数量不匹配：预期 ${blockNum}，实际 ${sequence}`);
		const finished = await larkClient.drive.v1.file.uploadFinish({ data: { upload_id: uploadId, block_num: blockNum } });
		if (finished?.code && finished.code !== 0) throw new Error(`云空间完成上传失败：${finished.msg ?? finished.code}`);
		if (!finished?.data?.file_token) throw new Error('云空间完成上传未返回 file_token');
		return { kind: 'drive', fileToken: finished.data.file_token, filename: asText(fileName, 250) || 'relay-file' };
	}

	async function uploadToBaiduPan({ readable, fileName, size, remotePath }) {
		if (!baiduPan) throw new Error('百度网盘适配器未配置');
		return baiduPan.uploadReadable({ readable, fileName, size, remotePath });
	}

	async function uploadToCloud({ readable, fileName, size, remotePath }) {
		const provider = String(config.archiveProvider ?? 'auto').trim().toLowerCase();
		if (provider === 'baidu' || (provider === 'auto' && !config.driveFolderToken && config.baiduPanEnabled === true)) return uploadToBaiduPan({ readable, fileName, size, remotePath });
		if (provider === 'feishu' || provider === 'auto') return uploadToDrive({ readable, fileName, size });
		throw new Error(`不支持的云归档 provider：${provider}`);
	}

	async function searchMessages({ query, pageToken }) {
		if (!userRequest) throw new Error('用户 OAuth 不可用');
		const text = asText(query, 200);
		if (!text) throw new Error('请输入检索关键词');
		return userRequest('/search/v2/message', { method: 'POST', body: { query: text, page_size: 20, ...(pageToken ? { page_token: pageToken } : {}) } });
	}

	async function syncSourceChange(record, { deleted = false, originalSynced = false } = {}) {
		if (!record) return null;
		if (deleted) {
			const targetIds = [...new Set([...(Array.isArray(record.target_message_ids) ? record.target_message_ids : []), record.action_card_message_id].filter(Boolean))];
			if (!targetIds.length) return record;
			for (const messageId of targetIds) {
				await tenantRequest(`/im/v1/messages/${encodeURIComponent(messageId)}`, { method: 'DELETE' }).catch((error) => logger.warn(`撤回汇总群同步消息失败：${error.message}`));
			}
			return ledger.updateRelayWorkflow(record.source_message_id, { workflowState: 'recalled', workflowNote: '源群消息已撤回，汇总群副本已同步撤回', actorOpenId: null, action: 'source_recalled' });
		}
		const targetMessageId = Array.isArray(record.target_message_ids) ? record.target_message_ids[0] : null;
		if (!originalSynced && record.message?.msg_type === 'text' && targetMessageId) {
			await tenantRequest(`/im/v1/messages/${encodeURIComponent(targetMessageId)}`, { method: 'PUT', body: { msg_type: 'text', content: JSON.stringify({ text: taggedText(record.route_tag, messagePlainText(record.message)) }) } }).catch((error) => logger.warn(`同步源文本编辑失败：${error.message}`));
		}
		await updateActionCard(record).catch((error) => logger.warn(`同步源消息编辑到行动卡片失败：${error.message}`));
		await replyToAction(record, `#${record.route_tag} 源群消息已编辑；行动卡片中的原始消息已更新。`).catch((error) => logger.warn(`通知源消息编辑失败：${error.message}`));
		return record;
	}

	return {
		status: async () => ({ target_configured: Boolean(targetChatId), public_h5_url: config.publicBaseUrl ? `${String(config.publicBaseUrl).replace(/\/$/, '')}/workbench` : null, capabilities: capabilityCatalog(config), application_inspection: applicationInspection }),
		publishActionCard, performAction, createDocument, createWikiDocument, createBaseRecord, listBaseRecords, updateBaseRecord, deleteBaseRecord, createCalendarEvent, createApproval, publishDigest, ensureWorkbenchTab, searchMessages, syncSourceChange, uploadToDrive, uploadToBaiduPan, uploadToCloud, inspectApplication,
		buildActionCard: (record) => buildActionCard({ sourceMessageId: record.source_message_id, routeTag: record.route_tag, sourceName: record.source_chat_name, message: record.message, workflowState: record.workflow_state, note: record.workflow_note }),
	};
}
