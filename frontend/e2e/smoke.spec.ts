import { expect, test } from '@playwright/test';
import { RESEARCH_SMOKE_ROUTE } from '../src/dashboard-navigation';

// The dashboard's default landing route is the light decision workspace (see
// resolveInitialDashboardSection in src/dashboard-navigation.ts); the research
// console is a deep link, not the '/' default. RESEARCH_SMOKE_ROUTE is the
// single source of truth for that deep link -- see dashboard-navigation.test.ts
// for the assertion that keeps it resolving to the research section.
test('research shell renders and does not expose an HTML-as-JSON error', async ({ page }) => {
  await page.goto(RESEARCH_SMOKE_ROUTE);
  await expect(page.getByRole('heading', { name: '量化研究台' })).toBeVisible();
  await expect(page.getByText(/Unexpected token '<'/)).toHaveCount(0);
});

test('research tabs lazy-load their independently mounted view', async ({ page }) => {
  await page.goto(RESEARCH_SMOKE_ROUTE);
  await page.getByRole('tab', { name: '收盘复盘' }).click();
  await expect(page.getByText('短线交易七步复盘')).toBeVisible();

  await page.getByRole('tab', { name: '数据源' }).click();
  await expect(page.getByText('盘中实时链路与日终摘要')).toBeVisible();
});
