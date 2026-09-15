// Keep tab dependencies explicit: visiting one page must not hydrate every
// historical laboratory and provider diagnostic in the application.
export const RESEARCH_TAB_PANELS: Record<string, readonly string[]> = {
  overview: ['overview', 'replay-readiness', 'recommendations'],
  'market-snapshots': ['overview', 'market-snapshots', 'sectors', 'sector-flows', 'concept-signals', 'concept-candidates', 'announcements', 'provider-capabilities'],
  'close-review': ['post-close-strategy', 'close-board-report', 'close-strategy-review', 'lhb-events', 'concept-backfill', 'pattern-mining', 'ten-day-leader-rotation', 'intraday-outcomes', 'analyst-scorecards'],
  strategy: ['recommendations', 'universe', 'features', 'strategies'],
  'factor-lab': ['factors', 'factor-evaluations', 'strategy-experiments', 'main-wave-experiments', 'frameworks', 'training-roadmap'],
  'stock-study': ['universe'],
  evidence: ['reports', 'claims', 'remote-messages', 'analyst-skills', 'analyst-research-status', 'analyst-observations', 'analyst-sync-health', 'analyst-market-evaluation', 'analyst-daily-review', 'analyst-weekly-review', 'analyst-review-runs', 'automation-runs', 'analyst-prompt-lab', 'strategy-ablation', 'strategy-governance', 'strategy-health'],
  'claim-review': ['claim-reviews'],
  providers: ['provider-health', 'provider-capabilities', 'catalog', 'paper-status', 'strategy-funnel', 'strategy-governance'],
  catalog: ['catalog'],
  quality: ['quality-issues', 'minute-imports'],
};

export function panelsForResearchTab<T extends { key: string }>(entries: T[], tab: string): T[] {
  const index = new Map(entries.map(entry => [entry.key, entry]));
  return (RESEARCH_TAB_PANELS[tab] ?? RESEARCH_TAB_PANELS.overview!).flatMap(key => {
    const entry = index.get(key);
    return entry ? [entry] : [];
  });
}

// Four active requests leave capacity for a user-opened detail/governance panel.
// Errors settle independently; cancellation prevents queued obsolete tab work.
export async function settlePanelQueue<T extends { key: string; run: () => Promise<unknown> }>(
  entries: T[], onSettled: (entry: T, error?: unknown) => void, signal?: AbortSignal,
): Promise<void> {
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(4, entries.length) }, async () => {
    while (!signal?.aborted) {
      const entry = entries[next++];
      if (!entry) return;
      try {
        await entry.run();
        if (!signal?.aborted) onSettled(entry);
      } catch (error) {
        if (!signal?.aborted) onSettled(entry, error);
      }
    }
  }));
}
