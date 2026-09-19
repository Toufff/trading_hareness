import assert from 'node:assert/strict';
import test from 'node:test';

import { isMutatingApiRoute } from './dashboard-auth.mjs';
import { disciplineResearchPaths, resolveDisciplinePlanPath } from './discipline-routes.mjs';

const PLAN_ID = '699144a6-00e4-42a2-bdf7-168e601bfef0';

test('discipline UI has read-only proxy routes for every owner discipline read', () => {
	assert.deepEqual([...disciplineResearchPaths.entries()], [
		['/api/research/discipline/plans/latest', '/api/v1/discipline/plans/latest'],
		['/api/research/discipline/plans/history', '/api/v1/discipline/plans/history'],
		['/api/research/discipline/evaluations/latest', '/api/v1/discipline/evaluations/latest'],
		['/api/research/discipline/reconciliations', '/api/v1/discipline/reconciliations'],
	]);
	for (const upstream of disciplineResearchPaths.values()) assert.match(upstream, /^\/api\/v1\/discipline\//);
});

test('per-plan reads map the plan, its chart and its evaluation history', () => {
	assert.equal(resolveDisciplinePlanPath('GET', `/api/research/discipline/plans/${PLAN_ID}`), `/api/v1/discipline/plans/${PLAN_ID}`);
	assert.equal(resolveDisciplinePlanPath('GET', `/api/research/discipline/plans/${PLAN_ID}/chart`), `/api/v1/discipline/plans/${PLAN_ID}/chart`);
	assert.equal(resolveDisciplinePlanPath('GET', `/api/research/discipline/plans/${PLAN_ID}/evaluations`), `/api/v1/discipline/plans/${PLAN_ID}/evaluations`);
	assert.equal(resolveDisciplinePlanPath('get', `/api/research/discipline/plans/${PLAN_ID.toUpperCase()}/chart`), `/api/v1/discipline/plans/${PLAN_ID}/chart`);
});

test('per-plan mapping refuses anything but a GET on a real plan id', () => {
	for (const method of ['POST', 'PUT', 'DELETE', 'PATCH']) {
		assert.equal(resolveDisciplinePlanPath(method, `/api/research/discipline/plans/${PLAN_ID}`), null);
	}
	assert.equal(resolveDisciplinePlanPath('GET', '/api/research/discipline/plans/not-a-uuid'), null);
	assert.equal(resolveDisciplinePlanPath('GET', `/api/research/discipline/plans/${PLAN_ID}/orders`), null);
	assert.equal(resolveDisciplinePlanPath('GET', `/api/research/discipline/plans/${PLAN_ID}/chart/../../x`), null);
	assert.equal(resolveDisciplinePlanPath('GET', '/api/research/discipline/plans/history'), null);
});

test('reads stay open like other research GETs; any write method needs the operator key', () => {
	const paths = [...disciplineResearchPaths.keys(), `/api/research/discipline/plans/${PLAN_ID}/chart`];
	for (const path of paths) {
		assert.equal(isMutatingApiRoute('GET', path), false);
		for (const method of ['POST', 'PUT', 'DELETE', 'PATCH']) assert.equal(isMutatingApiRoute(method, path), true);
	}
});

test('the dashboard server wires the discipline routes through the shared GET proxy', async () => {
	const { readFile } = await import('node:fs/promises');
	const source = await readFile(new URL('./index.mjs', import.meta.url), 'utf8');
	assert.match(source, /import \{ disciplineResearchPaths, resolveDisciplinePlanPath \} from '\.\/discipline-routes\.mjs';/);
	const researchMap = source.slice(source.indexOf('const researchPaths = new Map(['), source.indexOf('const researchActions = new Map(['));
	assert.match(researchMap, /\.\.\.disciplineResearchPaths,/);
	// the per-plan proxy sits after the operator-key gate and uses the same read-only GET proxy
	const gate = source.indexOf('if (isMutatingApiRoute(method, url.pathname))');
	const wiring = source.indexOf('const disciplinePlanPath = resolveDisciplinePlanPath(request.method, url.pathname);');
	assert.ok(gate > 0 && wiring > gate);
	assert.match(source.slice(wiring, wiring + 300), /proxyResearch\(disciplinePlanPath, url\.search, response\)/);
	const researchActions = source.slice(source.indexOf('const researchActions = new Map(['), source.indexOf('async function proxyResearch'));
	assert.doesNotMatch(researchActions, /discipline/);
});
