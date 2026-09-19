/**
 * Public read-only proxy contract for the trade-discipline card UI.
 *
 * Every route is a GET forwarded to the owner API (quant-service) under
 * /api/v1/discipline/...; there is no write, broker, THS or order path here.
 * Authentication is the adapter's shared rule (dashboard-auth.mjs): GETs under
 * /api/* stay open like every other research read, and any mutating method on
 * these paths is a mutating API route that needs X-Dashboard-Key before it
 * reaches a handler - and no handler exists for it.
 */
export const disciplineResearchPaths = new Map([
	['/api/research/discipline/plans/latest', '/api/v1/discipline/plans/latest'],
	['/api/research/discipline/plans/history', '/api/v1/discipline/plans/history'],
	['/api/research/discipline/evaluations/latest', '/api/v1/discipline/evaluations/latest'],
	['/api/research/discipline/reconciliations', '/api/v1/discipline/reconciliations'],
]);

const disciplinePlanRoute = /^\/api\/research\/discipline\/plans\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(\/chart|\/evaluations)?$/i;

/** Upstream path for a per-plan discipline read, or null (non-GET, malformed id, unknown suffix). */
export function resolveDisciplinePlanPath(method, pathname) {
	if (String(method ?? '').toUpperCase() !== 'GET') return null;
	const match = disciplinePlanRoute.exec(String(pathname ?? ''));
	if (!match) return null;
	return `/api/v1/discipline/plans/${match[1].toLowerCase()}${match[2] ?? ''}`;
}
