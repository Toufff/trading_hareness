import { afterEach, describe, expect, it, vi } from 'vitest';

import { useFeishuRelayWorkspace } from './useFeishuRelayWorkspace';

const jsonResponse = (value: unknown) => new Response(JSON.stringify(value), {
  headers: { 'content-type': 'application/json' },
});

afterEach(() => vi.unstubAllGlobals());

describe('useFeishuRelayWorkspace', () => {
  it('owns relay and workbench status loading outside the dashboard shell', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'healthy', sources: [{ key: 'anqiang', tag: 'anqiang', chat_name: '马安强', state: 'healthy' }] }))
      .mockResolvedValueOnce(jsonResponse({ target_configured: true, capabilities: [] }))
      .mockResolvedValueOnce(jsonResponse({ items: [{ source_message_id: 'om_1', route_tag: 'anqiang', status: 'sent' }] }));
    vi.stubGlobal('fetch', fetchMock);
    const workspace = useFeishuRelayWorkspace();

    await workspace.loadGroupRelayStatus();
    await workspace.loadFeishuWorkbench();

    expect(workspace.groupRelayStatus.value.sources?.[0]?.tag).toBe('anqiang');
    expect(workspace.feishuWorkbench.value.target_configured).toBe(true);
    expect(workspace.feishuWorkbenchMessages.value[0]?.source_message_id).toBe('om_1');
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/group-relay/status', expect.any(Object));
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/feishu-workbench/status', expect.any(Object));
    expect(fetchMock).toHaveBeenNthCalledWith(3, '/api/feishu-workbench/messages?limit=80', expect.any(Object));
  });
});
