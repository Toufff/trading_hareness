import { randomUUID } from 'node:crypto';

// Manual-relay event_id/message_id are the ledger's UNIQUE dedupe keys. A
// caller-supplied id would let anyone pre-register a real Feishu message_id
// so the genuine message is later silently dropped as a duplicate. These are
// therefore always generated server-side and tagged so they can never
// collide with (or be confused for) a real Feishu identifier.
export function manualEventId() {
	return `manual:${randomUUID()}`;
}

export function manualMessageId() {
	return `manual:${randomUUID()}`;
}
