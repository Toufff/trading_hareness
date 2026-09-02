/** Lifecycle-owned dashboard polling registry.
 *
 * Keeping timer ownership outside individual dashboard sections prevents a
 * new page from accidentally leaving a duplicate interval alive after a Vue
 * remount.  Feature modules register work; the shell starts and stops it.
 *
 * Two safety rules apply to every registered task:
 *  - it is skipped entirely while the tab is hidden (`document.visibilityState
 *    === 'hidden'`); the next tick after the tab becomes visible again runs
 *    it as usual, so polling is paused rather than buffered while backgrounded;
 *  - a task is only tracked as "in flight" when it returns a promise; while
 *    that promise is pending, later ticks are skipped instead of starting an
 *    overlapping request against the backend.
 */
export function usePolling() {
  const timers = new Set<number>();

  function every(intervalMs: number, task: () => void | Promise<unknown>): void {
    let running = false;
    const tick = () => {
      if (document.visibilityState === 'hidden') return;
      if (running) return;
      const result = task();
      if (result && typeof (result as Promise<unknown>).then === 'function') {
        running = true;
        (result as Promise<unknown>).finally(() => { running = false; }).catch(() => {});
      }
    };
    tick();
    timers.add(window.setInterval(tick, intervalMs));
  }

  function stop(): void {
    for (const timer of timers) window.clearInterval(timer);
    timers.clear();
  }

  return { every, stop };
}
