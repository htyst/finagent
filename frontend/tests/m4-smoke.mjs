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
  const agent = page.locator('.agent-section');
  async function send(message) {
    await agent.locator('#agent-message').fill(message);
    await agent.getByRole('button', { name: '发送' }).click();
  }
  await send('这个月餐饮消费多少？');
  await agent.getByText('1,200.00', { exact: false }).waitFor();
  let task = await page.evaluate(async () => (await (await fetch('/api/agent/tasks/current')).json()).data);
  if (task.status !== 'completed' || task.steps[0].tool_name !== 'summarize_spending') throw new Error('账单对话未调用真实统计工具');

  await send('从活期账户给张三转100元');
  await agent.getByText('3821', { exact: false }).waitFor();
  task = await page.evaluate(async () => (await (await fetch('/api/agent/tasks/current')).json()).data);
  if (task.proposal || task.status !== 'gathering_info') throw new Error('同名收款人未追问就生成操作单');
  await send('选尾号3821');
  await agent.locator('.proposal-card').getByText('¥100.00').waitFor();
  await agent.locator('.proposal-card').getByText('**3821', { exact: false }).waitFor();
  await page.locator('.account-card > strong').filter({ hasText: '¥10,000.00' }).waitFor();
  await send('确认');
  await agent.getByText('聊天确认不执行操作', { exact: false }).waitFor();
  await page.locator('.account-card > strong').filter({ hasText: '¥10,000.00' }).waitFor();
  await agent.getByRole('button', { name: '确认执行' }).click();
  await agent.locator('.receipt').getByText('模拟转账成功').waitFor();
  await page.locator('.account-card > strong').filter({ hasText: '¥9,900.00' }).waitFor();
  await page.reload({ waitUntil: 'networkidle' });
  await page.locator('.agent-section .receipt').getByText('模拟转账成功').waitFor();
  task = await page.evaluate(async () => (await (await fetch('/api/agent/tasks/current')).json()).data);
  if (task.status !== 'completed' || task.proposal.receipt.amount_minor !== 10000) throw new Error('对话任务刷新后未恢复真实回执');

  await send('冻结尾号1234的卡');
  await agent.locator('.proposal-card').getByText('日常借记卡', { exact: false }).waitFor();
  await agent.getByRole('button', { name: '确认执行' }).click();
  await agent.locator('.receipt').getByText('卡片已冻结').waitFor();
  const card = await page.evaluate(async () => (await (await fetch('/api/cards')).json()).data.find(row => row.id === 'c1a'));
  if (card.status !== 'frozen') throw new Error('卡片确认回执与真实状态不一致');
  await page.locator('#user').selectOption('u2');
  await page.locator('.account-card > strong').filter({ hasText: '¥6,800.00' }).waitFor();
  if (await page.locator('.agent-section .receipt').count()) throw new Error('切换用户后仍展示上一用户回执');
  console.log('M4 Edge 对话流程通过：真实统计、同名追问、聊天确认拒绝、按钮转账与回执恢复、卡片冻结、用户隔离。');
} finally {
  await browser.close();
}
