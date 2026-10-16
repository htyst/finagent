import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { chromium } from 'playwright-core';
const baseURL = process.env.FINAGENT_TEST_BASE_URL;
if (!baseURL) throw new Error('请提供隔离测试服务器 FINAGENT_TEST_BASE_URL');
const executablePath = ['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(existsSync);
const browser = await chromium.launch({ executablePath, headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto(baseURL);
  await page.locator('#user').selectOption('u1');
  const panel = page.locator('.agent-section');
  await panel.getByRole('button', { name: '重新查询状态' }).waitFor();
  async function send(text) {
    await panel.locator('#agent-message').fill(text);
    const response = page.waitForResponse(r => /\/api\/(tasks|tasks\/[^/]+\/messages)$/.test(new URL(r.url()).pathname) && r.request().method() === 'POST');
    await panel.getByRole('button', { name: '发送', exact: true }).click();
    const body = await (await response).json();
    assert.equal(body.status, 'ok');
    await panel.locator('#agent-message').filter({ hasText: '' }).waitFor();
    await page.waitForFunction(() => document.querySelector('#agent-message').value === '');
    return body.data;
  }
  let task = await send('介绍一下你的功能');
  assert.equal(task.steps.length, 0);
  task = await send('查询');
  assert.equal(task.steps.length, 0);
  task = await send('我想去日本玩，你在资金消费方面有什么建议');
  assert.equal(task.steps.length, 0);
  task = await send('两个人7天，总预算一万元');
  assert.match(task.messages.at(-1).content, /214.29/);
  task = await send('这个月吃饭花了多少');
  assert.equal(task.steps[0].tool_name, 'summarize_spending');
  task = await send('那上个月呢');
  assert.match(task.messages.at(-1).content, /800.00/);
  assert.equal(task.messages.length, 12);
  await page.reload();
  await panel.locator('.agent-reply').filter({ hasText: '800.00' }).waitFor();
  assert.equal(await panel.locator('.agent-user').count(), 6);
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: '../var/conversation-review/browser.png', fullPage: true });
  await page.locator('#user').selectOption('u2');
  await panel.locator('.agent-placeholder').waitFor();
  assert.equal(await panel.locator('.agent-user').count(), 0);
  console.log('Conversation Edge LIVE passed: reported examples, exact budget, follow-up, reload and user isolation.');
} finally { await browser.close(); }
