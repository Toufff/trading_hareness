/**
 * A Feishu-backed retry must re-read its immutable source message. This avoids
 * retrying a stale or partially written local copy and keeps the retry boundary
 * at the source message ID. A manual relay has no source resource to download.
 */
export function shouldRedownloadRetryMedia({ expectedResourceCount = 0, event = null }) {
	return Boolean(event?.message?.message_id && expectedResourceCount > 0);
}
