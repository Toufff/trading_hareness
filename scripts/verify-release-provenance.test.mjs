import assert from 'node:assert/strict';
import test from 'node:test';
import { validateBuildMetadata } from './verify-release-provenance.mjs';

test('release provenance requires an exact build contract', () => {
	const payload = { status: 'ok', build: { git_sha: 'a1b2c3d4e5f6', release: 'edge-2026.08.24.1', build_created_at: '2026-08-24T12:00:00Z' } };
	assert.equal(validateBuildMetadata(payload, { expectedSha: 'a1b2c3d', expectedRelease: 'edge-2026.08.24.1' }).git_sha, 'a1b2c3d4e5f6');
	assert.throws(() => validateBuildMetadata(payload, { expectedSha: 'deadbee' }));
	assert.throws(() => validateBuildMetadata({ status: 'ok', build: { git_sha: 'unknown' } }));
});
