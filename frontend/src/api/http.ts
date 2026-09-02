/** Typed JSON transport shared by dashboard feature modules.
 *
 * The adapter occasionally returns an HTML error page for a failed proxy
 * request.  Decode it here so every feature reports a useful error instead of
 * leaking a raw JSON parser exception into the UI.
 */
export async function decodeJson<T>(response: Response, path: string): Promise<T> {
  const text = await response.text();
  const contentType = response.headers.get('content-type') ?? '';
  let data: unknown;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    const preview = text.trim().replace(/\s+/g, ' ').slice(0, 120);
    throw new Error(`${path} 返回了非 JSON 响应（${contentType || '无 content-type'}）：${preview || '空响应'}`);
  }
  if (!response.ok) {
    const payload = data as { detail?: string; message?: string };
    throw new Error(payload.detail ?? payload.message ?? `HTTP ${response.status}`);
  }
  return data as T;
}

export async function getJson<T>(path: string, options: { signal?: AbortSignal } = {}): Promise<T> {
  return decodeJson<T>(await fetch(path, {
    headers: { accept: 'application/json' }, cache: 'no-store', signal: options.signal,
  }), path);
}

export async function postJson<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  return writeJson<T>('POST', path, body);
}

export async function putJson<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  return writeJson<T>('PUT', path, body);
}

export async function deleteJson<T>(path: string): Promise<T> {
  return decodeWriteJson<T>(await fetch(path, {
    method: 'DELETE', headers: { accept: 'application/json', ...dashboardKeyHeaders() },
  }), path);
}

async function writeJson<T>(method: 'POST' | 'PUT', path: string, body: Record<string, unknown>): Promise<T> {
  return decodeWriteJson<T>(await fetch(path, {
    method,
    headers: { 'content-type': 'application/json', accept: 'application/json', ...dashboardKeyHeaders() },
    body: JSON.stringify(body),
  }), path);
}

// --- Operator key (X-Dashboard-Key) -----------------------------------
//
// WP2: every write route in the adapter now requires an `X-Dashboard-Key`
// header matching its DASHBOARD_OPERATOR_KEY env var. The frontend never
// ships a key; an operator sets one for their own browser via
// setDashboardKey (persisted to localStorage so it survives a reload) or a
// one-time `?dashboard_key=` URL parameter consumed at startup (see
// main.ts). The storage key name (`dashboardOperatorKey`) is intentionally
// the same one the adapter's own /relay page reads/writes, so a key set from
// either surface is visible to the other in the same browser.
const DASHBOARD_KEY_STORAGE_KEY = 'dashboardOperatorKey';
let cachedDashboardKey: string | null = null;

function readStoredDashboardKey(): string {
  try {
    return localStorage.getItem(DASHBOARD_KEY_STORAGE_KEY) ?? '';
  } catch {
    // localStorage can throw in a locked-down/private browsing context; fall
    // back to "no key" rather than breaking the read paths that don't need one.
    return '';
  }
}

export function getDashboardKey(): string {
  if (cachedDashboardKey === null) cachedDashboardKey = readStoredDashboardKey();
  return cachedDashboardKey;
}

export function setDashboardKey(value: string): void {
  const trimmed = value.trim();
  cachedDashboardKey = trimmed;
  try {
    if (trimmed) localStorage.setItem(DASHBOARD_KEY_STORAGE_KEY, trimmed);
    else localStorage.removeItem(DASHBOARD_KEY_STORAGE_KEY);
  } catch {
    // Keep the in-memory value for this session even if persistence fails.
  }
}

function dashboardKeyHeaders(): Record<string, string> {
  const key = getDashboardKey();
  return key ? { 'X-Dashboard-Key': key } : {};
}

async function decodeWriteJson<T>(response: Response, path: string): Promise<T> {
  if ((response.status === 401 || response.status === 403) && !getDashboardKey()) {
    throw new Error(`${path} 需要先设置操作者 Key 才能执行写操作；请点击“设置 Key”后重试。`);
  }
  return decodeJson<T>(response, path);
}
