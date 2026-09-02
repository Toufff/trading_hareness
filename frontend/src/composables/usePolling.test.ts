import { afterEach, describe, expect, it, vi } from 'vitest';

import { usePolling } from './usePolling';

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true });
}

describe('usePolling', () => {
  afterEach(() => {
    setVisibility('visible');
  });

  it('owns and stops all registered timers', () => {
    vi.useFakeTimers();
    const polling = usePolling();
    const callback = vi.fn();

    polling.every(1_000, callback);
    vi.advanceTimersByTime(3_000);
    expect(callback).toHaveBeenCalledTimes(4);

    polling.stop();
    vi.advanceTimersByTime(3_000);
    expect(callback).toHaveBeenCalledTimes(4);
    vi.useRealTimers();
  });

  it('pauses polling while the tab is hidden and resumes once visible again', () => {
    vi.useFakeTimers();
    const polling = usePolling();
    const callback = vi.fn();
    setVisibility('hidden');

    polling.every(1_000, callback);
    vi.advanceTimersByTime(3_000);
    expect(callback).not.toHaveBeenCalled();

    setVisibility('visible');
    vi.advanceTimersByTime(1_000);
    expect(callback).toHaveBeenCalledTimes(1);

    polling.stop();
    vi.useRealTimers();
  });

  it('skips a tick while the previous async task is still in flight', async () => {
    vi.useFakeTimers();
    const polling = usePolling();
    let resolveFirst: (() => void) | undefined;
    const task = vi.fn(() => new Promise<void>((resolve) => { resolveFirst = resolve; }));

    polling.every(1_000, task);
    expect(task).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(3_000);
    // The first call never resolved, so no overlapping call should have started.
    expect(task).toHaveBeenCalledTimes(1);

    resolveFirst?.();
    // Let the resolved promise's `.finally` microtask clear the in-flight flag.
    await Promise.resolve();
    await Promise.resolve();

    vi.advanceTimersByTime(1_000);
    expect(task).toHaveBeenCalledTimes(2);

    polling.stop();
    vi.useRealTimers();
  });
});
