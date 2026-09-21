import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
// Resolve the large module before test timers/mocks are installed. Otherwise
// a cold dynamic import can outlive the first test and consume the next mock.
import { useDashboardWorkspace } from './useDashboardWorkspace';

// jsdom does not implement matchMedia; the dashboard shell uses it to track
// the mobile layout breakpoint outside of any test-relevant behaviour here.
beforeAll(() => {
  if (!window.matchMedia) {
    window.matchMedia = ((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })) as unknown as typeof window.matchMedia;
  }
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const jsonResponse = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status,
  headers: { 'content-type': 'application/json' },
});

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === 'string') return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

describe('useDashboardWorkspace visible-tab loading and stale flags', () => {
  it('loads the formal recommendation through the shared adapter route locally and publicly', async () => {
    const pool = { run_id: 'same-scan', items: [{ symbol: '600000' }] };
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.startsWith('/api/v1/')) return Promise.resolve(jsonResponse({ detail: 'Not Found' }, 404));
      if (url === '/api/research/strategy/post-close/watchlist/latest') {
        return Promise.resolve(jsonResponse({ recommendation_pool: pool }));
      }
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal('fetch', fetchMock);
    const dashboard = useDashboardWorkspace();
    dashboard.activeResearchTab = 'close-review';
    await dashboard.loadResearch();
    expect(dashboard.formalRecommendation).toEqual(pool);
    expect(dashboard.panelStatus['formal-recommendation']!.stale).toBe(false);
    const urls = fetchMock.mock.calls.map(call => requestUrl(call[0]));
    expect(urls).toContain('/api/research/strategy/post-close/watchlist/latest');
    expect(urls.some(url => url.startsWith('/api/v1/'))).toBe(false);
  });

  it('keeps every other panel usable and only flags the failing panel as stale', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.startsWith('/api/research/overview')) {
        return Promise.resolve(jsonResponse({ detail: 'overview backend unavailable' }, 500));
      }
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal('fetch', fetchMock);

    const dashboard = useDashboardWorkspace();
    await dashboard.loadResearch();

    // Failure isolation remains, but unrelated hidden tabs are not requested.
    expect(dashboard.panelStatus.overview!.stale).toBe(true);
    expect(dashboard.panelStatus.overview!.error).toContain('overview backend unavailable');
    expect(dashboard.panelStatus.recommendations!.stale).toBe(false);
    expect(dashboard.panelStatus.recommendations!.error).toBeNull();
    expect(dashboard.panelStatus.recommendations!.updatedAt).toBeTruthy();
    expect(dashboard.panelStatus['strategy-health']).toBeUndefined();
    expect(fetchMock.mock.calls.map(call => requestUrl(call[0]))).toHaveLength(3);

    expect(dashboard.stalePanelKeys).toEqual(['overview']);
    expect(dashboard.researchError).toContain('overview');
    expect(dashboard.researchLoaded).toBe(true);
  });

  it('preserves a panel\'s previous data when a later refresh for that panel alone fails', async () => {
    let overviewCallCount = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.startsWith('/api/research/overview')) {
        overviewCallCount += 1;
        if (overviewCallCount === 1) return Promise.resolve(jsonResponse({ counts: { remote_reports: 3 } }));
        return Promise.resolve(jsonResponse({ detail: 'overview backend unavailable again' }, 500));
      }
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal('fetch', fetchMock);

    const dashboard = useDashboardWorkspace();

    await dashboard.loadResearch();
    expect(dashboard.overview.counts?.remote_reports).toBe(3);
    expect(dashboard.panelStatus.overview!.stale).toBe(false);

    await dashboard.loadResearch();
    // The failing refresh keeps the previously loaded value instead of
    // clearing the panel to an empty/undefined state.
    expect(dashboard.overview.counts?.remote_reports).toBe(3);
    expect(dashboard.panelStatus.overview!.stale).toBe(true);
    expect(dashboard.panelStatus.overview!.error).toContain('overview backend unavailable again');
  });

  it('never rejects loadResearch even when every panel fails', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(jsonResponse({ detail: 'down' }, 500))));

    const dashboard = useDashboardWorkspace();

    await expect(dashboard.loadResearch()).resolves.toBeUndefined();
    expect(dashboard.stalePanelKeys).toHaveLength(3);
    expect(dashboard.researchError).toBeTruthy();
    expect(dashboard.researchLoaded).toBe(true);
  });
});

class FakeEventSource {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {}
  addEventListener(): void {}
  close(): void { this.closed = true; }
}

describe('useDashboardWorkspace unmount cleanup', () => {
  it('aborts an in-flight manual-relay XHR upload when the shell unmounts', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(jsonResponse({}))));
    vi.stubGlobal('EventSource', FakeEventSource);

    const { mount } = await import('@vue/test-utils');
    const { defineComponent, h } = await import('vue');

    let dashboard!: ReturnType<typeof useDashboardWorkspace>;
    const Host = defineComponent({
      setup() {
        dashboard = useDashboardWorkspace();
        return () => h('div');
      },
    });

    const wrapper = mount(Host);
    const abort = vi.fn();
    // submitRelay() owns creating this XHR; a fake stand-in is enough to
    // prove the shell aborts whatever upload is in flight on unmount.
    dashboard.relayXhr = { abort } as unknown as XMLHttpRequest;

    wrapper.unmount();

    expect(abort).toHaveBeenCalledTimes(1);
  });
});
