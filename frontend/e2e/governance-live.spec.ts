import { test, expect } from '@playwright/test';
import { readFileSync, writeFileSync } from 'node:fs';

test.use({ channel: 'msedge', headless: true });

test('published governance and daily price-volume evidence, no mutation', async ({ page, context }) => {
  test.skip(process.env.RUN_GOVERNANCE_UI_LIVE !== '1', 'Explicit production read-only acceptance only');
  test.setTimeout(90_000);
  const credentials = JSON.parse(readFileSync('C:/Users/brave/.stockbrain/dashboard-credentials.json', 'utf8'));
  await context.addCookies([{ name: 'stockbrain_access', value: credentials.magic_cookie_token,
    domain: 'stock.toufai.top', path: '/', secure: true, httpOnly: true }]);
  const errors: string[] = [];
  const started = Date.now();
  const timings: { path: string; status: number; atMs: number }[] = [];
  page.on('response', response => {
    const path = new URL(response.url()).pathname;
    if (path.includes('strategy/governance') || path.includes('strategy/short-term')) {
      timings.push({ path, status: response.status(), atMs: Date.now() - started });
    }
  });
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('https://stock.toufai.top/research');
  await page.getByRole('tab', { name: '收盘复盘', exact: true }).click();
  await page.getByRole('button', { name: '查看改进与待落地队列' }).click();
  const governance = page.getByRole('region', { name: '策略治理与实验验收' });
  await expect(governance.locator('.issue').first()).toBeVisible({ timeout: 30_000 });
  expect(await governance.locator('.issue').count()).toBeGreaterThanOrEqual(8);
  await expect(governance.getByText('此页只读', { exact: false })).toBeVisible();
  await expect(governance.locator('.error')).toHaveCount(0);
  await page.setViewportSize({ width: 1440, height: 1100 });
  await governance.screenshot({ path: 'G:/StockPlatform/data/research/2026-09-11-governance-pv-acceptance/governance-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  const geometry = await governance.evaluate(el => ({ width: el.clientWidth, scroll: el.scrollWidth }));
  expect(geometry.width).toBeGreaterThanOrEqual(320);
  expect(geometry.scroll).toBeLessThanOrEqual(geometry.width + 1);
  await governance.screenshot({ path: 'G:/StockPlatform/data/research/2026-09-11-governance-pv-acceptance/governance-mobile.png' });
  await page.getByRole('button', { name: '收起审查' }).click();
  await page.setViewportSize({ width: 1440, height: 1100 });
  await expect(page.locator('[data-report-key="accumulation"]')).toBeVisible({ timeout: 45_000 });
  await page.locator('[data-report-key="accumulation"]').click();
  const cautions = page.locator('[data-strategy-detail="accumulation"] > details');
  if (await cautions.count()) await cautions.locator('summary').click();
  const pv = page.getByRole('region', { name: '量价证据与边界' });
  await expect(pv.first()).toBeVisible();
  await pv.first().screenshot({ path: 'G:/StockPlatform/data/research/2026-09-11-governance-pv-acceptance/price-volume-desktop.png' });
  expect(errors).toEqual([]);
  await test.info().attach('public-request-timing', { body: JSON.stringify(timings), contentType: 'application/json' });
  writeFileSync('G:/StockPlatform/data/research/2026-09-11-governance-pv-acceptance/public-browser-receipt.json',
    JSON.stringify({ passed: true, at: new Date().toISOString(), timings, mobileGeometry: geometry,
      pageErrors: errors, scope: '公网治理队列、手机布局、策略量价证据只读验收；不代表盈利验证或其他页面全部正常' }, null, 2));
});
