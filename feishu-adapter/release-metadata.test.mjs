import assert from 'node:assert/strict';
import test from 'node:test';
import { releaseMetadata } from './release-metadata.mjs';

test('release metadata exposes build provenance without accepting arbitrary git text', () => {
	assert.deepEqual(releaseMetadata({
		APP_GIT_SHA: 'A1B2C3D4', APP_RELEASE: 'edge-2026.08.24.1', APP_BUILD_CREATED_AT: '2026-08-24T12:00:00Z',
	}), {
		git_sha: 'a1b2c3d4', release: 'edge-2026.08.24.1', build_created_at: '2026-08-24T12:00:00Z',
	});
	assert.equal(releaseMetadata({ APP_GIT_SHA: 'unknown' }).git_sha, null);
	assert.equal(releaseMetadata({ APP_GIT_SHA: 'not-a-sha' }).git_sha, null);
});
