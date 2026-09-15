import { test, expect } from '@playwright/test';
import { readFileSync, writeFileSync } from 'node:fs';

test.use({channel:'msedge', headless:true});
test('restricted leaders survive in the public report and watchlist', async ({page, context}) => {
  test.skip(process.env.RUN_DISCOVERY_LIVE !== '1', 'Explicit live read-only acceptance');
  test.setTimeout(90_000);
  const credentials = JSON.parse(readFileSync('C:/Users/brave/.stockbrain/dashboard-credentials.json','utf8'));
  await context.addCookies([{name:'stockbrain_access', value:credentials.magic_cookie_token,
    domain:'stock.toufai.top',path:'/',secure:true,httpOnly:true}]);
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.setViewportSize({width:1440,height:1000});
  await page.goto('https://stock.toufai.top/research');
  await page.getByRole('tab',{name:'收盘复盘',exact:true}).click();
  await expect(page.locator('[data-report-key]')).toHaveCount(10,{timeout:45_000});
  await page.locator('[data-report-key="relay"]').click();
  const discovery = page.locator('[data-testid="independent-discovery"]');
  await expect(discovery).toContainText('风华高科');
  await expect(discovery).toContainText('不证明封板或可成交');
  await expect(discovery).toContainText('暂不改变正式排名');
  await discovery.scrollIntoViewIfNeeded();
  await page.screenshot({path:'G:/StockPlatform/reports/short-term/discovery-split-desktop.png'});
  await page.setViewportSize({width:390,height:844});
  const dimensions = await discovery.evaluate(el => ({width:el.clientWidth,scroll:el.scrollWidth}));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width+1);
  await page.screenshot({path:'G:/StockPlatform/reports/short-term/discovery-split-mobile.png'});
  const response = await context.request.get('https://stock.toufai.top/api/v1/strategy/post-close/watchlist/latest');
  expect(response.ok()).toBeTruthy();
  const watchlist = await response.json();
  const row = watchlist.items.find((r:{symbol:string}) => r.symbol==='000636.SZ');
  expect(row).toBeTruthy();
  expect(row.buy_authorized).toBe(false);
  expect(row.execution.market_route).toBe('disabled');
  expect(errors).toEqual([]);
  writeFileSync('G:/StockPlatform/reports/short-term/discovery-split-browser-receipt.json',
    JSON.stringify({passed:true,at:new Date().toISOString(),dimensions,pageErrors:errors,
      scope:'public strategy report, read-only watchlist and mobile layout; not profitability'},null,2));
});
