import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
test.use({channel:'msedge',headless:true});
test('public page and local evidence expose the identical recommendation decision', async ({page,context}) => {
  test.skip(process.env.RUN_RECOMMENDATION_LIVE !== '1', 'Explicit live read-only verification');
  test.setTimeout(120000);
  const bundle = JSON.parse(readFileSync(process.env.RECOMMENDATION_DECISION_FILE!, 'utf8'));
  const credentials = JSON.parse(readFileSync('C:/Users/brave/.stockbrain/dashboard-credentials.json','utf8'));
  await context.addCookies([{name:'stockbrain_access',value:credentials.magic_cookie_token,domain:'stock.toufai.top',path:'/',secure:true,httpOnly:true}]);
  const errors:string[]=[];
  page.on('pageerror', e=>errors.push(e.message));
  await page.goto('https://stock.toufai.top/research');
  await page.getByRole('tab',{name:'收盘复盘',exact:true}).click();
  const panel=page.locator('[data-decision-id="'+bundle.decision_id+'"]');
  await expect(panel).toBeVisible({timeout:60000});
  for(const p of bundle.recommended) {
    await expect(panel).toContainText(p.name);
    await expect(panel).toContainText(p.trigger);
  }
  const publicBundle=await page.evaluate(async()=> (await (await fetch('/api/v1/strategy/post-close/watchlist/latest')).json()).recommendation_pool);
  expect(publicBundle).toEqual(bundle);
  await page.setViewportSize({width:1440,height:1000});
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({path:'G:/StockPlatform/reports/recommendation/desktop.png'});
  await page.setViewportSize({width:390,height:844});
  await panel.scrollIntoViewIfNeeded();
  const size=await panel.evaluate(el=>({width:el.clientWidth,scroll:el.scrollWidth}));
  expect(size.scroll).toBeLessThanOrEqual(size.width+1);
  await page.screenshot({path:'G:/StockPlatform/reports/recommendation/mobile.png'});
  expect(errors).toEqual([]);
});
