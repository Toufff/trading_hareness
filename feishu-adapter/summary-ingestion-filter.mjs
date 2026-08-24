export function hasImportableTaggedPayload(messageText, { hasMedia = false } = {}) {
	if (hasMedia) return true;
	const tagged = String(messageText ?? '').match(/^#[a-z0-9-]+(?=\s|$)\s*/i);
	if (!tagged) return false;
	return String(messageText).slice(tagged[0].length).trim().length > 0;
}
