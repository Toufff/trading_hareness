import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';

// Opt-in authenticated public verification; credentials never enter artifacts.
test.beforeEach(async ({ context, baseURL }) => {
  const file = process.env.GUANSHI_CREDENTIALS_FILE;
  if (file && new URL(baseURL!).hostname === 'stock.toufai.top') {
    const credentials = JSON.parse(readFileSync(file, 'utf8'));
    await context.addCookies([{ name: 'stockbrain_access', value: credentials.magic_cookie_token,
      domain: 'stock.toufai.top', path: '/', secure: true, httpOnly: true }]);
  }
});

// Read-only, actual backend data. Never clicks generation, save, trading or send actions.
const routes = ['/market', '/holdings', '/research', '/sector-heat', '/intraday', '/agent-paper', '/monitor', '/workbench', '/relay'];
for (const route of routes) {
  test(`观市 shell and responsive layout ${route}`, async ({ page }, testInfo) => {
    const errors: string[] = [];
    page.on('pageerror', err => errors.push(err.message));
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto(route);
    await expect(page.getByRole('link', { name: '观市 首页' })).toBeVisible();
    await expect(page.locator('.workspace-header')).toHaveCSS('background-color', 'rgb(45, 98, 124)');
    await expect(page.locator('body')).toHaveCSS('background-color', 'rgb(234, 229, 216)');
    await expect(page.locator('#startup-status')).toHaveCount(0);
    await expect(page.locator('main')).toBeVisible();
    // Give late-arriving real data a bounded opportunity to expose layout changes.
    await page.waitForTimeout(2500);
    if (route === '/market') {
      await expect(page.locator('.recommendation-decision .picks article').first()).toBeVisible();
      await expect(page.locator('.recommendation-decision').first()).toHaveCSS('background-color', 'rgb(250, 247, 239)');
    }
    if (route === '/agent-paper') {
      await expect(page.locator('.agent-page')).toHaveCSS('padding-left', '16px');
      await expect(page.locator('.metrics')).toHaveCSS('display', 'grid');
    }
    if (route === '/intraday') await expect(page.locator('.scan-page')).toHaveCSS('max-width', '1500px');
    if (route === '/holdings') await expect(page.locator('.discipline-board')).toBeVisible();
    if (route === '/sector-heat') await expect(page.locator('.ranking-row').first()).toBeVisible({ timeout: 15000 });
    await page.screenshot({ path: testInfo.outputPath('desktop.png') });
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      await expect(page.getByRole('navigation', { name: '主导航', exact: true })).toBeVisible();
      for (const text of ['选股', '持仓', '盘中', '板块', '研究', '模拟盘']) {
        await expect(page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('link', { name: text, exact: true })).toBeVisible();
      }
      await page.locator('.workspace-tools summary').click();
      await expect(page.getByRole('link', { name: '飞书工作台', exact: true })).toBeVisible();
      await page.locator('.workspace-tools summary').click();
      const dimensions = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }));
      expect(dimensions.scroll, JSON.stringify(dimensions)).toBeLessThanOrEqual(dimensions.width + 1);
      await page.screenshot({ path: testInfo.outputPath(`mobile-${width}.png`) });
    }
    expect(errors).toEqual([]);
  });
}

test('market/holdings navigation preserves browser back and view separation', async ({ page }) => {
  await page.goto('/market');
  const nav = page.getByRole('navigation', { name: '主导航', exact: true });
  await nav.getByRole('link', { name: '持仓', exact: true }).click();
  await expect(page).toHaveURL(/\/holdings$/);
  await expect(page.getByRole('heading', { name: '我的持仓', exact: true })).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/\/market$/);
  await expect(page.getByRole('heading', { name: '市场与选股', exact: true })).toBeVisible();
});

test('real K-line drawer supports strategy changes and mobile layout', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/market');
  await page.getByPlaceholder('如 600487.SH').fill('603650.SH');
  await page.getByRole('button', { name: '打开', exact: true }).click();
  const workbench = page.locator('.workbench-shell');
  await expect(workbench.getByRole('heading', { name: /彤程新材/, level: 2 })).toBeVisible({ timeout: 20000 });
  await expect(workbench.locator('.price-chart canvas')).toBeVisible();
  await expect(workbench).toHaveCSS('background-color', 'rgb(234, 229, 216)');
  for (const button of await workbench.locator('.strategy-tabs button').all()) {
    await button.click();
    await expect(button).toHaveClass(/active/);
    await expect(workbench.locator('.price-chart canvas')).toBeVisible();
  }
  await workbench.getByRole('button', { name: '周线', exact: true }).click();
  await expect(workbench.getByRole('button', { name: '周线', exact: true })).toHaveClass(/active/);
  await page.screenshot({ path: testInfo.outputPath('chart-desktop.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(workbench.locator('.price-chart canvas')).toBeVisible();
  await page.waitForTimeout(500);
  const box = await workbench.boundingBox();
  expect(box!.x, JSON.stringify(box)).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(391);
  await page.screenshot({ path: testInfo.outputPath('chart-mobile.png') });
  await workbench.locator('.price-chart').scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('chart-mobile-candles.png') });
});

test('all research tabs remain selectable, with read-only live data loading', async ({ page }, testInfo) => {
  await page.goto('/research');
  const errors: string[] = [];
  page.on('pageerror', err => errors.push(err.message));
  await expect(page.getByRole('tab')).toHaveCount(11);
  for (const tab of await page.getByRole('tab').all()) {
    await tab.click();
    await expect(tab).toHaveAttribute('aria-selected', 'true');
    await page.waitForTimeout(350);
  }
  await page.getByRole('tab', { name: '收盘复盘', exact: true }).click();
  await expect(page.getByText('短线交易七步复盘')).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(1500);
  const width = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(width).toBeLessThanOrEqual(391);
  await page.screenshot({ path: testInfo.outputPath('research-mobile.png') });
  expect(errors).toEqual([]);
});
