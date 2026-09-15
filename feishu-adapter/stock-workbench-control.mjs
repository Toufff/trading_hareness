import { randomUUID } from 'node:crypto';

const STRATEGIES = new Set([
	'accumulation', 'expansion', 'pullback', 'trend', 'event', 'relay',
	'contraction', 'rotation', 'reclaim',
]);
const TIMEFRAMES = new Set(['daily', 'weekly']);
const METRICS = new Set(['vendor_flow', 'volume', 'turnover', 'macd', 'rsi']);
const PANELS = new Set(['price', 'metric', 'next_session', 'next_week', 'messages', 'trade_plan']);
const ANNOTATION_KINDS = new Set(['price_line', 'point', 'region', 'note']);
const FOCUS_SCOPES = new Set(['next_session', 'next_week']);
const SYMBOL_PATTERN = /^\d{6}\.(?:SH|SZ|BJ)$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

function fail(message) {
	const error = new Error(message);
	error.statusCode = 400;
	throw error;
}

function finiteNumber(value, label, { min = -Infinity, max = Infinity, required = false } = {}) {
	if (value === undefined || value === null || value === '') {
		if (required) fail(`${label} is required`);
		return undefined;
	}
	const number = Number(value);
	if (!Number.isFinite(number) || number < min || number > max) fail(`${label} must be between ${min} and ${max}`);
	return number;
}

function shortText(value, label, maxLength, { required = false } = {}) {
	const text = String(value ?? '').trim();
	if (required && !text) fail(`${label} is required`);
	if (text.length > maxLength) fail(`${label} must be at most ${maxLength} characters`);
	return text || undefined;
}

function isoDate(value, label, { required = false } = {}) {
	const text = shortText(value, label, 10, { required });
	if (text && !DATE_PATTERN.test(text)) fail(`${label} must use YYYY-MM-DD`);
	return text;
}

function enumValue(value, allowed, label) {
	if (value === undefined || value === null || value === '') return undefined;
	const text = String(value).trim();
	if (!allowed.has(text)) fail(`${label} is not supported`);
	return text;
}

function clone(value) {
	return structuredClone(value);
}

function normalizeAnnotation(raw, idFactory) {
	if (!raw || typeof raw !== 'object' || Array.isArray(raw)) fail('annotation must be an object');
	const kind = enumValue(raw.kind, ANNOTATION_KINDS, 'annotation.kind');
	if (!kind) fail('annotation.kind is required');
	const annotation = {
		id: shortText(raw.id, 'annotation.id', 80) ?? idFactory(),
		kind,
		label: shortText(raw.label, 'annotation.label', 80, { required: true }),
		detail: shortText(raw.detail, 'annotation.detail', 600),
		color: shortText(raw.color, 'annotation.color', 32),
		source_label: shortText(raw.source_label, 'annotation.source_label', 120),
		as_of: shortText(raw.as_of, 'annotation.as_of', 40),
	};
	if (kind === 'price_line') annotation.price = finiteNumber(raw.price, 'annotation.price', { min: 0, required: true });
	if (kind === 'point') {
		annotation.date = isoDate(raw.date, 'annotation.date', { required: true });
		annotation.price = finiteNumber(raw.price, 'annotation.price', { min: 0, required: true });
	}
	if (kind === 'region') {
		annotation.start_date = isoDate(raw.start_date, 'annotation.start_date', { required: true });
		annotation.end_date = isoDate(raw.end_date, 'annotation.end_date', { required: true });
		annotation.low = finiteNumber(raw.low, 'annotation.low', { min: 0, required: true });
		annotation.high = finiteNumber(raw.high, 'annotation.high', { min: 0, required: true });
		if (annotation.start_date > annotation.end_date) fail('annotation.start_date must not be after end_date');
		if (annotation.low > annotation.high) fail('annotation.low must not exceed high');
	}
	return annotation;
}

function normalizeSpeakerNote(raw) {
	if (raw === null) return null;
	if (!raw || typeof raw !== 'object' || Array.isArray(raw)) fail('speaker_note must be an object or null');
	return {
		title: shortText(raw.title, 'speaker_note.title', 100, { required: true }),
		body: shortText(raw.body, 'speaker_note.body', 1200, { required: true }),
		source_label: shortText(raw.source_label, 'speaker_note.source_label', 120),
		as_of: shortText(raw.as_of, 'speaker_note.as_of', 40),
	};
}

function idleState(revision, now) {
	return {
		contract_version: 'stock-workbench-control.v1',
		revision,
		active: false,
		workspace_id: 'primary',
		updated_at: new Date(now).toISOString(),
		expires_at: null,
		symbol: null,
		lookback_days: 120,
		strategy_key: null,
		timeframe: 'daily',
		metric: null,
		zoom: { start: 55, end: 100 },
		focus: null,
		panel_visibility: Object.fromEntries([...PANELS].map((key) => [key, true])),
		annotations: [],
		speaker_note: null,
		presentation_only: true,
	};
}

