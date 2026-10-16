import { chromium } from 'playwright-core';
import { existsSync, mkdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
const root = resolve('..');
const fixture = (action) => {
  const output = execFileSync(resolve(root, '.venv/Scripts/python.exe'), [resolve(root, 'scripts/m6-fixture.py'), action], { cwd: root, encoding: 'utf8', env: process.env });
  return action === 'snapshot' ? JSON.parse(output) : null;
};
const executablePath = [process.env.FINAGENT_BROWSER_PATH,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(path => path && existsSync(path));
const browser = await chromium.launch({ executablePath, headless: true });
mkdirSync('../var/screenshots', { recursive: true });
const story = '从活期账户转到储蓄账户100元';
async function send(page, text) {
  const agent = page.locator('.agent-section');
  await agent.locator('#agent-message').fill(text);
  await agent.getByRole('button', { name: '发送', exact: true }).click();
}
async function prepared(page, text = story) {
  await send(page, text);
  await page.locator('.agent-section').getByRole('button', { name: '确认执行', exact: true }).waitFor();
  await page.waitForFunction(() => !document.querySelector('.agent-proposal .proposal-actions button')?.disabled);
}
async function run(name, body, viewport = { width: 1360, height: 950 }) {
  if (process.env.FINAGENT_M6_CASE && !name.includes(process.env.FINAGENT_M6_CASE)) return;
  fixture('reset');
  const context = await browser.newContext({ viewport, locale: 'zh-CN' });
  const page = await context.newPage();
  page.setDefaultTimeout(90000);
  try {
    await page.goto('http://127.0.0.1:5173', { waitUntil: 'networkidle' });
    await page.locator('#user').selectOption('u1');
    await page.locator('.account-card > strong').filter({ hasText: '¥10,000.00' }).waitFor();
    await body(page, context);
    console.log(`M6 PASS: ${name}`);
  } catch (error) {
    await page.screenshot({ path: `../var/screenshots/m6-failure-${name}.png`, fullPage: true }).catch(() => {});
    throw error;
  } finally { await context.close(); }
}
try {
  await run('lost-response', async (page) => {
    await prepared(page);
    let submits = 0, blockReads = true;
    await page.route('**/api/**', async route => {
      const req = route.request();
      if (req.method() === 'POST' && /\/proposals\/[^/]+\/confirm$/.test(new URL(req.url()).pathname)) {
        submits++; await route.fetch(); await route.abort('failed'); return;
      }
      if (blockReads && req.method() === 'GET' && /\/api\/(agent\/)?tasks\//.test(new URL(req.url()).pathname)) {
        await route.abort('failed'); return;
      }
      await route.continue();
    });
    await page.locator('.agent-section').getByRole('button', { name: '确认执行', exact: true }).click();
    await page.locator('.agent-section .task-sync').getByText('同步已暂停', { exact: false }).waitFor();
    assert.equal(await page.locator('.agent-section').getByRole('button', { name: '确认执行', exact: true }).isDisabled(), true);
    assert.equal(fixture('snapshot').results, 1);
    blockReads = false;
    await page.locator('.agent-section').getByRole('button', { name: '重新查询状态', exact: true }).click();
    await page.locator('.agent-section .receipt').getByText('模拟转账成功').waitFor();
    await page.locator('.account-card > strong').filter({ hasText: '¥9,900.00' }).waitFor();
    assert.equal(submits, 1);
    assert.equal(fixture('snapshot').transfers, 1);
    let reads = 0;
    page.on('request', req => { if (req.method() === 'GET' && req.url().includes('/api/agent/tasks/')) reads++; });
    await page.waitForTimeout(500); const baseline = reads;
    await page.waitForTimeout(4500); assert.equal(reads, baseline, '终态仍在轮询');
    await page.reload({ waitUntil: 'networkidle' });
    await page.locator('.agent-section .receipt').getByText('模拟转账成功').waitFor();
    assert.equal(fixture('snapshot').results, 1);
  });
  await run('portfolio-order', async (page) => {
    await prepared(page);
    let held, release;
    const captured = new Promise(resolve => { held = resolve; });
    const gate = new Promise(resolve => { release = resolve; });
    let once = true;
    await page.route('**/api/accounts', async route => {
      if (!once) { await route.continue(); return; }
      once = false; const response = await route.fetch(); held(); await gate; await route.fulfill({ response });
    });
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await captured;
    await page.locator('.agent-section').getByRole('button', { name: '确认执行', exact: true }).click();
    await page.locator('.account-card > strong').filter({ hasText: '¥9,900.00' }).waitFor();
    release(); await page.waitForTimeout(500);
    assert.equal(await page.locator('.account-card > strong').filter({ hasText: '¥10,000.00' }).count(), 0, '旧余额响应覆盖新余额');
    assert.equal(fixture('snapshot').balances.a1c, 990000);
  });
  await run('same-user-new-session', async (page) => {
    let held, release;
    const captured = new Promise(resolve => { held = resolve; });
    const gate = new Promise(resolve => { release = resolve; });
    let once = true;
    await page.route('**/api/accounts', async route => {
      if (!once) { await route.continue(); return; }
      once = false; held(); await gate;
      await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ status: 'error', data: null, error_code: 'SESSION_EXPIRED', message: '旧会话过期' }) });
    });
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await captured;
    await page.locator('#user').selectOption('u2');
    await page.locator('.account-card > strong').filter({ hasText: '¥6,800.00' }).waitFor();
    await page.locator('#user').selectOption('u1');
    await page.locator('.account-card > strong').filter({ hasText: '¥10,000.00' }).waitFor();
    release(); await page.waitForTimeout(500);
    assert.equal(await page.locator('#user').inputValue(), 'u1');
    assert.equal(await page.locator('.account-card').count(), 2, '旧会话错误清除了同用户的新会话');
  });
  await run('tabs-and-expiry', async (page, context) => {
    await prepared(page);
    const peer = await context.newPage();
    await peer.goto('http://127.0.0.1:5173', { waitUntil: 'networkidle' });
    await peer.locator('.agent-section').getByRole('button', { name: '取消整个任务' }).click();
    await page.locator('.agent-section').getByText('任务状态：已取消', { exact: true }).waitFor();
    assert.equal(fixture('snapshot').results, 0);
    await peer.close();
    await prepared(page);
    fixture('expire');
    await page.locator('.agent-section').getByText('任务状态：已过期', { exact: true }).waitFor();
    assert.equal(await page.locator('.agent-section').getByRole('button', { name: '确认执行', exact: true }).count(), 0);
    assert.equal(fixture('snapshot').results, 0);
  });
  await run('old-read-and-user-switch', async (page, context) => {
    await prepared(page);
    let held, release;
    const captured = new Promise(resolve => { held = resolve; });
    const gate = new Promise(resolve => { release = resolve; });
    let once = true;
    await page.route('**/api/agent/tasks/current', async route => {
      if (!once) { await route.continue(); return; }
      once = false; const response = await route.fetch(); held(); await gate; await route.fulfill({ response });
    });
    await captured;
    await page.locator('.agent-section').getByRole('button', { name: '取消整个任务' }).click();
    await page.locator('.agent-section').getByText('任务状态：已取消', { exact: true }).waitFor();
    release(); await page.waitForTimeout(500);
    assert.equal(await page.locator('.agent-section').getByRole('button', { name: '确认执行', exact: true }).count(), 0);
    await page.unroute('**/api/agent/tasks/current');
    const peer = await context.newPage();
    await peer.goto('http://127.0.0.1:5173', { waitUntil: 'networkidle' });
    await peer.locator('#user').selectOption('u2');
    await peer.locator('.account-card > strong').filter({ hasText: '¥6,800.00' }).waitFor();
    await page.locator('.agent-section').getByRole('button', { name: '重新查询状态', exact: true }).click();
    await page.getByRole('alert').getByText('其他标签页已切换用户', { exact: false }).waitFor();
    assert.equal(await page.locator('.account-card').count(), 0);
    assert.equal(await page.locator('.agent-section').count(), 0);
  });
  await run('changed-condition-and-chart', async (page) => {
    await prepared(page, '如果本月餐饮超过500元，就从活期转100元到储蓄账户。');
    fixture('refund');
    await page.locator('.agent-section').getByRole('button', { name: '确认执行', exact: true }).click();
    await page.locator('.agent-section').getByText('任务状态：已完成', { exact: true }).waitFor();
    await page.locator('.agent-proposal .condition-details').getByText('¥400.00', { exact: false }).waitFor();
    const state = fixture('snapshot'); assert.equal(state.results, 0); assert.equal(state.balances.a1c, 1080000);
    const spending = page.locator('.spending-section');
    await spending.getByLabel('类别', { exact: true }).selectOption('全部');
    await spending.getByRole('button', { name: '查询', exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('.chart-row').length > 1);
    await spending.locator('.spending-chart').screenshot({ path: '../var/screenshots/m6-chart.png' });
    await spending.getByLabel('开始日期', { exact: true }).fill('2026-09-24');
    await spending.getByLabel('结束日期', { exact: true }).fill('2026-09-24');
    await spending.getByLabel('类别', { exact: true }).selectOption('餐饮');
    await spending.getByRole('button', { name: '查询', exact: true }).click();
    await spending.locator('.chart-bar.negative').waitFor();
    await spending.locator('.chart-label').getByText('-¥800.00').waitFor();
    fixture('incomplete');
    await spending.getByRole('button', { name: '查询', exact: true }).click();
    await spending.locator('figcaption').getByText('仅展示已知小计，数据不完整').waitFor();
    await page.locator('.agent-proposal').evaluate(el => el.scrollIntoView({ block: 'start' }));
    await page.screenshot({ path: '../var/screenshots/m6-desktop.png' });
  });
  await run('mobile-keyboard', async (page) => {
    const agent = page.locator('.agent-section');
    await agent.locator('#agent-message').fill(story);
    await agent.locator('#agent-message').press('Enter');
    const confirm = agent.getByRole('button', { name: '确认执行', exact: true });
    await confirm.waitFor();
    await page.waitForFunction(() => !document.querySelector('.agent-proposal .proposal-actions button')?.disabled);
    await confirm.focus(); await page.keyboard.press('Enter');
    await agent.locator('.receipt').getByText('模拟转账成功').waitFor();
    assert.equal(fixture('snapshot').balances.a1c, 990000);
    await prepared(page, '冻结尾号1234的卡');
    await agent.locator('.agent-proposal').evaluate(el => el.scrollIntoView({ block: 'start' }));
    await page.screenshot({ path: '../var/screenshots/m6-mobile-confirm.png' });
    const cancel = agent.getByRole('button', { name: '取消整个任务' });
    await cancel.focus(); await page.keyboard.press('Enter');
    await agent.getByText('任务状态：已取消', { exact: true }).waitFor();
    assert.equal(fixture('snapshot').cards.c1a, 'active');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, '窄屏水平溢出');
    await agent.locator('.agent-proposal').evaluate(el => el.scrollIntoView({ block: 'start' }));
    await page.screenshot({ path: '../var/screenshots/m6-mobile.png' });
    await page.locator('.manual-form summary').focus(); await page.keyboard.press('Enter');
    assert.equal(await page.locator('.manual-form').getAttribute('open') !== null, true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  }, { width: 360, height: 800 });
} finally { await browser.close(); }
