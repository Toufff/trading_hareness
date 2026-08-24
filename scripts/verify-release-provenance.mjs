import { pathToFileURL } from 'node:url';

function option(name) {
	const index = process.argv.indexOf(name);
	return index >= 0 ? process.argv[index + 1] : undefined;
}

export function validateBuildMetadata(payload, { expectedSha, expectedRelease } = {}) {
	if (payload?.status !== 'ok') throw new Error(`health status must be ok, received ${payload?.status ?? '<missing>'}`);
	const build = payload?.build;
	if (!build || typeof build !== 'object') throw new Error('health payload lacks build provenance');
	if (!/^[0-9a-f]{7,64}$/i.test(String(build.git_sha ?? ''))) throw new Error('health build.git_sha is missing or invalid');
	if (expectedSha && !String(build.git_sha).startsWith(expectedSha.toLowerCase())) throw new Error(`health git SHA ${build.git_sha} does not match ${expectedSha}`);
	if (expectedRelease && build.release !== expectedRelease) throw new Error(`health release ${build.release ?? '<missing>'} does not match ${expectedRelease}`);
	return build;
}

async function readJson(url) {
	const response = await fetch(url, { signal: AbortSignal.timeout(8_000) });
	if (!response.ok) throw new Error(`${url} returned HTTP ${response.status}`);
	return await response.json();
}

export async function main() {
	const url = option('--url');
	if (!url || process.argv.includes('--help')) {
		console.log('Usage: node scripts/verify-release-provenance.mjs --url http://127.0.0.1:18110/health [--sha <git-sha>] [--release <release>]');
		return;
	}
	const build = validateBuildMetadata(await readJson(url), { expectedSha: option('--sha'), expectedRelease: option('--release') });
	console.log(`release provenance verified: sha=${build.git_sha} release=${build.release ?? '<unset>'} built=${build.build_created_at ?? '<unset>'}`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
	main().catch((error) => {
		console.error(error instanceof Error ? error.message : String(error));
		process.exitCode = 1;
	});
}
