import { describe, expect, it } from 'vitest';
import { isDashboardSection, resolveInitialDashboardSection } from './dashboard-navigation';

describe('dashboard navigation', () => {
  it('honours explicit deep links before persisted navigation', () => {
    expect(resolveInitialDashboardSection('/research', 'personal')).toBe('research');
    expect(resolveInitialDashboardSection('/personal/', 'research')).toBe('personal');
  });

  it('defaults to the light decision workspace instead of eagerly loading research', () => {
    expect(resolveInitialDashboardSection('/', null)).toBe('personal');
    expect(resolveInitialDashboardSection('/unknown', 'invalid')).toBe('personal');
  });

  it('accepts only known persisted sections', () => {
    expect(isDashboardSection('monitor')).toBe(true);
    expect(isDashboardSection('admin')).toBe(false);
  });
});
