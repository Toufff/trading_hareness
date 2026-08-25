import { describe, expect, it, vi } from 'vitest';

import { decodeJson, deleteJson, getJson, postJson, putJson } from './http';

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
});
