const PAPER_INGEST_COMMAND = /^(?:收录|收|ingest)(?:\s*[:：]\s*|\s+)(.+)$/i;
const ARXIV_ID = /\b\d{4}\.\d{4,5}(?:v\d+)?\b/gi;

export function parsePaperIngestIds(messageText) {
	const match = String(messageText ?? '').trim().match(PAPER_INGEST_COMMAND);
	if (!match) return null;
	const ids = [...match[1].matchAll(ARXIV_ID)].map((idMatch) => idMatch[0]);
	return ids.length ? [...new Set(ids)] : null;
}
