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

export async function getJson<T>(path: string): Promise<T> {
  return decodeJson<T>(await fetch(path, { headers: { accept: 'application/json' }, cache: 'no-store' }), path);
}

export async function postJson<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  return writeJson<T>('POST', path, body);
}

export async function putJson<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  return writeJson<T>('PUT', path, body);
}

export async function deleteJson<T>(path: string): Promise<T> {
  return decodeJson<T>(await fetch(path, { method: 'DELETE', headers: { accept: 'application/json' } }), path);
}

async function writeJson<T>(method: 'POST' | 'PUT', path: string, body: Record<string, unknown>): Promise<T> {
  return decodeJson<T>(await fetch(path, {
    method, headers: { 'content-type': 'application/json', accept: 'application/json' }, body: JSON.stringify(body),
  }), path);
}
