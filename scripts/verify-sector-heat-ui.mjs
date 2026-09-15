/** Real-browser acceptance: published Vue page and its actual snapshot API. */
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(path.join(root, 'frontend/package.json'));
const { chromium } = require('@playwright/test');
const baseURL = process.env.SECTOR_HEAT_BASE_URL || 'https://stock.toufai.top';
const apiOrigin = process.env.SECTOR_HEAT_API_ORIGIN || baseURL;
const credentialsPath = process.env.STOCK_DASHBOARD_CREDENTIALS;
const credentials = credentialsPath ? JSON.parse(await fs.readFile(credentialsPath, 'utf8')) : null;
const browser = await chromium.launch({ headless: true,
  ...(process.env.SECTOR_HEAT_DIRECT === '1' ? { args: ['--no-proxy-server'] } : {}),
  ...(process.env.SECTOR_HEAT_BROWSER_CHANNEL ? { channel: process.env.SECTOR_HEAT_BROWSER_CHANNEL } : {}) });
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  ...(credentials ? { httpCredentials: { username: credentials.username, password: credentials.password } } : {}),
});
// Match the existing password-free user entry, avoiding repeated Basic challenges.
if (credentials?.magic_cookie_token) await context.addCookies([{
  name: 'stockbrain_access', value: credentials.magic_cookie_token,
  url: apiOrigin, httpOnly: true, secure: apiOrigin.startsWith('https:'), sameSite: 'Strict',
}]);
const page = await context.newPage();
// Local visual acceptance uses the real published API, never fabricated fixtures.
if (apiOrigin !== baseURL) await page.route('**/api/v1/sector-heat**', async route => {
  const url = new URL(route.request().url());
  const response = await context.request.get(apiOrigin + url.pathname + url.search);
  await route.fulfill({ response });
});
const errors = [];
const startedAt = Date.now();
const timings = {};
const checkpoint = (name) => { timings[name] = Date.now() - startedAt; console.log(JSON.stringify({ step: name, elapsed_ms: timings[name] })); };
page.on('pageerror', (error) => errors.push(error.message));
try {
  const response = await context.request.get(`${apiOrigin}/api/v1/sector-heat`);
  assert.equal(response.status(), 200, 'public market section must be accessible');
  const market = await response.json();
  const heat = market.sector_heat;
  assert.ok(heat?.items?.length > 0, 'published snapshot must contain real sector heat');
  assert.equal(heat.schema_version, 1);
  checkpoint('ranking_loaded');
  await page.goto(`${baseURL}/sector-heat`, { waitUntil: 'commit', timeout: 30000 });
  await page.waitForFunction(() => Boolean(document.getElementById('app')?.textContent?.trim()), null, { timeout: 5000 });
  checkpoint('nonblank_entry_visible');
  await page.getByRole('heading', { name: '板块热度与轮动', exact: true }).waitFor({ timeout: 20000 });
  checkpoint('standalone_visible');
  const panel = page.locator('.sector-heat-panel');
  await panel.getByText(heat.trade_date, { exact: false }).first().waitFor({ timeout: 30000 });
  const industry = heat.items.find((item) => item.kind === 'industry' && item.attention_score != null);
  assert.ok(industry, 'at least one real industry is scored');
  const search = panel.getByPlaceholder('搜索名称或代码');
  await search.fill(industry.name);
  await panel.getByRole('button', { name: industry.name, exact: true }).first().click();
  await page.waitForFunction((key) => document.querySelector('.sector-heat-panel')?.getAttribute('data-detail-key') === key, industry.key);
  checkpoint('industry_data_visible');
  assert.ok(timings.industry_data_visible - timings.nonblank_entry_visible < 20000, 'readable real data must not wait for chart assets or minute-long bootstrap');
  await panel.locator('canvas').first().waitFor({ state: 'visible' });
  checkpoint('industry_chart_visible');
  const evidenceDir = path.join(root, 'docs/evidence/sector-heat');
  await fs.mkdir(evidenceDir, { recursive: true });
  await search.fill('');
  // Screenshot only after the chart's 240ms entrance animation has settled.
  await page.evaluate(() => new Promise(resolve => { const start = performance.now(); const tick = () => performance.now() - start > 400 ? resolve() : requestAnimationFrame(tick); requestAnimationFrame(tick); }));
  await page.screenshot({ path: path.join(evidenceDir, 'desktop-1440.png'), fullPage: true });
  const geometry = await page.evaluate(() => {
    const rect = (selector) => { const r = document.querySelector(selector).getBoundingClientRect(); return { x:r.x, y:r.y, width:r.width, height:r.height }; };
    return { chart: rect('.heat-chart'), ranking:rect('.ranking-pane'), detail:rect('.detail-pane'), overflow:document.documentElement.scrollWidth > innerWidth };
  });
  assert.equal(geometry.overflow, false, 'desktop must have no horizontal overflow');
  assert.ok(geometry.chart.width >= 650 && geometry.chart.height >= 300, 'chart must be a substantial primary visual');
  assert.ok(geometry.chart.y < 750, 'chart must begin in the first desktop viewport');
  assert.ok(Math.abs(geometry.ranking.y - geometry.detail.y) < 3, 'ranking/detail must share a top edge');
  assert.ok(geometry.ranking.height < 1000 && geometry.detail.height < 1000, 'default layout must not expand all ranking rows or leave a giant blank detail column');
  assert.ok(Math.abs(geometry.ranking.height - geometry.detail.height) < 3, 'desktop columns must have aligned bottom edges');
  assert.ok(await panel.locator('.ranking-list').evaluate(el => el.scrollHeight > el.clientHeight), 'full ranking must scroll inside its panel');
  assert.equal(await panel.locator('.evidence-section').getAttribute('open'), null, 'audit material starts collapsed');
  await panel.locator('.evidence-section summary').click();
  await panel.getByText('三日持续性', { exact: true }).waitFor();
  await panel.locator('.evidence-section summary').click();
  await panel.getByLabel('完整历史窗口').check();
  await panel.getByLabel('完整历史窗口').uncheck();
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.evaluate(() => new Promise(resolve => { const start = performance.now(); const tick = () => performance.now() - start > 400 ? resolve() : requestAnimationFrame(tick); requestAnimationFrame(tick); }));
  await page.screenshot({ path: path.join(evidenceDir, 'desktop-1920.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await panel.getByText('日内', { exact: true }).click();
  await panel.getByText('日间', { exact: true }).click();
  await search.fill('');
  await panel.getByText('概念', { exact: true }).click();
  const concept = heat.items.find((item) => item.kind === 'concept');
  assert.ok(concept, 'real concept records must exist');
  await search.fill(concept.name);
  await panel.getByRole('button', { name: concept.name, exact: true }).first().click();
  await page.waitForFunction((key) => document.querySelector('.sector-heat-panel')?.getAttribute('data-detail-key') === key, concept.key);
  await panel.locator('canvas').first().waitFor({ state: 'visible' });
  assert.ok((await panel.textContent()).includes('三日持续性'), 'the selected board must render its factor details');
  checkpoint('concept_detail_visible');
  assert.equal(errors.length, 0, `browser runtime errors: ${errors.join('; ')}`);
  await search.fill('');
  await panel.screenshot({ path: path.join(evidenceDir, 'desktop.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await panel.scrollIntoViewIfNeeded();
  await panel.screenshot({ path: path.join(evidenceDir, 'mobile.png') });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'mobile must not overflow sideways');
  const mobileGeometry = await page.evaluate(() => ({
    rankingHeight: document.querySelector('.ranking-pane').getBoundingClientRect().height,
    chartWidth: document.querySelector('.heat-chart').getBoundingClientRect().width,
  }));
  assert.ok(mobileGeometry.rankingHeight < 300, 'mobile ranking must not bury selected details');
  assert.ok(mobileGeometry.chartWidth >= 300, 'mobile chart stays readable');
  for (const width of [1280, 768, 320]) {
    await page.setViewportSize({ width, height: 900 });
    await page.waitForFunction(() => {
      const canvas = document.querySelector('.heat-chart canvas');
      const box = document.querySelector('.heat-chart');
      return canvas && box && Math.abs(canvas.getBoundingClientRect().width - box.getBoundingClientRect().width) < 3;
    }, null, { timeout: 5000 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `no overflow at ${width}px`);
  }
  assert.equal(errors.length, 0);
  const report = {
    passed: true, checked_at: new Date().toISOString(), base_url: baseURL,
    trade_date: heat.trade_date, model_version: heat.model_version,
    timings_ms: timings, desktop_geometry: geometry, mobile_geometry: mobileGeometry,
    sectors: heat.items.length, industry: industry.name, concept: concept.name,
    checks: ['real public section API', 'visible standalone entry', 'industry search and selection',
      'rendered chart canvas', 'intraday/daily controls', 'concept selection',
      'desktop/mobile rendering', 'no browser runtime exceptions'],
  };
  await fs.writeFile(path.join(evidenceDir, 'browser.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report));
} catch (error) {
  const evidenceDir = path.join(root, 'docs/evidence/sector-heat');
  await fs.mkdir(evidenceDir, { recursive: true });
  await page.screenshot({ path: path.join(evidenceDir, 'failure.png'), timeout: 5000 }).catch(() => {});
  // Playwright's request call log can contain Basic authentication headers.
  console.error(JSON.stringify({ passed: false, error: String(error.message).split('Call log:')[0], page_errors: errors, timings_ms: timings }));
  process.exitCode = 1;
} finally {
  await context.close();
  await browser.close();
}
