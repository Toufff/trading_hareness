const gitShaPattern = /^[0-9a-f]{7,64}$/i;

function text(value) {
	const normalized = String(value ?? '').trim();
	if (!normalized || ['unknown', 'unset', 'none'].includes(normalized.toLowerCase())) return null;
	return normalized.slice(0, 160);
}

export function releaseMetadata(environment = process.env) {
	const gitSha = text(environment.APP_GIT_SHA);
	return {
		git_sha: gitSha && gitShaPattern.test(gitSha) ? gitSha.toLowerCase() : null,
		release: text(environment.APP_RELEASE),
		build_created_at: text(environment.APP_BUILD_CREATED_AT),
	};
}
