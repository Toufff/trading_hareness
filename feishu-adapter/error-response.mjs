// Sanitized error handling shared by every dashboard route.
//
// Client-facing responses only ever carry `error.message` (never the full
// error object, and never `String(error)` on something that isn't a plain
// Error). Server-side logs for process-level handlers likewise only print
// message/status/code, never the whole object -- the Lark SDK's underlying
// AxiosError carries the outbound request, including its Authorization
// header, on `error.config`/`error.request`, and PostgreSQL errors can carry
// connection details on non-enumerable fields that a naive `console.error`
// or JSON.stringify would still surface.
export function errorMessage(error, fallback = 'unexpected error') {
	if (error instanceof Error) return error.message || fallback;
	if (typeof error === 'string' && error) return error;
	return fallback;
}

// One helper standing in for the ~50 near-identical
// `.catch((error) => { response.writeHead(status, {...}); response.end(...); })`
// blocks that used to be inlined at every route.
export function routeErrorHandler(response, status = 503) {
	return (error) => {
		const message = errorMessage(error);
		try {
			response.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' });
		} catch {
			// Headers already sent (e.g. streaming response); fall through so the
			// caller at least sees the connection close rather than an uncaught
			// rejection.
		}
		try {
			response.end(JSON.stringify({ status: 'error', message }));
		} catch {
			// Response may already be finished.
		}
	};
}

// A safe-to-log projection of an error for process-level handlers. Never
// spreads the source object or logs it directly.
export function sanitizeErrorForLog(error) {
	if (error instanceof Error) {
		const detail = { message: error.message };
		if (error.name && error.name !== 'Error') detail.name = error.name;
		if (error.code !== undefined) detail.code = error.code;
		const status = error.status ?? error.response?.status;
		if (status !== undefined) detail.status = status;
		return detail;
	}
	return { message: String(error ?? 'unknown error') };
}
