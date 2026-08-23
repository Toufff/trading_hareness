/** Lifecycle-owned dashboard polling registry.
 *
 * Keeping timer ownership outside individual dashboard sections prevents a
 * new page from accidentally leaving a duplicate interval alive after a Vue
 * remount.  Feature modules register work; the shell starts and stops it.
 */
export function usePolling() {
  const timers = new Set<number>();

  function every(intervalMs: number, task: () => void): void {
    task();
    timers.add(window.setInterval(task, intervalMs));
  }

  function stop(): void {
    for (const timer of timers) window.clearInterval(timer);
    timers.clear();
  }

  return { every, stop };
}
