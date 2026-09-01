export const DASHBOARD_SECTIONS = ['research', 'personal', 'monitor', 'workbench', 'relay'] as const;

export type DashboardSection = (typeof DASHBOARD_SECTIONS)[number];

const routeSections: Record<string, DashboardSection> = {
  '/research': 'research',
  '/personal': 'personal',
  '/monitor': 'monitor',
  '/workbench': 'workbench',
  '/relay': 'relay',
};

export function isDashboardSection(value: string | null): value is DashboardSection {
  return value !== null && DASHBOARD_SECTIONS.includes(value as DashboardSection);
}

export function resolveInitialDashboardSection(pathname: string, persisted: string | null): DashboardSection {
  const routed = routeSections[pathname.replace(/\/$/, '') || '/'];
  if (routed) return routed;
  if (isDashboardSection(persisted)) return persisted;
  // The migrated product is decision-first.  The very large research console is
  // loaded only when the user asks for it, so it cannot stall the action page.
  return 'personal';
}
