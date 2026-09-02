import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

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

describe('useDashboardWorkspace research panel loading (allSettled + stale flags)', () => {
  it('keeps every other panel usable and only flags the failing panel as stale', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.startsWith('/api/research/overview')) {
        return Promise.resolve(jsonResponse({ detail: 'overview backend unavailable' }, 500));
      }
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal('fetch', fetchMock);

    const { useDashboardWorkspace } = await import('./useDashboardWorkspace');
    const dashboard = useDashboardWorkspace();
    await dashboard.loadResearch();

    // A single failing endpoint must not stop the ~50 other independent
    // panels sharing this refresh cycle from loading and being marked fresh.
    expect(dashboard.panelStatus.overview!.stale).toBe(true);
    expect(dashboard.panelStatus.overview!.error).toContain('overview backend unavailable');
    expect(dashboard.panelStatus.reports!.stale).toBe(false);
    expect(dashboard.panelStatus.reports!.error).toBeNull();
    expect(dashboard.panelStatus.reports!.updatedAt).toBeTruthy();
    expect(dashboard.panelStatus['strategy-health']!.stale).toBe(false);

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

    const { useDashboardWorkspace } = await import('./useDashboardWorkspace');
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

    const { useDashboardWorkspace } = await import('./useDashboardWorkspace');
    const dashboard = useDashboardWorkspace();

    await expect(dashboard.loadResearch()).resolves.toBeUndefined();
    expect(dashboard.stalePanelKeys.length).toBeGreaterThan(40);
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
    const { useDashboardWorkspace } = await import('./useDashboardWorkspace');

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
