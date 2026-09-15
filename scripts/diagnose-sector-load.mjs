import fs from 'node:fs/promises';
import { createRequire } from 'node:module';
import assert from 'node:assert/strict';
const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const { chromium } = require('@playwright/test');
const credentials = JSON.parse(await fs.readFile(process.env.STOCK_DASHBOARD_CREDENTIALS, 'utf8'));
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const context = await browser.newContext();
if (process.env.SECTOR_ANONYMOUS !== '1') await context.addCookies([{ name: 'stockbrain_access', value: credentials.magic_cookie_token, url: 'https://stock.toufai.top', httpOnly: true, secure: true, sameSite: 'Strict' }]);
const page = await context.newPage();
if (process.env.SECTOR_BLOCK_CHART === '1') await page.route('**/assets/installCanvasRenderer-*.js', route => route.abort());
const start = Date.now();
const requests = new Map();
const errors = [];
page.on('request', r => requests.set(r, { path: new URL(r.url()).pathname, start: Date.now() - start, type: r.resourceType() }));
page.on('response', r => { const entry = requests.get(r.request()); if (entry) entry.status = r.status(); });
page.on('requestfinished', r => { const entry = requests.get(r); if (entry) entry.finished = Date.now() - start; });
page.on('requestfailed', r => { const entry = requests.get(r); if (entry) entry.error = r.failure()?.errorText; });
page.on('pageerror', e => errors.push(e.message));
try {
  await page.goto('https://stock.toufai.top/sector-heat', { waitUntil: 'commit', timeout: 15000 });
  await page.locator('.sector-heat-panel[data-detail-key]:not([data-detail-key=""])').waitFor({ timeout: 25000 }).catch(() => {});
  if (process.env.SECTOR_BLOCK_CHART === '1') {
    await page.getByRole('alert').filter({ hasText: '图表资源加载失败' }).waitFor({ timeout: 15000 });
    assert.ok(await page.locator('.sector-heat-panel').getAttribute('data-detail-key'), 'real detail must survive chart failure');
    await page.locator('.evidence-section summary').click();
    assert.ok((await page.locator('.sector-heat-panel').innerText()).includes('三日持续性'), 'real factors must remain accessible');
    console.log(JSON.stringify({ chart_failure_isolated: true }));
  }
  console.log(JSON.stringify({ elapsed_ms: Date.now() - start, body: (await page.locator('body').innerText()).slice(0, 2500), errors, requests: [...requests.values()] }));
} catch (error) { console.log(JSON.stringify({ error: error.message.split('Call log:')[0], errors, requests: [...requests.values()] })); }
finally { await context.close(); await browser.close(); }
