import { describe, expect, it, vi } from 'vitest';

import { usePolling } from './usePolling';

describe('usePolling', () => {
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
});
