import assert from 'node:assert/strict';
import test from 'node:test';
import { buildActionCard, capabilityCatalog } from './feishu-workbench.mjs';

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
});
