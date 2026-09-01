import assert from 'node:assert/strict';
import test from 'node:test';

import { personalDecisionResearchPaths } from './personal-decision-routes.mjs';

test('personal decision UI has read-only proxy routes for every independent section', () => {
	assert.deepEqual([...personalDecisionResearchPaths.entries()], [
		['/api/research/personal/portfolio-snapshots/latest', '/api/v1/personal/portfolio-snapshots/latest'],
		['/api/research/personal/decision-briefs/latest', '/api/v1/personal/decision-briefs/latest'],
		['/api/research/personal/decision-research/latest', '/api/v1/personal/decision-research/latest'],
	]);
});
