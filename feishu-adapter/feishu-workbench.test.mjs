import assert from 'node:assert/strict';
import test from 'node:test';
import { Readable } from 'node:stream';
import { buildActionCard, capabilityCatalog, createFeishuWorkbench } from './feishu-workbench.mjs';

test('action card keeps route tag, source ID and all core workflow buttons', () => {
	const card = buildActionCard({
		sourceMessageId: 'om_source_1', routeTag: 'anqiang', sourceName: '马安强 (1)',
		message: { msg_type: 'text', create_time: '1780000000000', body: { content: JSON.stringify({ text: '测试消息' }) } },
	});
	assert.equal(card.header.title.content, '#anqiang · 待处理');
	const actionElement = card.elements.find((element) => element.tag === 'action');
	assert.deepEqual(actionElement.actions.map((action) => action.value.action), ['research', 'focus', 'task', 'ignore']);
	assert.equal(actionElement.actions[0].value.source_message_id, 'om_source_1');
});

test('configured capability catalog distinguishes optional product identifiers', () => {
	const capabilities = capabilityCatalog({ driveFolderToken: 'fldcn', tasklistGuid: '', baseAppToken: 'appcn', baseTableId: 'tblcn' });
	assert.equal(capabilities.find((item) => item.key === 'drive')?.configured, true);
	assert.equal(capabilities.find((item) => item.key === 'tasks')?.configured, false);
	assert.equal(capabilities.find((item) => item.key === 'base')?.configured, true);
	assert.deepEqual(capabilities.find((item) => item.key === 'ocr')?.requires, ['optical_char_recognition:image']);
	assert.deepEqual(capabilities.find((item) => item.key === 'speech')?.requires, ['speech_to_text:speech']);
	assert.deepEqual(capabilities.find((item) => item.key === 'translation')?.requires, ['translation:text']);
	assert.equal(capabilities.find((item) => item.key === 'message_search')?.authorization_subject, 'user');
	assert.equal(capabilities.find((item) => item.key === 'ocr')?.authorization_status, 'awaiting_verification');
	assert.deepEqual(capabilities.find((item) => item.key === 'application_inspection')?.requires, ['application:application:self_manage']);
});

test('application inspection is read-only and exposes published app scope names', async () => {
	const requestUrls = [];
	const fetchImpl = async (url) => {
		requestUrls.push(url);
		if (url.includes('/auth/v3/tenant_access_token/internal')) return new Response(JSON.stringify({ code: 0, tenant_access_token: 'tenant', expire: 7200 }));
		return new Response(JSON.stringify({ code: 0, data: { app: { status: 1, scopes: [{ scope: 'translation:text' }, { scope_name: 'speech_to_text:speech' }] } } }));
	};
	const workbench = createFeishuWorkbench({ appId: 'app', appSecret: 'secret', larkClient: {}, ledger: {}, config: { targetChatId: 'oc_target' }, fetchImpl });
	const inspection = await workbench.inspectApplication();
	assert.ok(requestUrls.some((url) => /application\/v6\/applications\/me\?lang=zh_cn/.test(url)));
	assert.ok(requestUrls.some((url) => /im\/v1\/chats\/oc_target/.test(url)));
	assert.equal(inspection.status, 'verified');
	assert.deepEqual(inspection.scopes, ['speech_to_text:speech', 'translation:text']);
	assert.equal(inspection.target_chat.status, 'verified');
	assert.equal((await workbench.status()).application_inspection.status, 'verified');
});

test('Drive fallback streams known-size source data in provider block sizes', async () => {
	const parts = []; let finished;
	const larkClient = { drive: { v1: { file: {
		uploadPrepare: async () => ({ code: 0, data: { upload_id: 'up_1', block_size: 4, block_num: 3 } }),
		uploadPart: async ({ data }) => { parts.push(data); return {}; },
		uploadFinish: async ({ data }) => { finished = data; return { code: 0, data: { file_token: 'boxcn_1' } }; },
	} } } };
	const workbench = createFeishuWorkbench({ appId: 'app', appSecret: 'secret', larkClient, ledger: {}, config: { driveFolderToken: 'fldcn', driveMaxFileBytes: 1024 } });
	const result = await workbench.uploadToDrive({ readable: Readable.from([Buffer.from('abc'), Buffer.from('defghij')]), fileName: 'sample.bin', size: 10 });
	assert.equal(result.fileToken, 'boxcn_1');
	assert.deepEqual(parts.map((part) => [part.seq, part.size, part.file.toString()]), [[0, 4, 'abcd'], [1, 4, 'efgh'], [2, 2, 'ij']]);
	assert.deepEqual(finished, { upload_id: 'up_1', block_num: 3 });
});

