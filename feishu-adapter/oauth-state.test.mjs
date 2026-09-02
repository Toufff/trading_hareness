import assert from 'node:assert/strict';
import test from 'node:test';
import { createOauthStateStore } from './oauth-state.mjs';

test('a created state can be consumed exactly once', () => {
	const store = createOauthStateStore();
	const state = store.create();
	assert.equal(store.consume(state), true);
	assert.equal(store.consume(state), false, 'a second consume must fail (single-use)');
});

test('an unknown or caller-supplied state is always rejected', () => {
	const store = createOauthStateStore();
	assert.equal(store.consume('attacker-chosen-state'), false);
	assert.equal(store.consume(''), false);
	assert.equal(store.consume(undefined), false);
});

test('a state older than its TTL expires', async () => {
	const store = createOauthStateStore({ ttlMs: 5 });
	const state = store.create();
	await new Promise((resolve) => setTimeout(resolve, 20));
	assert.equal(store.consume(state), false);
});

test('states created for different requests are unpredictable and independent', () => {
	const store = createOauthStateStore();
	const a = store.create();
	const b = store.create();
	assert.notEqual(a, b);
	assert.equal(store.consume(a), true);
	assert.equal(store.consume(b), true);
});
