import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';

test.use({ channel: 'msedge', headless: true });
test('published event evidence is readable on desktop/mobile and shared by all reports', async ({page,context})=>{
  test.skip(process.env.RUN_EVENT_RESEARCH_LIVE!=='1','Explicit read-only production acceptance');
  test.setTimeout(120_000);
  const credentials=JSON.parse(readFileSync('C:/Users/brave/.stockbrain/dashboard-credentials.json','utf8'));
  await context.addCookies([{name:'stockbrain_access',value:credentials.magic_cookie_token,
    domain:'stock.toufai.top',path:'/',secure:true,httpOnly:true}]);
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  await page.setViewportSize({width:1440,height:1000});
  await page.goto('https://stock.toufai.top/');
  const panel=page.getByTestId('event-research');
  await expect(panel).toBeVisible({timeout:45_000});
  const response=await page.request.get('https://stock.toufai.top/api/research/strategy/events/latest');
  expect(response.ok()).toBeTruthy();
  const latest=await response.json();
  expect(latest.events.length).toBeGreaterThan(0);
  await expect(panel.locator('article')).toHaveCount(latest.events.length);
  await expect(panel).toContainText(latest.run_id.slice(0,8));
  await expect(panel).toContainText('预期与时间');
  await expect(panel).toContainText('反证与失效');
  await expect(panel).toContainText(latest.summary);
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({path:'G:/StockPlatform/reports/events/public-desktop.png'});
  await page.setViewportSize({width:390,height:844});
  const geometry=await panel.evaluate(el=>({width:el.clientWidth,scroll:el.scrollWidth}));
  expect(geometry.scroll).toBeLessThanOrEqual(geometry.width+1);
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({path:'G:/StockPlatform/reports/events/public-mobile.png'});
  await page.setViewportSize({width:1440,height:1000});
  await page.goto('https://stock.toufai.top/research');
  await page.getByRole('tab',{name:'收盘复盘',exact:true}).click();
  await expect(page.locator('[data-report-key]')).toHaveCount(10,{timeout:45_000});
  const keys=await page.locator('[data-report-key]').evaluateAll(els=>els.map(el=>el.getAttribute('data-report-key')!));
  for(const key of keys){
    await page.locator(`[data-report-key="${key}"]`).click();
    const body=page.locator(`[data-report-view="${key}"]`);
    await expect(body.getByTestId('event-research')).toBeVisible();
    await expect(body.getByTestId('event-research')).toContainText('消息截止');
  }
  expect(errors).toEqual([]);
});
