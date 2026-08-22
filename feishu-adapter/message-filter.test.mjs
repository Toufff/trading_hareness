import assert from 'node:assert/strict';
import test from 'node:test';
import { isSystemMessage, isSystemRelayPlaceholder } from './message-filter.mjs';

test('filters Feishu system notices and their relayed #tag placeholder', () => {
	assert.equal(isSystemMessage({ msg_type: 'system' }), true);
	assert.equal(isSystemMessage({ msg_type: 'text' }), false);
	assert.equal(isSystemRelayPlaceholder('#quanneng\n[system]　此消息类型无法跨租户保持原组件。', 'quanneng'), true);
	assert.equal(isSystemRelayPlaceholder('#quanneng\n正常分析内容', 'quanneng'), false);
});
