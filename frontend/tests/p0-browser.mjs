import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';
import { readFile, unlink } from 'node:fs/promises';
import path from 'node:path';

// Use scripts/test-p0.ps1 with an isolated test instance. Never run financial
// browser tests against the normal demo database or a public banking service.
const baseURL = process.env.FINAGENT_E2E_BASE_URL;
if (!baseURL || !/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(baseURL)) throw Error('FINAGENT_E2E_BASE_URL must identify the isolated local test server');
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const context = await browser.newContext();
const page = await context.newPage();
const errors = [];
let simulatedInbox;
page.on('pageerror', error => errors.push(error.message));
try {
  const health = await (await context.request.get(baseURL + '/api/health')).json();
  assert.equal(health.data.test_only, true, 'Refusing mutations outside TEST-ONLY database');
  await page.goto(baseURL);
  await page.locator('#user').selectOption('u1');
  await page.locator('#agent-message').waitFor();
  const initial = await page.evaluate(async () => (await (await fetch('/api/accounts')).json()).data);
  assert(initial.length >= 2);
  await page.evaluate(() => { location.hash = 'assets/subscriptions'; });
  await page.getByRole('button', { name: '明确确认订阅查询', exact: true }).click();
  await page.getByText('订阅查询已确认', { exact: true }).waitFor();
  await page.evaluate(() => { location.hash = 'tasks/manual'; });
  await page.getByRole('button', {name:'重新查询操作状态',exact:true}).waitFor();
  const proposal = await page.evaluate(async () => {
    const token = (await (await fetch('/api/demo/csrf')).json()).data.csrf_token;
    const result = await fetch('/api/transfers/prepare', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-FinAgent-CSRF': token },
      body: JSON.stringify({ source_account_id: 'a1c', target: { type: 'own_account', account_id: 'a1s' }, amount_minor: 10000 }) });
    return (await result.json()).data.proposal;
  });
  assert(proposal.id);
  await page.getByRole('button', { name: '重新查询操作状态', exact: true }).click();
  await page.getByRole('button', { name: '核对并继续', exact: true }).last().click();
  await page.getByRole('dialog').waitFor();
  assert.match(await page.getByRole('dialog').innerText(), /黄色/);
  await page.getByRole('dialog').getByRole('button', { name: '确认条款并执行', exact: true }).click();
  await page.getByText('模拟转账成功', { exact: true }).first().waitFor();
  const committed = await page.evaluate(async () => (await (await fetch('/api/accounts')).json()).data);
  assert.equal(committed.find(a => a.id === 'a1c').balance_minor, initial.find(a => a.id === 'a1c').balance_minor - 10000);
  // Exercise the red popup using the OS-local simulator, never chat or an API
  // returning a code. The TEST-ONLY health check above is mandatory.
  await page.evaluate(async () => {
    const csrf = (await (await fetch('/api/demo/csrf')).json()).data.csrf_token;
    const response = await fetch('/api/transfers/prepare', { method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-FinAgent-CSRF': csrf },
      body: JSON.stringify({ source_account_id: 'a1c', target: { type: 'own_account', account_id: 'a1s' }, amount_minor: 200000 }) });
    if (!response.ok) throw Error('Red test draft preparation failed');
  });
  await page.getByRole('button', { name: '重新查询操作状态', exact: true }).click();
  await page.getByRole('button', { name: '核对并继续', exact: true }).last().click();
  const dialog = page.getByRole('dialog', { name: '红色 · 独立强验证', exact: true });
  await dialog.waitFor();
  assert.match(await dialog.innerText(), /红色/);
  await dialog.getByRole('button', { name: '获取模拟短信验证码', exact: true }).click();
  await dialog.getByText(/挑战编号：/).waitFor();
  const identifier = (await dialog.innerText()).match(/挑战编号：([0-9a-f]{32})/)?.[1];
  assert(identifier, 'Simulator challenge identifier must be visible');
  simulatedInbox = path.resolve(import.meta.dirname, '../../var/auth-simulator', identifier + '.json');
  const simulated = JSON.parse(await readFile(simulatedInbox, 'utf8'));
  assert.match(simulated.code, /^\d{6}$/);
  const popup = page.getByRole('dialog', { name: '模拟短信验证码', exact: true });
  await popup.getByRole('button', { name: '填入验证码', exact: true }).click();
  assert.equal(await dialog.getByLabel('六位验证码', { exact: true }).inputValue(), simulated.code);
  await dialog.getByRole('button', { name: '确认条款并验证执行', exact: true }).click();
  let afterRed;
  for (let attempt = 0; attempt < 60; attempt++) {
    afterRed = await page.evaluate(async () => (await (await fetch('/api/accounts')).json()).data);
    if (afterRed.find(a => a.id === 'a1c').balance_minor === initial.find(a => a.id === 'a1c').balance_minor - 210000) break;
    await page.waitForTimeout(250);
  }
  assert.equal(afterRed.find(a => a.id === 'a1c').balance_minor, initial.find(a => a.id === 'a1c').balance_minor - 210000);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ browser: 'Edge', scenarios: 4, passed: true, note: 'session/read, yellow subscription query, yellow transfer, red simulator popup and confirmed balances' }));
} finally {
  await context.close(); await browser.close();
  if (simulatedInbox) await unlink(simulatedInbox).catch(() => {});
}
