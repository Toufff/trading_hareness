import { test, expect } from '@playwright/test';
import { readFileSync, writeFileSync } from 'node:fs';

test.use({channel:'msedge',headless:true});
test('records published research request timing and mobile shell width',async({page,context})=>{
  test.skip(process.env.RUN_RESEARCH_LOADING_LIVE!=='1','Read-only public performance diagnostic');
  test.setTimeout(120_000);
  const credential=JSON.parse(readFileSync('C:/Users/brave/.stockbrain/dashboard-credentials.json','utf8'));
  await context.addCookies([{name:'stockbrain_access',value:credential.magic_cookie_token,domain:'stock.toufai.top',path:'/',secure:true,httpOnly:true}]);
  const started=Date.now();const rows:Record<string,unknown>[]=[];
  page.on('requestfinished',async request=>{
    if(!new URL(request.url()).pathname.startsWith('/api/'))return;
    rows.push({path:new URL(request.url()).pathname,start_ms:request.timing().startTime-started,
      timing:request.timing(),status:(await request.response())?.status()});
  });
  await page.goto('https://stock.toufai.top/research');
  await page.getByRole('tab',{name:'收盘复盘',exact:true}).click();
  const requested=Date.now();
  await page.getByRole('button',{name:'查看改进与待落地队列'}).click();
  const governance=page.getByRole('region',{name:'策略治理与实验验收'});
  await expect(governance.locator('.issue').first()).toBeVisible({timeout:90_000});
  const rendered=Date.now();
  await page.setViewportSize({width:390,height:844});
  const geometry=await governance.evaluate(el=>({width:el.clientWidth,scroll:el.scrollWidth}));
  writeFileSync(`G:/StockPlatform/data/research/2026-09-11-governance-pv-acceptance/research-waterfall-${process.env.PERF_PHASE??'current'}.json`,JSON.stringify({elapsed_ms:rendered-started,governance_ms:rendered-requested,geometry,requests:rows},null,2));
  expect(geometry.width).toBeGreaterThanOrEqual(320);
});
