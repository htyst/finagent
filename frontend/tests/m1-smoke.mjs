import { chromium } from 'playwright-core';
import { existsSync, mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

const candidates = [
  process.env.FINAGENT_BROWSER_PATH,
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].filter(Boolean);
const executablePath = candidates.find((path) => existsSync(path));
if (!executablePath) throw new Error('未找到 Edge；请设置 FINAGENT_BROWSER_PATH');
const out = resolve('..', 'var');
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ executablePath, headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1360, height: 900 }, locale: 'zh-CN' });
  await page.goto('http://127.0.0.1:5173', { waitUntil: 'networkidle' });
  await page.locator('#user').selectOption('u1');
  await page.locator('.account-card > strong').filter({ hasText: '¥10,000.00' }).waitFor();
  if (await page.locator('.account-card').count() !== 2) throw new Error('主用户账户数错误');
  if (await page.locator('.bank-card').count() !== 2) throw new Error('主用户卡片数错误');
  if (!await page.getByText('•••• 1234').count()) throw new Error('主演示卡尾号缺失');
  await page.locator('.spending-summary').getByText('¥1,200.00').waitFor();
  await page.getByText('变化 50.0%').waitFor();
  await page.getByText('t_refund_50', { exact: false }).waitFor();
  const category = page.locator('.spending-filters select').nth(1);
  await category.selectOption('全部');
  await page.locator('.spending-filters button').click();
  await page.getByText('¥1,355.00').waitFor();
  await category.selectOption('餐饮');
  await page.locator('.evidence-list + .pager').getByRole('button', { name: '下一页' }).click();
  await page.locator('.evidence-list + .pager').getByText('2 / 2').waitFor();
  await page.getByText('¥1,355.00').waitFor();
  if (!await page.locator('.evidence-item').count()) throw new Error('修改表单但未查询时，分页丢失原查询证据');
  if (await page.getByText('所选范围没有已入账消费或退款。').count()) throw new Error('分页误用了未提交的类别');
  await page.locator('.spending-filters button').click();
  await page.locator('.spending-summary').getByText('¥1,200.00').waitFor();
  await page.screenshot({ path: resolve(out, 'm1-desktop.png'), fullPage: true });
  await page.locator('.transaction-list + .pager').getByRole('button', { name: '下一页' }).click();
  await page.getByText('2 / 2').waitFor();
  let releaseOldAccounts;
  let oldAccountsReady;
  const oldReady = new Promise((resolve) => { oldAccountsReady = resolve; });
  const oldReleased = new Promise((resolve) => { releaseOldAccounts = resolve; });
  let delayedResponseDelivered;
  const delivered = new Promise((resolve) => { delayedResponseDelivered = resolve; });
  let delayOnce = true;
  await page.route('**/api/accounts', async (route) => {
    if (!delayOnce) return route.continue();
    delayOnce = false;
    const response = await route.fetch();
    oldAccountsReady();
    await oldReleased;
    await route.fulfill({ response });
    delayedResponseDelivered();
  });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await oldReady;
  if (await page.locator('#user').inputValue() !== 'u1') throw new Error('刷新后会话未恢复');
  await page.locator('#user').selectOption('u2');
  await page.locator('.account-card > strong').filter({ hasText: '¥6,800.00' }).waitFor();
  releaseOldAccounts();
  await delivered;
  await page.waitForTimeout(100);
  if (await page.locator('.account-card > strong').filter({ hasText: '¥10,000.00' }).count()) throw new Error('旧用户延迟账户响应覆盖了新用户');
  await page.unroute('**/api/accounts');
  if (await page.locator('.account-card > strong').filter({ hasText: '¥10,000.00' }).count() || await page.locator('.spending-summary').getByText('¥1,200.00').count()) throw new Error('用户切换后仍展示旧数据');
  await page.locator('#user').selectOption('u3');
  await page.getByText('已冻结').waitFor();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: resolve(out, 'm1-mobile.png'), fullPage: true });
  if (await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)) throw new Error('窄屏产生水平溢出');
  console.log('Edge 浏览器验收通过：三用户隔离、消费统计与退款证据、分页、刷新恢复、冻结卡与窄屏布局。');
} finally {
  await browser.close();
}
