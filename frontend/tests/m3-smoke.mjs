import { chromium } from 'playwright-core';
import { existsSync } from 'node:fs';

const executablePath = [process.env.FINAGENT_BROWSER_PATH,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(path => path && existsSync(path));
if (!executablePath) throw new Error('未找到 Edge；请设置 FINAGENT_BROWSER_PATH');

const browser = await chromium.launch({ executablePath, headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1360, height: 900 }, locale: 'zh-CN' });
  await page.goto('http://127.0.0.1:5173', { waitUntil: 'networkidle' });
  await page.locator('#user').selectOption('u1');
  await page.locator('.account-card > strong').filter({ hasText: '¥10,000.00' }).waitFor();

  const csrfDenied = await page.evaluate(async () => {
    const response = await fetch('/api/transfers/prepare', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_account_id: 'a1c', target: { type: 'own_account', account_id: 'a1s' }, amount_minor: 1 }) });
    return response.status;
  });
  if (csrfDenied !== 403) throw new Error('缺少 CSRF 保护的准备请求未被拒绝');

  await page.locator('.manual-form summary').click();
  let selects = page.locator('.operation-form select');
  if (!(await page.locator('.manual-form').getAttribute('open') !== null)) await page.locator('.manual-form summary').click();
  await selects.nth(1).selectOption('a1c');
  await selects.nth(3).selectOption('a1s');
  await page.locator('.operation-form input').first().fill('100.00');
  await page.getByRole('button', { name: '准备操作单' }).click();
  await Promise.race([page.locator('.proposal-card').waitFor(), page.locator('.operation-section [role=alert]').waitFor()]);
  const firstError = await page.locator('.operation-section [role=alert]').count() ? await page.locator('.operation-section [role=alert]').textContent() : null;
  if (firstError) throw new Error(`准备操作单失败: ${firstError}`);
  await page.locator('.account-card > strong').filter({ hasText: '¥10,000.00' }).waitFor();
  await page.getByRole('button', { name: '确认执行' }).click();
  await page.locator('.receipt').getByText('模拟转账成功').waitFor();
  await page.locator('.account-card > strong').filter({ hasText: '¥9,900.00' }).waitFor();
  await page.locator('.account-card > strong').filter({ hasText: '¥20,100.00' }).waitFor();
  await page.reload({ waitUntil: 'networkidle' });
  await page.locator('.receipt').getByText('模拟转账成功').waitFor();
  const restored = await page.evaluate(async () => (await (await fetch('/api/tasks/current')).json()).data);
  if (restored?.proposal?.status !== 'executed' || restored.proposal.receipt.amount_minor !== 10000) {
    throw new Error('刷新后未从服务端恢复已执行转账回执');
  }

  selects = page.locator('.operation-form select');
  if (!(await page.locator('.manual-form').getAttribute('open') !== null)) await page.locator('.manual-form summary').click();
  await selects.nth(1).selectOption('a1c');
  await selects.nth(2).selectOption('saved_recipient');
  await selects.nth(3).selectOption('r1');
  await page.locator('.operation-form input').first().fill('20.00');
  await page.getByRole('button', { name: '准备操作单' }).click();
  await page.locator('.proposal-card').getByText('¥20.00').waitFor();
  await page.getByRole('button', { name: '确认执行' }).click();
  await page.locator('.receipt').getByText('模拟转账成功').waitFor();
  await page.locator('.account-card > strong').filter({ hasText: '¥9,880.00' }).waitFor();

  await selects.first().selectOption('card_action');
  selects = page.locator('.operation-form select');
  await selects.nth(1).selectOption('c1a');
  await selects.nth(2).selectOption('freeze');
  await page.getByRole('button', { name: '准备操作单' }).click();
  await page.locator('.proposal-card').getByText('日常借记卡', { exact: false }).waitFor();
  await page.getByRole('button', { name: '确认执行' }).click();
  await page.locator('.receipt').getByText('卡片已冻结').waitFor();
  let card = await page.evaluate(async () => (await (await fetch('/api/cards')).json()).data.find(row => row.id === 'c1a'));
  if (card.status !== 'frozen') throw new Error('冻结回执与卡状态不一致');

  await selects.nth(2).selectOption('unfreeze');
  await page.getByRole('button', { name: '准备操作单' }).click();
  await page.getByRole('button', { name: '确认执行' }).click();
  await page.locator('.receipt').getByText('卡片已解冻').waitFor();
  card = await page.evaluate(async () => (await (await fetch('/api/cards')).json()).data.find(row => row.id === 'c1a'));
  if (card.status !== 'active') throw new Error('解冻回执与卡状态不一致');

  await selects.first().selectOption('transfer');
  selects = page.locator('.operation-form select');
  if (!(await page.locator('.manual-form').getAttribute('open') !== null)) await page.locator('.manual-form summary').click();
  await selects.nth(1).selectOption('a1c');
  await selects.nth(2).selectOption('own_account');
  await selects.nth(3).selectOption('a1s');
  await page.locator('.operation-form input').first().fill('1.00');
  await page.getByRole('button', { name: '准备操作单' }).click();
  await page.getByRole('button', { name: '取消操作单' }).click();
  await page.locator('.proposal-card').getByText('已取消').waitFor();
  await page.locator('.account-card > strong').filter({ hasText: '¥9,880.00' }).waitFor();
  console.log('M3 Edge 流程通过：本人/外部模拟转账、确认回执和刷新恢复、冻/解冻、取消及 CSRF 拒绝。');
} finally {
  await browser.close();
}
