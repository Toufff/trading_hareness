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
  it('loads market, formal recommendation, holdings and strategy watchlist from independent endpoints', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({
        status: 'ready', as_of_at: '2026-09-01T15:15:00+08:00',
        content: { market_state: 'rotation' }, delivery: { eligible: true, complete: true },
      }))
      .mockResolvedValueOnce(jsonResponse({
        status: 'blocked', as_of_at: '2026-09-01T15:15:00+08:00', actions: [],
        freshness_status: 'stale_or_unverified', delivery: { eligible: false },
      }))
      .mockResolvedValueOnce(jsonResponse({
        as_of_date: '2026-09-01', status: 'completed', research_only: true,
        depends_on_holdings: false, total_unique: 1,
        recommendation_pool: { status: 'ready', recommended: [{ symbol: '600000.SH' }] },
        items: [{ symbol: '600001.SH', name: '扫描股票', lane_keys: ['trend'], lane_labels: ['主线趋势'], review_status: 'retain_watch', review_label: '公司复核后保留观察', buy_authorized: false, depends_on_holdings: false }],
      }));
    vi.stubGlobal('fetch', fetchMock);

    const workspace = usePersonalDecisionWorkspace();
    await workspace.load();

    expect(workspace.brief.value?.holdings.status).toBe('blocked');
    expect(workspace.brief.value?.new_buys.actions).toHaveLength(0);
    expect(workspace.formalRecommendation.value?.status).toBe('ready');
    expect(workspace.scanWatchlist.value?.items).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/research/advice/market/latest',
      expect.any(Object),
    );
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('post-close/latest'))).toBe(false);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/research/personal/holding-advice/latest?account_key=citics-primary',
      expect.any(Object),
    );
  });

  it('keeps stock and market surfaces isolated when broker transport fails', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'ready', as_of_at: '2026-09-01T12:00:00+08:00', content: {}, delivery: { eligible: true, complete: true } }))
      .mockResolvedValueOnce(new Response(
        JSON.stringify({ detail: 'portfolio unavailable' }),
        { status: 503, headers: { 'content-type': 'application/json' } },
      ))
      .mockResolvedValueOnce(jsonResponse({ as_of_date: '2026-09-01', status: 'completed', research_only: true, depends_on_holdings: false, total_unique: 1, recommendation_pool: { status: 'ready', recommended: [] }, items: [{ symbol: '600001.SH' }] }));
    vi.stubGlobal('fetch', fetchMock);
    const workspace = usePersonalDecisionWorkspace();

    await workspace.load();

    expect(workspace.brief.value?.market.status).toBe('ready');
    expect(workspace.brief.value?.new_buys.actions).toHaveLength(0);
    expect(workspace.brief.value?.holdings.actions).toEqual([]);
    expect(workspace.holdingError.value).toBe('portfolio unavailable');
    expect(workspace.scanWatchlist.value?.items).toHaveLength(1);
    expect(workspace.error.value).toBe('');
  });

  it('keeps market visible when the combined scan projection fails', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'ready', as_of_at: '2026-09-01T15:15:00+08:00', content: { market_state: 'rotation' }, delivery: { eligible: true, complete: true } }))
      .mockResolvedValueOnce(jsonResponse({ status: 'ready', as_of_at: '2026-09-01T15:15:00+08:00', actions: [], freshness_status: 'current', delivery: { eligible: true } }))
      .mockResolvedValueOnce(new Response(
        JSON.stringify({ detail: 'research audit unavailable' }),
        { status: 503, headers: { 'content-type': 'application/json' } },
      ));
    vi.stubGlobal('fetch', fetchMock);

    const workspace = usePersonalDecisionWorkspace();
    await workspace.load();

    expect(workspace.brief.value?.market.status).toBe('ready');
    expect(workspace.error.value).toBe('');
    expect(workspace.formalRecommendation.value).toBeNull();
    expect(workspace.scanWatchlist.value).toBeNull();
  });

  it('does not touch the broker endpoint on the market and stock surface', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ status: 'ready', as_of_at: '2026-09-04T15:15:00+08:00', content: {}, delivery: { eligible: true, complete: true } }))
      .mockResolvedValueOnce(jsonResponse({ as_of_date: '2026-09-04', status: 'completed', research_only: true, depends_on_holdings: false, total_unique: 0, recommendation_pool: { status: 'ready', recommended: [] }, items: [] }));
    vi.stubGlobal('fetch', fetchMock);

    const workspace = usePersonalDecisionWorkspace('market');
    await workspace.load();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('holding-advice'))).toBe(false);
  });

  it('loads only the broker endpoint on the holdings surface', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({
      status: 'ready', as_of_at: '2026-09-04T15:15:00+08:00', portfolio_observed_at: '2026-09-04T15:15:00+08:00',
      freshness_status: 'current', actions: [], delivery: { eligible: true },
    }));
    vi.stubGlobal('fetch', fetchMock);

    const workspace = usePersonalDecisionWorkspace('holdings');
    await workspace.load();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toContain('/api/research/personal/holding-advice/latest');
    expect(workspace.brief.value?.delivery.market_eligible).toBe(false);
    expect(workspace.brief.value?.delivery.holding_actions_eligible).toBe(true);
  });
});