export function createStockWorkbenchControl({ now = () => Date.now(), idFactory = randomUUID, defaultTtlSeconds = 1800 } = {}) {
	let state = idleState(0, now());

	function snapshot() {
		if (state.active && state.expires_at && Date.parse(state.expires_at) <= now()) {
			state = idleState(state.revision + 1, now());
		}
		return clone(state);
	}

	function apply(raw = {}) {
		if (!raw || typeof raw !== 'object' || Array.isArray(raw)) fail('control command must be an object');
		const operation = String(raw.operation ?? 'present').trim();
		if (!new Set(['present', 'annotate', 'remove_annotation', 'clear_annotations', 'reset']).has(operation)) fail('operation is not supported');
		const current = snapshot();
		if (operation === 'reset') {
			state = idleState(current.revision + 1, now());
			return snapshot();
		}

		const ttlSeconds = finiteNumber(raw.ttl_seconds, 'ttl_seconds', { min: 30, max: 86_400 }) ?? defaultTtlSeconds;
		const next = clone(current);
		next.revision += 1;
		next.active = true;
		next.workspace_id = shortText(raw.workspace_id ?? next.workspace_id, 'workspace_id', 64, { required: true });
		if (!/^[a-zA-Z0-9_-]+$/.test(next.workspace_id)) fail('workspace_id may only contain letters, digits, underscores and hyphens');
		next.updated_at = new Date(now()).toISOString();
		next.expires_at = new Date(now() + ttlSeconds * 1000).toISOString();

		if (operation === 'present') {
			if (raw.symbol !== undefined) {
				const symbol = String(raw.symbol ?? '').trim().toUpperCase();
				if (!SYMBOL_PATTERN.test(symbol)) fail('symbol must look like 600487.SH');
				next.symbol = symbol;
			}
			const lookback = finiteNumber(raw.lookback_days, 'lookback_days', { min: 30, max: 300 });
			if (lookback !== undefined) next.lookback_days = Math.trunc(lookback);
			const strategy = enumValue(raw.strategy_key, STRATEGIES, 'strategy_key');
			if (strategy) next.strategy_key = strategy;
			const timeframe = enumValue(raw.timeframe, TIMEFRAMES, 'timeframe');
			if (timeframe) next.timeframe = timeframe;
			const metric = enumValue(raw.metric, METRICS, 'metric');
			if (metric) next.metric = metric;
			if (raw.zoom !== undefined) {
				if (!raw.zoom || typeof raw.zoom !== 'object' || Array.isArray(raw.zoom)) fail('zoom must be an object');
				const start = finiteNumber(raw.zoom.start, 'zoom.start', { min: 0, max: 99 }) ?? next.zoom.start;
				const end = finiteNumber(raw.zoom.end, 'zoom.end', { min: 1, max: 100 }) ?? next.zoom.end;
				if (start >= end) fail('zoom.start must be below zoom.end');
				next.zoom = { start, end };
			}
			if (raw.focus === null) next.focus = null;
			else if (raw.focus !== undefined) {
				if (!raw.focus || typeof raw.focus !== 'object' || Array.isArray(raw.focus)) fail('focus must be an object or null');
				next.focus = {
					scope: enumValue(raw.focus.scope, FOCUS_SCOPES, 'focus.scope'),
					state: shortText(raw.focus.state, 'focus.state', 80, { required: true }),
				};
				if (!next.focus.scope) fail('focus.scope is required');
			}
			if (raw.panel_visibility !== undefined) {
				if (!raw.panel_visibility || typeof raw.panel_visibility !== 'object' || Array.isArray(raw.panel_visibility)) fail('panel_visibility must be an object');
				for (const [panel, visible] of Object.entries(raw.panel_visibility)) {
					if (!PANELS.has(panel)) fail(`unsupported panel: ${panel}`);
					if (typeof visible !== 'boolean') fail(`panel_visibility.${panel} must be boolean`);
					next.panel_visibility[panel] = visible;
				}
			}
			if (raw.speaker_note !== undefined) next.speaker_note = normalizeSpeakerNote(raw.speaker_note);
		}

		if (operation === 'annotate') {
			const annotation = normalizeAnnotation(raw.annotation, idFactory);
			next.annotations = [...next.annotations.filter((item) => item.id !== annotation.id), annotation].slice(-40);
		}
		if (operation === 'remove_annotation') {
			const id = shortText(raw.annotation_id, 'annotation_id', 80, { required: true });
			next.annotations = next.annotations.filter((item) => item.id !== id);
		}
		if (operation === 'clear_annotations') next.annotations = [];
		state = next;
		return snapshot();
	}

	return { snapshot, apply };
}

export const stockWorkbenchControlVocabulary = Object.freeze({
	strategies: [...STRATEGIES], timeframes: [...TIMEFRAMES], metrics: [...METRICS], panels: [...PANELS], annotation_kinds: [...ANNOTATION_KINDS], focus_scopes: [...FOCUS_SCOPES],
});
