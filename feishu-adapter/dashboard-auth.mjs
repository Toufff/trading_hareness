import { timingSafeEqual } from 'node:crypto';

// Mutating dashboard routes (anything that changes state on this adapter or
// forwards a write to the quant service) must present an operator credential.
// Read-only GET routes (/health, /api/config, /events, /jobs, /metrics, and
// GET under /api/*) stay open so local monitoring keeps working without a key.
const MUTATING_METHODS = new Set(['POST', 'PUT', 'DELETE', 'PATCH']);
const EXTRA_MUTATING_PATHS = new Set(['/manual-relay', '/reconcile', '/n8n-status', '/n8n-error']);

export function isMutatingApiRoute(method, pathname) {
	if (!MUTATING_METHODS.has(String(method ?? '').toUpperCase())) return false;
	const path = String(pathname ?? '');
	if (path.startsWith('/api/')) return true;
	return EXTRA_MUTATING_PATHS.has(path);
}

// Browsers set Sec-Fetch-Site on same-origin/none navigations and fetches.
// When that header is absent (older browsers, or non-browser callers such as
// n8n / curl / server-to-server scripts) fall back to comparing Origin with
// Host; a request with neither header is not a cross-site browser request.
export function isSameOriginRequest(headers) {
	const secFetchSite = String(headers?.['sec-fetch-site'] ?? '').toLowerCase();
	if (secFetchSite) return secFetchSite === 'same-origin' || secFetchSite === 'none';
	const origin = headers?.origin;
	if (!origin) return true;
	const host = headers?.host;
	if (!host) return false;
	try {
		return new URL(String(origin)).host === String(host);
	} catch {
		return false;
	}
}

export function timingSafeEqualStrings(a, b) {
	const bufferA = Buffer.from(String(a ?? ''), 'utf8');
	const bufferB = Buffer.from(String(b ?? ''), 'utf8');
	if (bufferA.length !== bufferB.length) {
		// Still perform a constant-time compare of equal-length buffers so the
		// early return above leaks as little timing signal as practical.
		timingSafeEqual(bufferA, bufferA);
		return false;
	}
	return timingSafeEqual(bufferA, bufferB);
}

// Fail-closed by default: an adapter that can reach the quant write API and
// mutate local state must not boot without an operator credential unless an
// operator has explicitly opted into running unauthenticated.
export function resolveOperatorAuthConfig(env = process.env) {
	const operatorKey = String(env.DASHBOARD_OPERATOR_KEY ?? '').trim();
	const allowUnauthenticated = String(env.DASHBOARD_ALLOW_UNAUTHENTICATED ?? '').trim() === '1';
	if (!operatorKey && !allowUnauthenticated) {
		throw new Error('DASHBOARD_OPERATOR_KEY must be configured, or set DASHBOARD_ALLOW_UNAUTHENTICATED=1 to explicitly run every mutating dashboard route without operator authentication');
	}
	return { operatorKey, allowUnauthenticated, enabled: Boolean(operatorKey) };
}

export function createOperatorAuth(config, logger = console) {
	if (!config.enabled && config.allowUnauthenticated) {
		logger.warn('DASHBOARD_OPERATOR_KEY is not set; DASHBOARD_ALLOW_UNAUTHENTICATED=1 leaves every mutating dashboard route open to any caller that can reach this port.');
	}
	return {
		enabled: config.enabled,
		check(headers) {
			if (!config.enabled) return true;
			const provided = headers?.['x-dashboard-key'];
			if (typeof provided !== 'string' || !provided) return false;
			return timingSafeEqualStrings(provided, config.operatorKey);
		},
	};
}
