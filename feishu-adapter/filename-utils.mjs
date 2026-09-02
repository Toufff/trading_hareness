// Shared header/filename helpers. index.mjs and group-relay.mjs previously
// carried two slightly different implementations: index.mjs's variant
// stripped CJK characters from the recovered filename while group-relay.mjs's
// variant (used here) preserves them, matching filenameForMediaType's own
// sanitizer. Preserving CJK filenames is strictly more useful, so both
// callers now share this version.
export function headerValue(headers, name) {
	return headers?.[name] ?? headers?.[name.toLowerCase()] ?? headers?.[name.toUpperCase()] ?? '';
}

export function filenameFromHeaders(headers, fallback) {
	const contentDisposition = String(headerValue(headers, 'content-disposition') ?? '');
	const match = contentDisposition.match(/filename\*?=(?:UTF-8''|\")?([^;\"]+)/i);
	return match ? decodeURIComponent(match[1].replace(/\"/g, '')).replace(/[^\w.\-()一-鿿]+/g, '_') : fallback;
}