test('intelligence actions call Feishu translation, OCR and ASR then persist results', async () => {
	const record = {
		source_message_id: 'om_1', route_tag: 'anqiang', source_chat_name: '马安强', workflow_state: 'new', action_card_message_id: null,
		message: { msg_type: 'post', body: { content: JSON.stringify({ content: [[{ tag: 'text', text: 'hello' }, { tag: 'img', image_key: 'img_1' }], [{ tag: 'media', file_key: 'file_1' }]] }) } },
	};
	const saved = {};
	const ledger = {
		getRelayMessage: async () => record,
		updateRelayWorkflow: async (_id, patch) => ({ ...record, workflow_note: patch.workflowNote }),
		recordRelayIntelligence: async (_id, kind, value) => { saved[kind] = value; return record; },
	};
	const sourceApi = { messageResourceGet: async ({ fileKey }) => ({ headers: { 'content-type': fileKey === 'img_1' ? 'image/png' : 'audio/mpeg' }, getReadableStream: () => Readable.from([Buffer.from(fileKey === 'img_1' ? 'image' : 'audio')]) }) };
	const larkClient = {
		translation: { v1: { text: { detect: async () => ({ data: { language: 'en' } }), translate: async () => ({ data: { text: '你好' } }) } } },
		optical_char_recognition: { v1: { image: { basicRecognize: async () => ({ data: { text_list: ['图片文字'] } }) } } },
		speech_to_text: { v1: { speech: { fileRecognize: async () => ({ data: { recognition_text: '语音内容' } }) } } },
		im: { v1: { message: { create: async () => ({ data: { message_id: 'om_target' } }) } } },
	};
	const workbench = createFeishuWorkbench({ appId: 'app', appSecret: 'secret', larkClient, ledger, sourceApi });
	assert.equal((await workbench.performAction({ sourceMessageId: 'om_1', action: 'translate' })).external.translated_text, '你好');
	assert.equal((await workbench.performAction({ sourceMessageId: 'om_1', action: 'ocr' })).external.text, '图片文字');
	assert.equal((await workbench.performAction({ sourceMessageId: 'om_1', action: 'transcribe' })).external.text, '语音内容');
	assert.deepEqual(Object.keys(saved).sort(), ['ocr', 'transcribe', 'translate']);
});

test('audio transcription rejects video and unsupported formats before invoking Feishu ASR', async () => {
	const record = {
		source_message_id: 'om_video', route_tag: 'quanneng', workflow_state: 'new',
		message: { msg_type: 'post', body: { content: JSON.stringify({ content: [[{ tag: 'media', file_key: 'video_1' }]] }) } },
	};
	let asrCalls = 0;
	const workbench = createFeishuWorkbench({
		appId: 'app', appSecret: 'secret', ledger: { getRelayMessage: async () => record },
		sourceApi: { messageResourceGet: async () => ({ headers: { 'content-type': 'video/mp4', 'content-disposition': 'attachment; filename="clip.mp4"' }, getReadableStream: () => Readable.from([Buffer.from('video')]) }) },
		larkClient: { speech_to_text: { v1: { speech: { fileRecognize: async () => { asrCalls += 1; return {}; } } } } },
	});
	await assert.rejects(() => workbench.performAction({ sourceMessageId: 'om_video', action: 'transcribe' }), /仅支持 Opus、MP3、WAV、M4A 音频/);
	assert.equal(asrCalls, 0);
});

test('intelligence actions expose Feishu frequency-control retry guidance', async () => {
	const record = { source_message_id: 'om_rate_limited', route_tag: 'anqiang', workflow_state: 'new', message: { msg_type: 'text', body: { content: JSON.stringify({ text: 'hello' }) } } };
	const rateLimited = Object.assign(new Error('Request failed with status code 400'), {
		response: { data: { code: 99991400, msg: 'request trigger frequency limit' }, headers: { 'x-ogw-ratelimit-reset': '2' } },
	});
	const workbench = createFeishuWorkbench({
		appId: 'app', appSecret: 'secret', ledger: { getRelayMessage: async () => record },
		larkClient: { translation: { v1: { text: { detect: async () => { throw rateLimited; } } } } },
	});
	await assert.rejects(() => workbench.performAction({ sourceMessageId: 'om_rate_limited', action: 'translate' }), /飞书文本语种识别触发频控（99991400），请在约 2 秒后重试/);
});

test('document creation converts Markdown and writes converted descendants', async () => {
	let inserted;
	const larkClient = { docx: { v1: {
		document: { create: async () => ({ data: { document: { document_id: 'doccn_1', title: 'note' } } }), convert: async ({ data }) => ({ data: { first_level_block_ids: ['b1'], blocks: [{ block_id: 'b1', block_type: 2, table: { merge_info: { readonly: true } } }, { block_id: 'child', block_type: 2 }] } }) },
		documentBlockDescendant: { create: async (payload) => { inserted = payload; return { code: 0, data: {} }; } },
	} } };
	const workbench = createFeishuWorkbench({ appId: 'app', appSecret: 'secret', larkClient, ledger: {}, config: { driveFolderToken: 'fldcn' } });
	const result = await workbench.createDocument({ title: 'note', content: '# heading\nbody' });
	assert.equal(result.document_id, 'doccn_1');
	assert.equal(result.content_written, true);
	assert.deepEqual(inserted.path, { document_id: 'doccn_1', block_id: 'doccn_1' });
	assert.equal('merge_info' in inserted.data.descendants[0].table, false);
});

