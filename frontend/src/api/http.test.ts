import { afterEach, describe, expect, it, vi } from 'vitest';

import { decodeJson, deleteJson, getDashboardKey, getJson, postJson, putJson, setDashboardKey } from './http';

afterEach(() => {
  setDashboardKey('');
  localStorage.clear();
});

describe('frontend HTTP boundary', () => {
  it('returns valid JSON responses', async () => {
    const response = new Response(JSON.stringify({ status: 'ok' }), {
      headers: { 'content-type': 'application/json' },
    });

    await expect(decodeJson<{ status: string }>(response, '/health')).resolves.toEqual({ status: 'ok' });
  });

  it('turns an HTML proxy response into a readable error instead of JSON parse noise', async () => {
    const response = new Response('<!doctype html><title>gateway unavailable</title>', {
      status: 502,
      statusText: 'Bad Gateway',
      headers: { 'content-type': 'text/html' },
    });

    await expect(decodeJson(response, '/api/research/overview')).rejects.toThrow(
      /非 JSON.*gateway unavailable/i,
    );
  });

  it('uses the shared JSON transport for every JSON HTTP method', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ value: 1 }), { headers: { 'content-type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ accepted: true }), { headers: { 'content-type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ updated: true }), { headers: { 'content-type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ deleted: true }), { headers: { 'content-type': 'application/json' } }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(getJson<{ value: number }>('/api/example')).resolves.toEqual({ value: 1 });
    await expect(postJson<{ accepted: boolean }>('/api/example', { mode: 'safe' })).resolves.toEqual({ accepted: true });
    await expect(putJson<{ updated: boolean }>('/api/example', { enabled: true })).resolves.toEqual({ updated: true });
    await expect(deleteJson<{ deleted: boolean }>('/api/example')).resolves.toEqual({ deleted: true });
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/example', expect.objectContaining({ headers: expect.objectContaining({ accept: 'application/json' }) }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/example', expect.objectContaining({ method: 'POST', body: JSON.stringify({ mode: 'safe' }) }));
    expect(fetchMock).toHaveBeenNthCalledWith(3, '/api/example', expect.objectContaining({ method: 'PUT', body: JSON.stringify({ enabled: true }) }));
    expect(fetchMock).toHaveBeenNthCalledWith(4, '/api/example', expect.objectContaining({ method: 'DELETE' }));
  });

  it('forwards an abort signal so inactive dashboard sections can cancel heavy reads', async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: 'ok' }), { headers: { 'content-type': 'application/json' } }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await getJson('/api/research/overview', { signal: controller.signal });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/research/overview',
      expect.objectContaining({ signal: controller.signal }),
    );
  });

  describe('X-Dashboard-Key operator header', () => {
    it('sends no key header until one has been set', async () => {
      const fetchMock = vi.fn((..._args: Parameters<typeof fetch>) => Promise.resolve(
        new Response(JSON.stringify({ accepted: true }), { headers: { 'content-type': 'application/json' } }),
      ));
      vi.stubGlobal('fetch', fetchMock);

      await postJson('/api/research/pipeline/daily');

      const [, init] = fetchMock.mock.calls[0]!;
      expect((init as RequestInit).headers).not.toHaveProperty('X-Dashboard-Key');
    });

    it('attaches X-Dashboard-Key to write requests once an operator key is set, and persists it', async () => {
      setDashboardKey('op-secret-1');
      expect(getDashboardKey()).toBe('op-secret-1');
      expect(localStorage.getItem('dashboardOperatorKey')).toBe('op-secret-1');

      const fetchMock = vi.fn((..._args: Parameters<typeof fetch>) => Promise.resolve(
        new Response(JSON.stringify({ accepted: true }), { headers: { 'content-type': 'application/json' } }),
      ));
      vi.stubGlobal('fetch', fetchMock);

      await postJson('/api/research/pipeline/daily');
      await putJson('/api/example', {});
      await deleteJson('/api/example');

      for (const [, init] of fetchMock.mock.calls) {
        expect((init as Record<string, string>).headers).toHaveProperty('X-Dashboard-Key', 'op-secret-1');
      }
    });

    it('does not attach the key to plain reads', async () => {
      setDashboardKey('op-secret-1');
      const fetchMock = vi.fn((..._args: Parameters<typeof fetch>) => Promise.resolve(
        new Response(JSON.stringify({ status: 'ok' }), { headers: { 'content-type': 'application/json' } }),
      ));
      vi.stubGlobal('fetch', fetchMock);

      await getJson('/api/research/overview');

      const [, init] = fetchMock.mock.calls[0]!;
      expect((init as RequestInit).headers).not.toHaveProperty('X-Dashboard-Key');
    });

    it('turns an unauthorized write into a readable "set the key" prompt when no key is set', async () => {
      const fetchMock = vi.fn().mockResolvedValue(new Response(
        JSON.stringify({ detail: 'missing X-Dashboard-Key' }),
        { status: 401, headers: { 'content-type': 'application/json' } },
      ));
      vi.stubGlobal('fetch', fetchMock);

      await expect(postJson('/api/research/pipeline/daily')).rejects.toThrow(/设置操作者 Key|设置.*Key/);
    });

    it('surfaces the backend error as-is once a key has been set (a 401 then means the key is wrong)', async () => {
      setDashboardKey('op-secret-1');
      const fetchMock = vi.fn().mockResolvedValue(new Response(
        JSON.stringify({ detail: 'invalid dashboard key' }),
        { status: 401, headers: { 'content-type': 'application/json' } },
      ));
      vi.stubGlobal('fetch', fetchMock);

      await expect(postJson('/api/research/pipeline/daily')).rejects.toThrow('invalid dashboard key');
    });
  });
});
