import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';

const base = process.env.FINAGENT_E2E_BASE_URL;
if (!base || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base)) throw Error('Isolated server required');
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const context = await browser.newContext();
const page = await context.newPage();
const errors = [], passed = [];
let verifies = 0, confirms = 0;
page.on('pageerror', e => errors.push(e.message));
page.on('request', r => {
  if (r.method() === 'POST' && r.url().endsWith('/verify')) verifies++;
  if (r.method() === 'POST' && /\/proposals\/[^/]+\/confirm$/.test(r.url())) confirms++;
});
const gate = page.getByRole('dialog', { name: '红色 · 独立强验证', exact: true });
const popup = page.getByRole('dialog', { name: '模拟短信验证码', exact: true });
const accounts = () => page.evaluate(async () => (await (await fetch('/api/accounts')).json()).data);
const open = async () => {
  await page.getByRole('button', { name: '重新查询操作状态', exact: true }).click();
  await page.getByRole('button', { name: '核对并继续', exact: true }).last().click();
  await gate.waitFor();
};
const requestSMS = async () => {
  await gate.getByRole('button', { name: /获取模拟短信验证码|重新签发验证码/ }).click();
  await popup.waitFor();
};
try {
  assert.equal((await (await context.request.get(base + '/api/health')).json()).data.test_only, true);
  await page.goto(base);
  await page.getByRole('button', { name: /^林晓/ }).click();
  await page.locator('#agent-message').waitFor();
  const before = await accounts();
  await page.evaluate(async () => {
    const csrf = (await (await fetch('/api/demo/csrf')).json()).data.csrf_token;
    const r = await fetch('/api/transfers/prepare', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-FinAgent-CSRF': csrf },
      body: JSON.stringify({ source_account_id: 'a1c', target: { type: 'own_account', account_id: 'a1s' }, amount_minor: 200000 }) });
    if (!r.ok) throw Error('Prepare failed');
    location.hash = 'tasks/manual';
  });
  await open(); await requestSMS();
  assert.match(await popup.getByLabel('测试验证码', { exact: true }).innerText(), /^\d{6}$/);
  await popup.screenshot({ path: process.env.FINAGENT_E2E_OUTPUT + '/sms-popup-desktop.png' });
  assert.equal(verifies, 0); assert.equal(confirms, 0);
  assert.deepEqual(await accounts(), before);
  for (let i = 0; i < 4; i++) {
    await page.keyboard.press('Tab');
    assert.equal(await popup.evaluate(el => el.contains(document.activeElement)), true);
  }
  passed.push('popup-displays-code-without-execution-and-traps-focus');
  await page.keyboard.press('Escape'); await popup.waitFor({ state: 'hidden' }); await gate.waitFor();
  assert.equal(await gate.getByLabel('六位验证码', { exact: true }).inputValue(), '');
  passed.push('escape-closes-only-sms-window');

  await requestSMS();
  await popup.getByRole('button', { name: '填入验证码', exact: true }).click();
  assert.match(await gate.getByLabel('六位验证码', { exact: true }).inputValue(), /^\d{6}$/);
  assert.equal(verifies, 0); assert.equal(confirms, 0);
  const oldChallenge = (await gate.innerText()).match(/挑战编号：([0-9a-f]{32})/)[1];
  await requestSMS(); await popup.getByRole('button', { name: '关闭', exact: true }).click();
  assert.equal(await gate.getByLabel('六位验证码', { exact: true }).inputValue(), '');
  assert.notEqual((await gate.innerText()).match(/挑战编号：([0-9a-f]{32})/)[1], oldChallenge);
  passed.push('one-click-fill-and-reissue-clears-old-code');

  await page.route('**/simulated-sms', route => route.fulfill({ status: 503, contentType: 'application/json',
    body: JSON.stringify({ status: 'error', error_code: 'SIMULATOR_UNAVAILABLE', message: '模拟短信读取失败，请重新获取验证码' }) }), { times: 1 });
  await gate.getByRole('button', { name: '重新签发验证码', exact: true }).click();
  await gate.getByRole('alert').filter({ hasText: '模拟短信读取失败' }).waitFor();
  assert.equal(await popup.count(), 0);
  await requestSMS(); await popup.getByRole('button', { name: '关闭', exact: true }).click();
  passed.push('delivery-error-visible-and-retry-works');

  await page.route('**/simulated-sms', async route => {
    const response = await route.fetch(); const body = await response.json();
    body.data.expires_at = new Date(Date.now() - 1000).toISOString();
    await route.fulfill({ response, json: body });
  }, { times: 1 });
  await requestSMS(); await popup.getByRole('alert').filter({ hasText: '验证码已过期' }).waitFor();
  assert.equal(await popup.getByRole('button', { name: '填入验证码', exact: true }).isDisabled(), true);
  assert.equal(await popup.getByLabel('测试验证码', { exact: true }).count(), 0);
  await popup.getByRole('button', { name: '关闭', exact: true }).click();
  passed.push('expired-popup-hides-code-and-disables-fill');

  await page.setViewportSize({ width: 320, height: 740 }); await requestSMS();
  const box = await popup.boundingBox(); assert(box.x >= 0 && box.x + box.width <= 320);
  await popup.screenshot({ path: process.env.FINAGENT_E2E_OUTPUT + '/sms-popup-mobile.png' });
  await popup.getByRole('button', { name: '填入验证码', exact: true }).click();
  await gate.getByRole('button', { name: '取消', exact: true }).click();
  await gate.waitFor({ state: 'hidden' }); assert.deepEqual(await accounts(), before);
  passed.push('mobile-popup-fits-and-cancel-preserves-balances');

  await page.setViewportSize({ width: 1280, height: 900 }); await open();
  let release, fetched;
  const responseReady = new Promise(resolve => { fetched = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  await page.route('**/simulated-sms', async route => {
    const response = await route.fetch(); fetched(); await held; await route.fulfill({ response });
  }, { times: 1 });
  await gate.getByRole('button', { name: '获取模拟短信验证码', exact: true }).click();
  await responseReady;
  await page.evaluate(() => window.dispatchEvent(new Event('finagent:session-lost')));
  await gate.waitFor({ state: 'hidden' }); release();
  await page.waitForTimeout(300); assert.equal(await popup.count(), 0);
  passed.push('cancelled-session-ignores-late-code-response');

  await page.reload();
  await open(); await requestSMS(); await popup.getByRole('button', { name: '填入验证码', exact: true }).click();
  await gate.getByRole('button', { name: '确认条款并验证执行', exact: true }).click();
  for (let i = 0; i < 60; i++) {
    if ((await accounts()).find(a => a.id === 'a1c').balance_minor === before.find(a => a.id === 'a1c').balance_minor - 200000) break;
    await page.waitForTimeout(150);
  }
  const after = await accounts();
  assert.equal(after.find(a => a.id === 'a1c').balance_minor, before.find(a => a.id === 'a1c').balance_minor - 200000);
  assert.equal(after.find(a => a.id === 'a1s').balance_minor, before.find(a => a.id === 'a1s').balance_minor + 200000);
  assert.equal(verifies, 1); assert.equal(confirms, 1); assert.deepEqual(errors, []);
  passed.push('explicit-confirm-verifies-and-executes-once');
  console.log(JSON.stringify({ passed, count: passed.length, status: 'passed', database: 'TEST-ONLY' }));
} finally { await context.close(); await browser.close(); }
