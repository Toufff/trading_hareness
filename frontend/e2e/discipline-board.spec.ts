import { expect, test, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';

// Fixture-driven (no live service): every /api request is answered from e2e/fixtures/discipline,
// which are real-shaped responses exported read-only from the owner API on 2026-09-19 (the three
// new-buy plans are the new generator's dry-run cards).  Run against a built frontend, e.g.
//   npx vite build && npx vite preview --port 15692 &
//   PLAYWRIGHT_BASE_URL=http://127.0.0.1:15692 npx playwright test e2e/discipline-board.spec.ts
test.use({ channel: 'msedge', headless: true });

const dir = new URL('./fixtures/discipline/', import.meta.url);
const load = (name: string) => JSON.parse(readFileSync(new URL(name, dir), 'utf8'));
const plans = load('plans-latest.json') as { items: Array<{ plan_id: string; symbol: string }> };
const symbolOf = (id: string) => plans.items.find((item) => item.plan_id === id)?.symbol;
const SHOTS = process.env.DISCIPLINE_SHOT_DIR;

async function serveFixtures(page: Page) {
  await page.clock.setFixedTime(new Date('2026-09-19T12:00:00+08:00'));
  const requests: string[] = [];
  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (!url.pathname.startsWith('/api/') && !['/events', '/health'].includes(url.pathname)) return route.continue();
    requests.push(`${request.method()} ${url.pathname}${url.search}`);
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    const path = url.pathname;
    if (path === '/api/research/discipline/plans/latest') return json(plans);
    if (path === '/api/research/discipline/reconciliations') return json(load('reconciliations.json'));
    if (path === '/api/research/discipline/plans/history') return json(load(`history-${url.searchParams.get('symbol')}.json`));
    const perPlan = /^\/api\/research\/discipline\/plans\/([0-9a-f-]{36})\/(chart|evaluations)$/.exec(path);
    if (perPlan) {
      const symbol = symbolOf(perPlan[1]!);
      if (perPlan[2] === 'evaluations') return json(load(`evaluations-${symbol}.json`));
      if (url.searchParams.get('basis') === 'minute') {
        return symbol === '600613.SH' ? json(load('minute-600613.SH-2026-09-18.json'))
          : json({ basis: 'minute', rows: [], count: 0, reason: '该交易日没有已入库的 longhu 分钟线', date: '2026-09-18' });
      }
      return json(load(`chart-${symbol}.json`));
    }
    if (path === '/api/research/personal/holding-advice/latest') return json(load('holding-advice.json'));
    if (path === '/api/research/strategy/post-close/latest') return json({ latest_completed: { summary: { recommendation_pool: load('recommendation-pool.json') } } });
    if (path === '/api/research/strategy/post-close/watchlist/latest') return json({ status: 'completed', research_only: true, depends_on_holdings: false, total_unique: 0, items: [] });
    if (path === '/api/research/advice/market/latest') return json({ status: 'unavailable', as_of_at: '2026-09-19T12:00:00+08:00', content: null, delivery: { eligible: false } });
    return json({});
  });
  return requests;
}

