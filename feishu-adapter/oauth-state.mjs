import { randomUUID } from 'node:crypto';

// A server-generated, single-use, short-lived OAuth `state` value. This
// prevents a caller from choosing (and later replaying) its own state, which
// would otherwise let an attacker bind their own authorization code to a
// victim's session (login CSRF / account-binding attack).
export function createOauthStateStore({ ttlMs = 10 * 60 * 1000 } = {}) {
	const states = new Map();

	function prune(now = Date.now()) {
		for (const [key, expiresAt] of states) {
			if (expiresAt <= now) states.delete(key);
		}
	}

	return {
		create() {
			prune();
			const state = randomUUID();
			states.set(state, Date.now() + ttlMs);
			return state;
		},
		// One-time use: a valid state is removed as soon as it is checked,
		// whether or not the caller goes on to complete the exchange.
		consume(state) {
			prune();
			const value = String(state ?? '');
			if (!value || !states.has(value)) return false;
			states.delete(value);
			return true;
		},
		size() {
			prune();
			return states.size;
		},
	};
}
