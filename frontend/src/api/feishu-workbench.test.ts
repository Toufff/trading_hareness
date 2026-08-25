import { describe, expect, it, vi } from 'vitest';

import { feishuWorkbenchApi } from './feishu-workbench';
import { groupRelayApi } from './group-relay';

const jsonResponse = (value: unknown) => new Response(JSON.stringify(value), {
  headers: { 'content-type': 'application/json' },
});

describe('Feishu feature API boundaries', () => {
  it('keeps workbench actions behind the shared JSON transport', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ accepted: true }))
      .mockResolvedValueOnce(jsonResponse({ accepted: true }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(feishuWorkbenchApi.updateMessageState('om_1', 'focus')).resolves.toEqual({ accepted: true });
    await expect(feishuWorkbenchApi.searchMessages<{ items: string[] }>('新能源')).resolves.toEqual({ accepted: true });

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/feishu-workbench/actions', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ source_message_id: 'om_1', action: 'focus' }),
    }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/feishu-workbench/message-search', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ query: '新能源' }),
    }));
  });

  it('uses explicit create, update and delete route operations', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ route: { key: 'anqiang' } }))
      .mockResolvedValueOnce(jsonResponse({ route: { key: 'anqiang' } }))
      .mockResolvedValueOnce(jsonResponse({ deleted: true }));
    vi.stubGlobal('fetch', fetchMock);
    const route = { chat_name: '马安强', tag: 'anqiang', enabled: true };

    await groupRelayApi.upsertRoute('', route);
    await groupRelayApi.upsertRoute('anqiang', route);
    await groupRelayApi.removeRoute('anqiang');

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/group-relay/routes', expect.objectContaining({ method: 'POST' }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/group-relay/routes/anqiang', expect.objectContaining({ method: 'PUT' }));
    expect(fetchMock).toHaveBeenNthCalledWith(3, '/api/group-relay/routes/anqiang', expect.objectContaining({ method: 'DELETE' }));
  });
});
