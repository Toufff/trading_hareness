import { afterEach, describe, expect, it, vi } from 'vitest';

import { usePersonalDecisionWorkspace } from './usePersonalDecisionWorkspace';

const jsonResponse = (value: unknown) => new Response(JSON.stringify(value), {
  headers: { 'content-type': 'application/json' },
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('usePersonalDecisionWorkspace', () => {
  it('loads the latest account brief without merging unavailable sections', async () => {
    const payload = {
      status: 'partial',
      as_of_at: '2026-09-01T15:15:00+08:00',
      market: { status: 'ready', content: { market_state: 'rotation' } },
      holdings: { status: 'blocked', actions: [] },
      new_buys: { status: 'ready', actions: [{ plan_key: 'buy-1', symbol: '600000' }] },
      delivery: {
        market_eligible: true,
        holding_actions_eligible: false,
        new_buy_actions_eligible: true,
      },
      diagnostics: ['broker_snapshot_missing'],
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(payload))
      .mockResolvedValueOnce(jsonResponse({
        as_of_date: '2026-09-01',
        summary: { total: 0, passed: 0, rejected: 0, incomplete: 0 },
        items: [],
      }));
    vi.stubGlobal('fetch', fetchMock);

    const workspace = usePersonalDecisionWorkspace();
    await workspace.load();

    expect(workspace.brief.value).toEqual(payload);
    expect(workspace.brief.value?.holdings.status).toBe('blocked');
    expect(workspace.brief.value?.new_buys.actions).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/research/personal/decision-briefs/latest?account_key=citics-primary',
      expect.any(Object),
    );
  });

  it('clears stale content and exposes a transport failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: 'portfolio unavailable' }),
      { status: 503, headers: { 'content-type': 'application/json' } },
    )));
    const workspace = usePersonalDecisionWorkspace();
    workspace.brief.value = {
      status: 'ready', as_of_at: '2026-09-01T12:00:00+08:00',
      market: { status: 'ready' }, holdings: { status: 'ready' },
      new_buys: { status: 'ready' },
      delivery: { market_eligible: true, holding_actions_eligible: true, new_buy_actions_eligible: false },
    };

    await workspace.load();

    expect(workspace.brief.value).toBeNull();
    expect(workspace.error.value).toBe('portfolio unavailable');
  });

  it('keeps a valid decision brief when the independent research audit fails', async () => {
    const payload = {
      status: 'ready',
      as_of_at: '2026-09-01T15:15:00+08:00',
      market: { status: 'ready', content: { market_state: 'rotation' } },
      holdings: { status: 'ready', actions: [] },
      new_buys: { status: 'ready', actions: [] },
      delivery: { market_eligible: true, holding_actions_eligible: true, new_buy_actions_eligible: false },
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(payload))
      .mockResolvedValueOnce(new Response(
        JSON.stringify({ detail: 'research audit unavailable' }),
        { status: 503, headers: { 'content-type': 'application/json' } },
      ));
    vi.stubGlobal('fetch', fetchMock);

    const workspace = usePersonalDecisionWorkspace();
    await workspace.load();

    expect(workspace.brief.value).toEqual(payload);
    expect(workspace.error.value).toBe('');
    expect(workspace.research.value).toBeNull();
    expect(workspace.researchError.value).toBe('research audit unavailable');
  });
});
