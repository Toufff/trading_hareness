// Real deployed UI acceptance. No routes/mocks, no order or broker actions.
import { chromium } from '../frontend/node_modules/playwright/index.mjs';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const symbol = process.env.THESIS_VERIFY_SYMBOL || '600988.SH';
const base = process.env.THESIS_VERIFY_BASE || 'https://stock.toufai.top';
const directory = process.env.THESIS_VERIFY_OUTPUT || 'G:/StockPlatform/reports/reviews/thesis-live-browser';
const credentials = JSON.parse(await readFile(process.env.THESIS_VERIFY_CREDENTIALS ||
  'C:/Users/brave/.stockbrain/dashboard-credentials.json', 'utf8'));
await mkdir(directory, { recursive: true });
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 },
    httpCredentials: { username: credentials.username, password: credentials.password, origin: base } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const url = `${base}/research?tab=stock-study&symbol=${encodeURIComponent(symbol)}`;
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.getByRole('button', { name: '打开策略工作台', exact: true }).waitFor({ timeout: 30000 });
  await page.getByRole('button', { name: '打开策略工作台', exact: true }).click();
  const panel = page.getByTestId('trade-thesis-panel');
  await panel.waitFor({ timeout: 45000 });
  await panel.getByText('当前观察事实', { exact: true }).first().waitFor({ timeout: 45000 });
  for (const text of ['当前新买', '已有持仓', '原始范围', '当前范围']) {
    if (!(await panel.getByText(text, { exact: true }).first().isVisible())) throw new Error(`Missing ${text}`);
  }
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: resolve(directory, 'desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: resolve(directory, 'mobile.png'), fullPage: true });
  const response = await context.request.get(`${base}/api/research/theses?symbol=${encodeURIComponent(symbol)}`);
  if (!response.ok()) throw new Error(`API ${response.status()}`);
  const body = await response.json();
  const evaluation = body.items[0]?.evaluation;
  if (!evaluation?.evaluation_id) throw new Error('No actual stored evaluation');
  if (errors.length) throw new Error(`Browser errors: ${errors.join(';')}`);
  const receipt = { status: 'passed', url, symbol, evaluation_id: evaluation.evaluation_id,
    content_hash: evaluation.content_hash, source_run_id: evaluation.source_run_id,
    screenshots: ['desktop.png', 'mobile.png'], page_errors: errors, mocked: false };
  await writeFile(resolve(directory, 'receipt.json'), JSON.stringify(receipt, null, 2));
  console.log(JSON.stringify(receipt));
} finally { await browser.close(); }
