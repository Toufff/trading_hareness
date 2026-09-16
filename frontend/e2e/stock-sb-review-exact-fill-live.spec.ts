import { expect, test } from '@playwright/test';

const reviewUrl = process.env.STOCK_SB_REVIEW_EXACT_URL;
if (process.env.STOCK_SB_REVIEW_BROWSER_CHANNEL === 'msedge') test.use({ channel: 'msedge' });

test('broker-confirmed fills stay clickable, including a fill after the last minute bar', async ({ page }) => {
  test.skip(!reviewUrl, 'Set STOCK_SB_REVIEW_EXACT_URL to a prepared private review page.');
  await page.goto(reviewUrl!);
  await expect(page.locator('.event')).toHaveCount(3);
  await expect(page.locator('.event.filled')).toHaveCount(3);
  await expect(page.locator('#meta')).toContainText('3 笔券商成交时刻已核实');

  for (let index = 0; index < 3; index += 1) {
    await page.locator('#intraday').scrollIntoViewIfNeeded();
    const point = await page.evaluate((eventIndex) => {
      const echarts = (window as unknown as { echarts: {
        getInstanceByDom: (element: HTMLElement) => {
          getOption: () => { series: Array<{ markPoint: { data: Array<{
            eventIndex: number; coord: [string, number]; symbolOffset: [number, number]; name: string
          }> } }> };
          convertToPixel: (query: { seriesIndex: number }, coord: [string, number]) => [number, number];
          getDom: () => HTMLElement;
        };
      } }).echarts;
      const chart = echarts.getInstanceByDom(document.getElementById('intraday')!);
      const marker = chart.getOption().series[0].markPoint.data.find(item => item.eventIndex === eventIndex);
      if (!marker) throw new Error(`Missing marker for fill ${eventIndex}`);
      if (eventIndex === 2 && !marker.name.includes('对应分钟K缺失')) {
        throw new Error('Missing-minute fill must be labeled, not projected onto a made-up K bar');
      }
      const [x, y] = chart.convertToPixel({ seriesIndex: 0 }, marker.coord);
      const bounds = chart.getDom().getBoundingClientRect();
      return { x: bounds.x + x + marker.symbolOffset[0], y: bounds.y + y + marker.symbolOffset[1] };
    }, index);
    await page.mouse.click(point.x, point.y);
    await expect(page.locator('.event').nth(index)).toHaveClass(/active/);
  }
  await expect(page.locator('#focus')).toContainText('实际成交时间 · 券商记录');
  await expect(page.locator('#focus')).toContainText('15:00:00');
});
