import { expect, test } from '@playwright/test';

const reviewUrl = process.env.STOCK_SB_REVIEW_CONTEXT_URL;
if (process.env.STOCK_SB_REVIEW_BROWSER_CHANNEL === 'msedge') test.use({ channel: 'msedge' });
// Review data lives in a page-scoped const; reading it needs eval under the page CSP.
test.use({ bypassCSP: true });

for (const [name, viewport] of [
  ['desktop', { width: 1440, height: 900 }],
  ['mobile', { width: 390, height: 844 }],
] as const) {
  test(`decision-time position, order book, board and breadth context on ${name}`, async ({ page }) => {
    test.skip(!reviewUrl, 'Set STOCK_SB_REVIEW_CONTEXT_URL to a prepared private review page with context data.');
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setViewportSize(viewport);
    await page.goto(reviewUrl!);

    const cards = page.locator('.event');
    const count = await cards.count();
    expect(count).toBeGreaterThan(0);
    for (let index = 0; index < count; index += 1) {
      await cards.nth(index).click();
      await expect(cards.nth(index)).toHaveClass(/active/);
      await expect(page.locator('#position')).toContainText('交易前券商快照');
      await expect(page.locator('#position')).toContainText('本笔前持仓 · 推算');
      const status = await page.evaluate((eventIndex) => {
        const event = ((0, eval)('D') as { events: Array<{ order_book_context: { status: string };
          board_context: { status: string }; breadth_context: unknown }> }).events[eventIndex];
        return { book: event.order_book_context.status, board: event.board_context.status, breadth: Boolean(event.breadth_context) };
      }, index);
      // Orders before the first collected snapshot must say so rather than borrow a later one.
      await expect(page.locator('#bookFocus')).toContainText(status.book === 'observed' ? '委托前最近盘口' : '委托前没有盘口快照');
      await expect(page.locator('#bookFocus')).toContainText(status.breadth ? '概念板块上涨占比' : '委托前没有市场宽度快照');
      await expect(page.locator('#boardFocus')).toContainText(status.board === 'observed' ? '委托前板块快照' : '委托前没有板块快照');

      // Every context timestamp shown for the order must not be later than the order itself.
      const leak = await page.evaluate((eventIndex) => {
        const data = (0, eval)('D') as {
          events: Array<{ order_at: string; order_book_context: { observed_at?: string };
            board_context: { observed_at?: string }; breadth_context: { available_at: string } | null }>;
        };
        const event = data.events[eventIndex];
        const at = new Date(event.order_at).getTime();
        return [event.order_book_context.observed_at, event.board_context.observed_at, event.breadth_context?.available_at]
          .filter((value): value is string => Boolean(value))
          .filter(value => new Date(value).getTime() > at);
      }, index);
      expect(leak).toEqual([]);
    }

    const cardsBefore = await page.evaluate(() => {
      const chart = (window as unknown as { echarts: { getInstanceByDom: (element: HTMLElement) => {
        getOption: () => { series: Array<{ name: string }> } } } }).echarts
        .getInstanceByDom(document.getElementById('intraday')!);
      return chart.getOption().series.map(series => series.name);
    });
    const search = page.locator('#boardSearch');
    const firstLabel = await page.locator('#boardList option').first().getAttribute('value');
    expect(firstLabel).toBeTruthy();
    await search.fill(firstLabel!);
    await page.locator('#boardAdd').click();
    await expect(page.locator('#boardChips')).toContainText(firstLabel!);
    const seriesAfter = await page.evaluate(() => {
      const chart = (window as unknown as { echarts: { getInstanceByDom: (element: HTMLElement) => {
        getOption: () => { series: Array<{ name: string }> } } } }).echarts
        .getInstanceByDom(document.getElementById('intraday')!);
      return chart.getOption().series.map(series => series.name);
    });
    expect(seriesAfter).toContain(`${firstLabel} %`);
    expect(seriesAfter.length).toBe(cardsBefore.length + (cardsBefore.includes(`${firstLabel} %`) ? 0 : 1));

    await cards.first().click();
    await page.locator('#viewBefore').click();
    const tapeLast = await page.evaluate(() => {
      const chart = (window as unknown as { echarts: { getInstanceByDom: (element: HTMLElement) => {
        getOption: () => { xAxis?: Array<{ data: string[] }> } } } }).echarts
        .getInstanceByDom(document.getElementById('tape')!);
      const data = (0, eval)('D') as { events: Array<{ order_at: string }> };
      const order = new Date(data.events[0].order_at).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }).slice(0, 5);
      return { last: chart.getOption().xAxis?.[0]?.data.at(-1) ?? null, order };
    });
    // An empty tape is valid when collection started after the order.
    if (tapeLast.last !== null) expect(tapeLast.last <= tapeLast.order).toBeTruthy();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(1);
    expect(errors).toEqual([]);
  });
}