test('holdings page: action summary first, discipline card with lines on the K-line, read-only', async ({ page }) => {
  const requests = await serveFixtures(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/holdings');
  const board = page.getByTestId('discipline-board');
  await expect(board).toBeVisible({ timeout: 30_000 });
  const summary = board.getByTestId('discipline-summary');
  await expect(summary).toContainText('今日动作汇总');
  await expect(summary).toContainText('当前总风险 7.55%');
  await expect(summary.locator('.summary-row')).toHaveCount(4);
  await expect(summary.locator('.summary-row[data-symbol="600613.SH"]')).toContainText('09-21 开盘15分钟内减到 1200 股（卖 4600，可卖 5800）');
  await expect(summary.locator('.summary-row[data-symbol="000977.SZ"]')).toContainText('清仓 300 股');

  await summary.locator('.summary-row[data-symbol="600613.SH"]').click();
  const detail = page.getByTestId('discipline-detail');
  await expect(detail).toHaveAttribute('data-plan-id', '699144a6-00e4-42a2-bdf7-168e601bfef0');
  await expect(detail.getByTestId('today-action')).toContainText('硬止损 7.63');
  const chart = detail.getByTestId('discipline-chart');
  await expect(chart.locator('canvas').first()).toBeVisible();
  await expect(chart).toHaveAttribute('data-labels', /^[1-9]/);          // right-hand price tags were placed
  // the fill markers explain themselves: legend entry here, per-fill detail on hover
  await expect(detail.getByTestId('marker-legend')).toContainText('真实成交（×N 为当日笔数');
  const table = detail.getByTestId('discipline-line-table');
  await expect(table.locator('tr[data-kind="hard_stop"]').first()).toContainText('7.63');
  await expect(table.locator('tr[data-kind="time_stop"]')).toContainText('8.69');

  // hovering the hard-stop row shows its formula with the binding term
  await table.locator('tr[data-kind="hard_stop"]').first().hover();
  await expect(detail.getByTestId('hard-stop-terms')).toContainText('起约束');
  await expect(detail.getByTestId('hard-stop-terms')).toContainText('0.9×ATR14');

  await expect(detail.getByTestId('sizing')).toContainText('floor(98996.26 × 1.0% ÷ (8.41 − 7.63) ÷ 100) × 100 = 1200 股');
  await detail.getByRole('tab', { name: '证据与历史' }).click();
  await expect(detail.getByTestId('quality-checks')).toContainText('有效期不超过 5 个交易日');
  await expect(detail).toContainText('研究用途，系统不下单');
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/e2e-holdings-1440.png`, fullPage: true });

  expect(requests.filter((item) => item.includes('/discipline/')).every((item) => item.startsWith('GET '))).toBe(true);
  expect(requests.some((item) => /^(POST|PUT|DELETE|PATCH) /.test(item))).toBe(false);
});

test('holdings page stacks list and detail at phone width without horizontal scroll', async ({ page }) => {
  await serveFixtures(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/holdings');
  await expect(page.getByTestId('discipline-summary')).toBeVisible({ timeout: 30_000 });
  const board = await page.getByTestId('discipline-board').boundingBox();
  expect(board!.width).toBeLessThanOrEqual(390);
  const list = await page.locator('.card-list').boundingBox();
  const detail = await page.locator('.card-detail').boundingBox();
  expect(detail!.y).toBeGreaterThan(list!.y);                              // stacked, not side by side
  const overflow = await page.getByTestId('discipline-board').evaluate((element) => element.scrollWidth - element.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/e2e-holdings-390.png`, fullPage: true });
});

test('minute mode shows the Longhu tape, VWAP and the 3-bar intraday stop count', async ({ page }) => {
  await serveFixtures(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/holdings');
  await page.getByTestId('discipline-summary').locator('.summary-row[data-symbol="600613.SH"]').click();
  const detail = page.getByTestId('discipline-detail');
  await detail.getByText('分钟', { exact: true }).click();
  await expect(detail.getByTestId('minute-streak')).toContainText('已连续 0/3 根收于止损下');
  await expect(detail).toContainText('longhu 分时只有每分钟一个价格');
  if (SHOTS) await detail.screenshot({ path: `${SHOTS}/e2e-minute.png` });
  // a session without stored minutes says why instead of drawing nothing
  await page.getByTestId('discipline-summary').locator('.summary-row[data-symbol="000977.SZ"]').click();
  await page.getByTestId('discipline-detail').getByText('分钟', { exact: true }).click();
  await expect(page.getByTestId('minute-empty')).toContainText('没有已入库的 longhu 分钟线');
});

test('recommendation pool: each pick expands its new-buy discipline card', async ({ page }) => {
  await serveFixtures(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/market');
  const pick = page.locator('.pool-discipline[data-symbol="000811.SZ"]');
  await expect(pick).toBeVisible({ timeout: 30_000 });
  await pick.getByRole('button', { name: '纪律卡' }).click();
  const card = page.locator('[data-testid=pool-discipline-panel][data-symbol="000811.SZ"]').getByTestId('discipline-detail');
  await expect(card).toContainText('新买计划');
  await expect(card.getByTestId('today-action')).toContainText('等待触发：收盘在 39.68–42.74 之间且成交额不低于前一日且行业当日不走弱，最多买 300 股');
  await expect(card.getByTestId('discipline-line-table').locator('tr[data-kind="chase_cap"]')).toContainText('已越过追高上限，不买');
  await expect(card.getByTestId('sizing')).toContainText('41.52 = max(lane 结构参考 37.94，2026-09-18 收盘 41.52)');
  if (SHOTS) { await page.waitForTimeout(800); await page.screenshot({ path: `${SHOTS}/e2e-pool-new-buy.png` }); }
  await card.getByRole('tab', { name: '推荐池研究条件' }).click();
  await expect(card.getByTestId('research-conditions')).toContainText('研究条件，非系统线');
  if (SHOTS) await card.screenshot({ path: `${SHOTS}/e2e-pool-new-buy-research.png` });
});
