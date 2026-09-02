import assert from 'node:assert/strict';
import test from 'node:test';
import { errorMessage, routeErrorHandler, sanitizeErrorForLog } from './error-response.mjs';

function fakeResponse() {
	const state = { statusCode: null, headers: null, body: null, ended: false };
	return {
		state,
		writeHead(status, headers) { state.statusCode = status; state.headers = headers; },
		end(body) { state.body = body; state.ended = true; },
	};
}

test('errorMessage never leaks a raw non-Error value', () => {
	assert.equal(errorMessage(new Error('boom')), 'boom');
	assert.equal(errorMessage('plain string'), 'plain string');
	assert.equal(errorMessage({ some: 'object' }), 'unexpected error');
	assert.equal(errorMessage(undefined), 'unexpected error');
});

test('routeErrorHandler writes only a status and a sanitized message, never the raw error object', () => {
	const response = fakeResponse();
	const axiosLikeError = new Error('request failed with status code 401');
	axiosLikeError.config = { headers: { Authorization: 'Bearer super-secret-token' } };
	routeErrorHandler(response, 401)(axiosLikeError);
	assert.equal(response.state.statusCode, 401);
	assert.equal(response.state.headers['content-type'], 'application/json');
	const parsed = JSON.parse(response.state.body);
	assert.equal(parsed.status, 'error');
	assert.equal(parsed.message, 'request failed with status code 401');
	assert.ok(!response.state.body.includes('super-secret-token'));
});

test('routeErrorHandler defaults to 503 and always ends the response', () => {
	const response = fakeResponse();
	routeErrorHandler(response)(new Error('db unreachable'));
	assert.equal(response.state.statusCode, 503);
	assert.equal(response.state.ended, true);
});

test('sanitizeErrorForLog drops everything except message/name/code/status', () => {
	const error = new Error('boom');
	error.code = 'ECONNRESET';
	error.config = { headers: { Authorization: 'Bearer leak-me' } };
	error.request = { path: '/secret' };
	const safe = sanitizeErrorForLog(error);
	assert.equal(safe.message, 'boom');
	assert.equal(safe.code, 'ECONNRESET');
	assert.equal(safe.config, undefined);
	assert.equal(safe.request, undefined);
	assert.ok(!JSON.stringify(safe).includes('leak-me'));
});
