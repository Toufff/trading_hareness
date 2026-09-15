import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';

test.use({ channel: 'msedge', headless: true });

test('published nine reports lead with saved results on desktop and mobile', async ({ page, context }) => {
  test.skip(process.env.RUN_REPORT_RESULTS_LIVE !== '1', 'Explicit read-only production acceptance');
  test.setTimeout(120_000);
  const credentials = JSON.parse(readFileSync('C:/Users/brave/.stockbrain/dashboard-credentials.json', 'utf8'));
  await context.addCookies([{ name: 'stockbrain_access', value: credentials.magic_cookie_token,
    domain: 'stock.toufai.top', path: '/', secure: true, httpOnly: true }]);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('https://stock.toufai.top/research');
  await page.getByRole('tab', { name: '收盘复盘', exact: true }).click();
  await expect(page.locator('[data-report-key]')).toHaveCount(10, { timeout: 45_000 });
  const keys = await page.locator('[data-report-key]').evaluateAll(els => els.map(el => el.getAttribute('data-report-key')!));
  for (const key of keys) {
    await page.locator(`[data-report-key="${key}"]`).click();
    const body = page.locator(`[data-report-view="${key}"]`);
    await expect(body.locator('h3').first()).toHaveText('本次结论');
    if (key === 'overview') continue;
    const summary = body.locator(`[data-result-summary="${key}"]`);
    await expect(summary).toBeVisible();
    const text = await body.innerText();
    expect(text.indexOf('本次结论')).toBeLessThan(text.indexOf('数据与筛选说明'));
    if (key === 'accumulation') {
      await expect(summary).toContainText('恺英网络（002517）');
      await expect(summary).toContainText('确认条件');
      await summary.scrollIntoViewIfNeeded();
      await page.screenshot({ path: 'G:/StockPlatform/reports/short-term/results-first-desktop.png' });
      await page.setViewportSize({ width: 390, height: 844 });
      const geometry = await summary.evaluate(el => ({ width: el.clientWidth, scroll: el.scrollWidth }));
      expect(geometry.scroll).toBeLessThanOrEqual(geometry.width + 1);
      await summary.scrollIntoViewIfNeeded();
      await page.screenshot({ path: 'G:/StockPlatform/reports/short-term/results-first-mobile.png' });
      await page.setViewportSize({ width: 1440, height: 1000 });
    }
  }
  expect(errors).toEqual([]);
});
