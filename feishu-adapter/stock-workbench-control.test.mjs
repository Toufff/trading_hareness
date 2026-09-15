import test from 'node:test';
import assert from 'node:assert/strict';
import { createStockWorkbenchControl } from './stock-workbench-control.mjs';

test('presentation control is revisioned, bounded and expires to idle', () => {
	let clock = Date.parse('2026-09-05T02:00:00Z');
	const control = createStockWorkbenchControl({ now: () => clock, idFactory: () => 'generated-id', defaultTtlSeconds: 60 });
	const presented = control.apply({
		operation: 'present', symbol: '600487.sh', strategy_key: 'event', timeframe: 'weekly', metric: 'volume',
		zoom: { start: 25, end: 90 }, panel_visibility: { messages: false },
		focus: { scope: 'next_session', state: '强势路径' },
		speaker_note: { title: '演示观点', body: '只解释已经加载的证据', source_label: '对话', as_of: '2026-09-05' },
	});
	assert.equal(presented.revision, 1);
	assert.equal(presented.symbol, '600487.SH');
	assert.equal(presented.panel_visibility.messages, false);
	assert.equal(presented.presentation_only, true);
	clock += 60_001;
	const expired = control.snapshot();
	assert.equal(expired.active, false);
	assert.equal(expired.annotations.length, 0);
	assert.equal(expired.revision, 2);
});

test('annotations can be added, replaced, removed and cleared', () => {
	const control = createStockWorkbenchControl({ idFactory: () => 'generated-id' });
	control.apply({ operation: 'annotate', annotation: { kind: 'price_line', label: '回踩观察', price: 50.2 } });
	assert.equal(control.snapshot().annotations[0].price, 50.2);
	control.apply({ operation: 'annotate', annotation: { id: 'generated-id', kind: 'point', label: '事件', date: '2026-09-04', price: 52 } });
	assert.equal(control.snapshot().annotations.length, 1);
	assert.equal(control.snapshot().annotations[0].kind, 'point');
	control.apply({ operation: 'remove_annotation', annotation_id: 'generated-id' });
	assert.equal(control.snapshot().annotations.length, 0);
	control.apply({ operation: 'annotate', annotation: { kind: 'note', label: '解释', detail: '非事实源' } });
	control.apply({ operation: 'clear_annotations' });
	assert.equal(control.snapshot().annotations.length, 0);
});

test('invalid control values fail closed', () => {
	const control = createStockWorkbenchControl();
	assert.throws(() => control.apply({ operation: 'present', symbol: '600487' }), /symbol/);
	assert.throws(() => control.apply({ operation: 'present', metric: 'mystery' }), /metric/);
	assert.throws(() => control.apply({ operation: 'present', strategy_key: 'defensive' }), /strategy/);
	assert.throws(() => control.apply({ operation: 'present', panel_visibility: { orders: true } }), /panel/);
	assert.throws(() => control.apply({ operation: 'annotate', annotation: { kind: 'region', label: '反了', start_date: '2026-09-05', end_date: '2026-09-01', low: 1, high: 2 } }), /start_date/);
});

test('all nine deployed research strategies can be presented', () => {
	const control = createStockWorkbenchControl();
	for (const strategy of ['accumulation', 'expansion', 'pullback', 'trend', 'event', 'relay', 'contraction', 'rotation', 'reclaim']) {
		assert.equal(control.apply({ operation: 'present', strategy_key: strategy }).strategy_key, strategy);
	}
});
