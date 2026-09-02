import assert from 'node:assert/strict';
import test from 'node:test';
import { createOperatorAuth, isMutatingApiRoute, isSameOriginRequest, resolveOperatorAuthConfig, timingSafeEqualStrings } from './dashboard-auth.mjs';

test('mutating /api/* routes require operator auth regardless of the specific path', () => {
	assert.equal(isMutatingApiRoute('POST', '/api/feishu-workbench/actions'), true);
	assert.equal(isMutatingApiRoute('PUT', '/api/research/paper/accounts'), true);
	assert.equal(isMutatingApiRoute('DELETE', '/api/group-relay/routes/abc'), true);
	assert.equal(isMutatingApiRoute('POST', '/api/baidu-pan/manage'), true);
});

test('read-only GET routes never require operator auth', () => {
	assert.equal(isMutatingApiRoute('GET', '/api/config'), false);
	assert.equal(isMutatingApiRoute('GET', '/api/jobs/abc'), false);
	assert.equal(isMutatingApiRoute('GET', '/health'), false);
	assert.equal(isMutatingApiRoute('GET', '/events'), false);
	assert.equal(isMutatingApiRoute('GET', '/jobs'), false);
});

test('named non-/api/ mutating routes are covered explicitly', () => {
	for (const path of ['/manual-relay', '/reconcile', '/n8n-status', '/n8n-error']) {
		assert.equal(isMutatingApiRoute('POST', path), true, path);
	}
});

test('internal token-gated routes are not swept into the operator-key requirement', () => {
	assert.equal(isMutatingApiRoute('POST', '/internal/quant-alert'), false);
	assert.equal(isMutatingApiRoute('POST', '/internal/feishu-user-oauth'), false);
});

test('resolveOperatorAuthConfig fails closed when no key and no explicit opt-out are configured', () => {
	assert.throws(() => resolveOperatorAuthConfig({}), /DASHBOARD_OPERATOR_KEY/);
});

test('resolveOperatorAuthConfig allows an explicit, logged opt-out', () => {
	const config = resolveOperatorAuthConfig({ DASHBOARD_ALLOW_UNAUTHENTICATED: '1' });
	assert.equal(config.enabled, false);
	assert.equal(config.allowUnauthenticated, true);
});

test('createOperatorAuth rejects missing or wrong keys and accepts the right one', () => {
	const config = resolveOperatorAuthConfig({ DASHBOARD_OPERATOR_KEY: 'super-secret' });
	const auth = createOperatorAuth(config, { warn() {} });
	assert.equal(auth.check({}), false);
	assert.equal(auth.check({ 'x-dashboard-key': 'wrong' }), false);
	assert.equal(auth.check({ 'x-dashboard-key': 'super-secret' }), true);
});

test('timingSafeEqualStrings compares by content, not just length', () => {
	assert.equal(timingSafeEqualStrings('abc', 'abc'), true);
	assert.equal(timingSafeEqualStrings('abc', 'abd'), false);
	assert.equal(timingSafeEqualStrings('abc', 'ab'), false);
});

test('same-origin check trusts Sec-Fetch-Site first', () => {
	assert.equal(isSameOriginRequest({ 'sec-fetch-site': 'same-origin' }), true);
	assert.equal(isSameOriginRequest({ 'sec-fetch-site': 'cross-site', origin: 'http://evil.example', host: 'adapter.local' }), false);
});

test('same-origin check falls back to comparing Origin against Host', () => {
	assert.equal(isSameOriginRequest({ origin: 'http://adapter.local', host: 'adapter.local' }), true);
	assert.equal(isSameOriginRequest({ origin: 'http://evil.example', host: 'adapter.local' }), false);
	assert.equal(isSameOriginRequest({}), true, 'no Origin header at all means a non-browser caller');
});
