import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';

test.use({ channel:'msedge', headless:true });
const navigation = ['量化研究台','市场与选股','我的持仓','导入监控','飞书工作台','手动投递'];
test('phone shell preserves all navigation and gives content full width; desktop sidebar is unchanged', async ({page}) => {
  const css = readFileSync('src/style.css','utf8');
  const element = readFileSync('node_modules/element-plus/dist/index.css','utf8');
  // Real browser geometry regression with the same shell hierarchy and inline aside width.
  // This complements, rather than replaces, the authenticated published-page acceptance.
  await page.setContent(`<style>${css}</style><style>${element}</style><div class="el-container app-shell">
    <aside class="el-aside side-nav" style="--el-aside-width:236px;width:236px">
      <div class="brand"><div><strong>Quant Research</strong><span>投研与市场数据</span></div></div>
      <ul class="el-menu menu">${navigation.map(name=>`<li role="menuitem" class="el-menu-item"><span>${name}</span></li>`).join('')}</ul>
      <div class="side-state">连接状态<button class="el-button">设置 Key</button></div>
    </aside><section class="el-container is-vertical"><header class="el-header topbar"><h1>量化研究台</h1></header>
    <main class="el-main content"><section class="governance" style="width:100%;border:1px solid">策略治理与实验验收</section></main></section></div>`);
  await page.setViewportSize({width:390,height:844});
  const box=await page.locator('.governance').boundingBox();
  expect(box!.width).toBeGreaterThan(320);expect(box!.x).toBeLessThan(20);
  for(const name of navigation) await expect(page.getByRole('menuitem',{name})).toBeVisible();
  await expect(page.getByRole('button',{name:'设置 Key'})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.setViewportSize({width:1440,height:1000});
  const side=await page.locator('.side-nav').boundingBox();
  expect(side!.width).toBe(236);
  expect((await page.locator('.content').boundingBox())!.x).toBeGreaterThanOrEqual(236);
});
