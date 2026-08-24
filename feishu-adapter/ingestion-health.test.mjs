import assert from 'node:assert/strict';
import test from 'node:test';
import { isOperatorPausedIngestion } from './ingestion-health.mjs';

test('recognizes an operator-paused relay without treating a transport failure as paused', () => {
	assert.equal(isOperatorPausedIngestion({ error_class: 'operator_pause' }), true);
	assert.equal(isOperatorPausedIngestion({ error_class: 'n8n_webhook' }), false);
	assert.equal(isOperatorPausedIngestion(null), false);
});
