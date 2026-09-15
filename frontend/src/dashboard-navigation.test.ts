import { describe, expect, it } from 'vitest';
import { dashboardSectionPath, isDashboardSection, RESEARCH_SMOKE_ROUTE, resolveInitialDashboardSection } from './dashboard-navigation';

describe('dashboard navigation', () => {
  it('honours explicit deep links before persisted navigation', () => {
    expect(resolveInitialDashboardSection('/research', 'holdings')).toBe('research');
    expect(resolveInitialDashboardSection('/personal/', 'research')).toBe('market-decision');
    expect(resolveInitialDashboardSection('/holdings', 'research')).toBe('holdings');
  });

  it('defaults to the light decision workspace instead of eagerly loading research', () => {
    expect(resolveInitialDashboardSection('/', null)).toBe('market-decision');
    expect(resolveInitialDashboardSection('/unknown', 'invalid')).toBe('market-decision');
  });

  it('accepts only known persisted sections', () => {
    expect(isDashboardSection('monitor')).toBe(true);
    expect(isDashboardSection('admin')).toBe(false);
  });

  it('gives the two decision surfaces distinct URLs', () => {
    expect(dashboardSectionPath('market-decision')).toBe('/market');
    expect(dashboardSectionPath('holdings')).toBe('/holdings');
  });

  it('keeps RESEARCH_SMOKE_ROUTE (what e2e/smoke.spec.ts navigates to) resolving to the research console', () => {
    // e2e/smoke.spec.ts expects the "量化研究台" heading, which only the
    // research section renders. Since the default route ('/') is now the
    // personal decision workspace, the spec imports RESEARCH_SMOKE_ROUTE
    // instead of hardcoding its own path; this test is what catches the two
    // sides drifting apart, instead of a flaky Playwright run doing it.
    expect(resolveInitialDashboardSection(RESEARCH_SMOKE_ROUTE, null)).toBe('research');
  });
});