test('Wiki archive moves a created document and workbench tab is idempotent', async () => {
	let movePayload; let tabCreatePayload;
	const larkClient = {
		docx: { v1: {
			document: { create: async () => ({ data: { document: { document_id: 'doccn_wiki' } } }), convert: async () => ({ data: { first_level_block_ids: [], blocks: [] } }) },
			documentBlockDescendant: { create: async () => ({ code: 0 }) },
		} },
		wiki: { v2: { spaceNode: { moveDocsToWiki: async (payload) => { movePayload = payload; return { data: { wiki_token: 'wikcn_1' } }; } } } },
		im: { v1: { chatTab: {
			listTabs: async () => ({ data: { chat_tabs: [] } }),
			create: async (payload) => { tabCreatePayload = payload; return { data: { chat_tabs: [{ tab_id: 'tab_1', tab_type: 'url' }] } }; },
		} } },
	};
	const workbench = createFeishuWorkbench({ appId: 'app', appSecret: 'secret', larkClient, ledger: {}, config: { targetChatId: 'oc_target', wikiSpaceId: 'spc_1', wikiParentNodeToken: 'wikcn_parent', publicBaseUrl: 'https://workbench.example.test/' } });
	const wikiResult = await workbench.createWikiDocument({ title: 'note', content: 'hello' });
	assert.equal(wikiResult.document_id, 'doccn_wiki');
	assert.deepEqual(movePayload, { path: { space_id: 'spc_1' }, data: { obj_type: 'docx', obj_token: 'doccn_wiki', apply: true, parent_wiki_token: 'wikcn_parent' } });
	const tabResult = await workbench.ensureWorkbenchTab();
	assert.equal(tabResult.created, true);
	assert.deepEqual(tabCreatePayload, { path: { chat_id: 'oc_target' }, data: { chat_tabs: [{ tab_name: '分析师工作台', tab_type: 'url', tab_content: { url: 'https://workbench.example.test/workbench' } }] } });
});

test('pin action targets the original relay message when action cards are disabled', async () => {
	let pinBody;
	const record = { source_message_id: 'om_source', route_tag: 'anqiang', message: { msg_type: 'text', body: { content: JSON.stringify({ text: '原文' }) } }, target_message_ids: ['om_original'], workflow_state: 'new', action_card_message_id: null };
	const ledger = { getRelayMessage: async () => record, updateRelayWorkflow: async (_id, patch) => ({ ...record, workflow_note: patch.workflowNote }) };
	const fetchImpl = async (url, options = {}) => {
		if (url.includes('/auth/v3/tenant_access_token/internal')) return new Response(JSON.stringify({ code: 0, tenant_access_token: 'tenant', expire: 7200 }), { status: 200 });
		if (url.endsWith('/im/v1/pins')) { pinBody = JSON.parse(options.body); return new Response(JSON.stringify({ code: 0, data: {} }), { status: 200 }); }
		throw new Error(`unexpected URL: ${url}`);
	};
	const workbench = createFeishuWorkbench({ appId: 'app', appSecret: 'secret', larkClient: {}, ledger, fetchImpl });
	await workbench.performAction({ sourceMessageId: 'om_source', action: 'pin' });
	assert.deepEqual(pinBody, { message_id: 'om_original' });
});

test('urgent action resolves the OAuth user and declares open_id addressing', async () => {
	let urgentUrl; let urgentBody;
	const record = { source_message_id: 'om_source', route_tag: 'anqiang', message: { msg_type: 'text', body: { content: JSON.stringify({ text: '原文' }) } }, target_message_ids: ['om_original'], workflow_state: 'new', action_card_message_id: null };
	const ledger = { getRelayMessage: async () => record, updateRelayWorkflow: async (_id, patch) => ({ ...record, workflow_note: patch.workflowNote }) };
	const fetchImpl = async (url, options = {}) => {
		if (url.includes('/auth/v3/tenant_access_token/internal')) return new Response(JSON.stringify({ code: 0, tenant_access_token: 'tenant', expire: 7200 }), { status: 200 });
		if (url.includes('/urgent_app')) { urgentUrl = url; urgentBody = JSON.parse(options.body); return new Response(JSON.stringify({ code: 0, data: {} }), { status: 200 }); }
		throw new Error(`unexpected URL: ${url}`);
	};
	const workbench = createFeishuWorkbench({ appId: 'app', appSecret: 'secret', larkClient: {}, ledger, userRequest: async () => ({ data: { open_id: 'ou_current' } }), fetchImpl });
	await workbench.performAction({ sourceMessageId: 'om_source', action: 'urgent' });
	assert.match(urgentUrl, /user_id_type=open_id/);
	assert.deepEqual(urgentBody, { user_id_list: ['ou_current'] });
});
