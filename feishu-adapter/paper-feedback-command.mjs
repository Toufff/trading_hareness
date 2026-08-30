const ITEM = /^(?:\d{1,2}|\d{4}-W\d{2}-\d{2})$/i;

function parseItems(raw) {
	return [...new Set(String(raw ?? '')
		.split(/[\s,，、]+/)
		.map((value) => value.trim())
		.filter((value) => ITEM.test(value))
		.map((value) => /^\d{1,2}$/.test(value) ? Number(value) : value))];
}

/** Parse only explicit recommendation commands; ordinary group chatter is ignored. */
export function parsePaperFeedback(messageText) {
	const text = String(messageText ?? '').trim();
	let match = text.match(/^(收|留|略|稍后|原因)(?=\s|[:：])\s*[:：]?\s*(.+)$/i);
	if (match) {
		const actions = { 收: 'accept', 留: 'save', 略: 'dismiss', 稍后: 'snooze', 原因: 'reason' };
		const items = parseItems(match[2]);
		return items.length ? { action: actions[match[1]], items } : null;
	}
	match = text.match(/^(多点|少点)(?:\s|[:：])+(.+)$/i);
	if (match) {
		const topic = match[2].trim().slice(0, 80);
		return topic ? { action: match[1] === '多点' ? 'topic_more' : 'topic_less', topic } : null;
	}
	match = text.match(/^作者\s*\+\s*(.+)$/i);
	if (match) {
		const author = match[1].trim().slice(0, 120);
		return author ? { action: 'follow_author', author } : null;
	}
	return null;
}
