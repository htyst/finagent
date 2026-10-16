import { chromium } from 'playwright-core';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
const executablePath = [process.env.FINAGENT_BROWSER_PATH,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(path => path && existsSync(path));
if (!executablePath) throw new Error('未找到 Edge');
const recording = process.env.FINAGENT_RECORD_DIR;
const began = Date.now();
const evidence = { mode: process.env.FINAGENT_MODE || 'mock', started: new Date().toISOString(), passed: false, billing: false, steps: [] };
const browser = await chromium.launch({ executablePath, headless: true });
try {
  const page = await browser.newPage({ ...(recording ? { recordVideo: { dir: recording, size: { width: 1360, height: 1000 } } } : {}), viewport: { width: 1360, height: 1000 }, locale: 'zh-CN' });
  page.setDefaultTimeout(90000);
  await page.goto(process.env.FINAGENT_TEST_BASE_URL || 'http://127.0.0.1:5173', { waitUntil: 'networkidle' });
  await page.locator('#user').selectOption('u1');
  await page.locator('.account-card > strong').filter({ hasText: '¥10,000.00' }).waitFor();
  const agent = page.locator('.agent-section');
  async function send(message) {
    await agent.locator('#agent-message').fill(message);
    await agent.getByRole('button', { name: '发送', exact: true }).click();
  }
  const current = () => page.evaluate(async () => (await (await fetch('/api/agent/tasks/current')).json()).data);
  if (recording) {
    await send('这个月餐饮消费多少？');
    await agent.getByText('任务状态：已完成', { exact: true }).waitFor();
    const bill = await current();
    const reply = bill.messages.at(-1).content;
    if (!reply.includes('1,200.00') || !reply.includes('50.0%')) throw new Error('账单金额不符合演示口径');
    evidence.billing = true;
    await agent.scrollIntoViewIfNeeded();
    await page.waitForTimeout(2000);
  }
  const storyA = '昨晚有笔不认识的消费，帮我查是哪张卡；如果是尾号1234，帮我临时冻结它，再告诉我账户可用余额。';
  await send('如果本月餐饮超过500元，就从活期转100元到储蓄账户。');
  await agent.locator('.agent-proposal .condition-details').getByText('净消费须严格大于', { exact: false }).waitFor();
  await page.locator('.operation-section').getByText('当前操作单已在', { exact: false }).waitFor();
  if (await page.locator('.proposal-card').count() !== 1) throw new Error('同一操作单重复展示');
  let task = await current();
  if (task.status !== 'awaiting_confirmation' || task.steps.length !== 2 || task.proposal.summary.condition.threshold_minor !== 50000) throw new Error('条件转账未建立固定计划');
  await send('确认');
  await agent.getByText('聊天确认不执行操作', { exact: false }).waitFor();
  await page.locator('.account-card > strong').filter({ hasText: '¥10,000.00' }).waitFor();
  if (recording) { await agent.locator('.agent-proposal').scrollIntoViewIfNeeded(); await page.waitForTimeout(2000); }
  await agent.getByRole('button', { name: '确认执行', exact: true }).click();
  await agent.locator('.receipt').getByText('模拟转账成功').waitFor();
  await page.locator('.account-card > strong').filter({ hasText: '¥9,900.00' }).waitFor();
  await page.reload({ waitUntil: 'networkidle' });
  await agent.locator('.receipt').getByText('模拟转账成功').waitFor();
  if ((await current()).status !== 'completed') throw new Error('条件转账恢复失败');

  async function chooseOriginal() {
    await agent.getByText('昨晚有多笔候选消费', { exact: false }).waitFor();
    if (await agent.locator('.agent-candidate').count() !== 2) throw new Error('未展示两笔交易候选');
    await agent.locator('.agent-candidate').filter({ hasText: '卡尾号 1234' }).getByRole('button', { name: '选择这笔交易' }).click();
    await agent.locator('.agent-proposal .condition-details').getByText('仅冻结这笔交易关联的尾号 1234 卡片。').waitFor();
  }
  await send(storyA);
  await chooseOriginal();
  await agent.getByRole('button', { name: '仅拒绝冻结，继续查余额' }).click();
  await agent.getByText('任务状态：部分完成', { exact: true }).waitFor();
  await agent.locator('.agent-step').getByText('可用余额', { exact: false }).waitFor();
  task = await current();
  if (task.steps[2].result.available_balance_minor !== 990000 || task.proposal.receipt) throw new Error('拒绝冻结后余额或回执错误');
  const cardStatus = () => page.evaluate(async () => (await (await fetch('/api/cards')).json()).data.find(row => row.id === 'c1a').status);
  if (await cardStatus() !== 'active') throw new Error('拒绝操作仍冻结了卡片');

  await send(storyA);
  await chooseOriginal();
  if (recording) { await agent.locator('.agent-proposal').scrollIntoViewIfNeeded(); await page.waitForTimeout(2000); }
  await agent.getByRole('button', { name: '确认执行', exact: true }).click();
  await agent.locator('.receipt').getByText('卡片已冻结').waitFor();
  await agent.getByText('任务状态：已完成', { exact: true }).waitFor();
  task = await current();
  if (await cardStatus() !== 'frozen' || task.steps[2].result.available_balance_minor !== 990000) throw new Error('冻结和后续余额结果不一致');
  await page.reload({ waitUntil: 'networkidle' });
  await agent.locator('.receipt').getByText('卡片已冻结').waitFor();
  if ((await current()).status !== 'completed') throw new Error('跨场景任务刷新后未恢复');
  mkdirSync('../var/screenshots', { recursive: true });
  await agent.screenshot({ path: `../var/screenshots/m5-${process.env.FINAGENT_MODE || 'mock'}.png` });
  await page.locator('#user').selectOption('u2');
  await page.locator('.account-card > strong').filter({ hasText: '¥6,800.00' }).waitFor();
  if (await agent.locator('.receipt').count() || await agent.locator('.agent-candidate').count()) throw new Error('跨用户泄露任务结果');
  evidence.passed = true;
  evidence.steps = ['billing', 'conditional-transfer-confirm-and-reload', 'ambiguous-transaction-selection', 'decline-freeze-continue-balance', 'freeze-confirm-balance-reload', 'switch-user-isolation'];
  console.log('M5 Edge 通过：条件展示、确认转账、候选交易选择、拒绝冻结继续余额、冻结后查余额、刷新恢复、用户隔离。');
} catch (error) { evidence.error = error.message; throw error;
} finally {
  await browser.close();
  evidence.seconds = (Date.now() - began) / 1000;
  if (recording) writeFileSync(`${recording}/result.json`, JSON.stringify(evidence, null, 2));
}
