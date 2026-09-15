/** Actual deployed UI + PostgreSQL-backed API; no synthetic successful payload. */
import {createRequire} from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const require=createRequire(path.join(root,'frontend/package.json'));
const {chromium}=require('@playwright/test');
const base=process.env.INTRADAY_BASE_URL??'http://127.0.0.1:5680';
const browser=await chromium.launch({headless:true,channel:process.env.INTRADAY_BROWSER_CHANNEL??'msedge',args:['--no-proxy-server']});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
if(process.env.STOCK_DASHBOARD_CREDENTIALS){
 const credentials=JSON.parse(await fs.readFile(process.env.STOCK_DASHBOARD_CREDENTIALS,'utf8'));
 await context.addCookies([{name:'stockbrain_access',value:credentials.magic_cookie_token,url:base,httpOnly:true,secure:base.startsWith('https:'),sameSite:'Strict'}]);
}
const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
const out=path.join(root,'docs/evidence/intraday');await fs.mkdir(out,{recursive:true});
try{
 const response=await context.request.get(base+'/api/research/intraday-scans');assert.equal(response.status(),200);
 const api=await response.json();assert.equal(api.status,'completed');assert.equal(api.result.lanes.length,9);
 await page.goto(base+'/intraday',{waitUntil:'domcontentloaded'});
 await page.waitForFunction(id=>document.querySelector('.scan-page')?.getAttribute('data-run-id')===id,api.run_id);
 await page.locator('.detail[data-symbol]:not([data-symbol=""])').waitFor();
 await page.locator('.chart canvas').first().waitFor();
 assert.equal(await page.locator('nav button').count(),9);
 await page.getByRole('button',{name:'真实日K',exact:true}).click();
 await page.locator('.chart canvas').first().waitFor();
 await page.screenshot({path:path.join(out,'desktop.png'),fullPage:true});
 await page.locator('nav button').nth(1).click();await page.locator('.detail[data-symbol]:not([data-symbol=""])').waitFor();
 await page.setViewportSize({width:390,height:844});
 await page.waitForFunction(()=>document.documentElement.scrollWidth<=window.innerWidth+2,{},{timeout:5000});
 await page.screenshot({path:path.join(out,'mobile.png'),fullPage:true});
 for(let i=0;i<api.result.lanes.length;i++){
   const lane=api.result.lanes[i];await page.locator('nav button').nth(i).click();
   await page.waitForFunction(label=>document.querySelector('nav button.active')?.textContent?.includes(label),lane.label);
   if(lane.items.length)await page.waitForFunction(code=>document.querySelector('.detail')?.getAttribute('data-symbol')===code,lane.items[0].symbol);
   if(Object.values(lane.data_gaps??{}).some(n=>n>0)){
     await page.getByTestId('coverage-warning').waitFor();
     assert.match(await page.getByTestId('coverage-warning').innerText(),/零匹配不代表全市场没有机会/);
   }
 }
 // Controlled transport failure must be visible, not an empty successful card.
 await page.route('**/api/research/intraday-scans*',route=>route.fulfill({status:503,contentType:'application/json',body:'{"detail":"acceptance_injected_unavailable"}'}));
 await page.getByRole('button',{name:'刷新结果',exact:true}).click();
 await page.getByRole('alert').filter({hasText:'acceptance_injected_unavailable'}).waitFor();
 assert.deepEqual(errors,[]);
 const receipt={base,run_id:api.run_id,lanes:9,all_lane_switches:true,coverage_warning:true,desktop:true,mobile:true,real_charts:true,error_state:true,source:'actual_deployed_api',errors};
 await fs.writeFile(path.join(out,'receipt.json'),JSON.stringify(receipt,null,2));console.log(JSON.stringify(receipt));
}finally{await browser.close();}
