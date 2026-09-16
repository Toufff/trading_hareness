import { expect, test } from '@playwright/test';

const reviewUrl = process.env.STOCK_SB_REVIEW_URL;
if (process.env.STOCK_SB_REVIEW_BROWSER_CHANNEL === 'msedge') test.use({ channel: 'msedge' });

for (const [name, viewport] of [
  ['desktop', { width: 1440, height: 900 }],
  ['mobile', { width: 390, height: 844 }],
] as const) {
  test(`review order status and chart selection on ${name}`, async ({ page }) => {
    test.skip(!reviewUrl, 'Set STOCK_SB_REVIEW_URL to a prepared private review page.');
    await page.setViewportSize(viewport);
    await page.goto(reviewUrl!);

    const cards = page.locator('.event');
    await expect(cards).toHaveCount(6);
    await expect(page.locator('.event.filled')).toHaveCount(2);
    await expect(page.locator('.event.cancelled')).toHaveCount(4);
    await expect(page.locator('.event.cancelled').first()).toContainText('成交 0 股');

    for (let index = 0; index < 6; index += 1) {
      await page.locator('#intraday').scrollIntoViewIfNeeded();
      const point = await page.evaluate((eventIndex) => {
        const echarts = (window as unknown as { echarts: {
          getInstanceByDom: (element: HTMLElement) => {
            getOption: () => { series: Array<{ markPoint: { data: Array<{
              eventIndex: number; coord: [string, number]; symbolOffset: [number, number]
            }> } }> };
            convertToPixel: (query: { seriesIndex: number }, coord: [string, number]) => [number, number];
            getDom: () => HTMLElement;
          };
        } }).echarts;
        const chart = echarts.getInstanceByDom(document.getElementById('intraday')!);
        const marker = chart.getOption().series[0].markPoint.data.find(item => item.eventIndex === eventIndex);
        if (!marker) throw new Error(`Missing marker for order ${eventIndex}`);
        const [x, y] = chart.convertToPixel({ seriesIndex: 0 }, marker.coord);
        const bounds = chart.getDom().getBoundingClientRect();
        return { x: bounds.x + x + marker.symbolOffset[0], y: bounds.y + y + marker.symbolOffset[1] };
      }, index);
      await page.mouse.click(point.x, point.y);
      await expect(cards.nth(index)).toHaveClass(/active/);
    }

    await cards.nth(5).click();
    await page.locator('#viewBefore').click();
    const beforeView = await page.evaluate(() => {
      const echarts = (window as unknown as { echarts: {
        getInstanceByDom: (element: HTMLElement) => {
          getOption: () => {
            xAxis: Array<{ data: string[] }>;
            series: Array<{ markPoint: { data: Array<{ eventIndex: number }> } }>;
          };
        };
      } }).echarts;
      const option = echarts.getInstanceByDom(document.getElementById('intraday')!).getOption();
      return {
        markerIndices: option.series[0].markPoint.data.map(item => item.eventIndex),
        lastMinute: option.xAxis[0].data.at(-1),
      };
    });
    expect(beforeView.markerIndices).toEqual([0, 1, 2, 3]);
    expect(beforeView.lastMinute).toBe('14:26');
  });
}
