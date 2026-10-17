import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';

const base = process.env.FINAGENT_E2E_BASE_URL;
if (!base || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base)) throw Error('Isolated test services required');
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const context = await browser.newContext(), page = await context.newPage();
const errors = [], passed = [];
page.on('pageerror', e => errors.push(e.message));
async function read(route) { const r = await context.request.get(base + route); assert.equal(r.status(), 200); return (await r.json()).data; }
async function post(route, data) {
  const csrf = (await read('/api/demo/csrf')).csrf_token;
  return context.request.post(base + route, { headers: { Origin: base, 'X-FinAgent-CSRF': csrf }, ...(data ? { data } : {}) });
}
async function manual() {
  await page.evaluate(() => { location.hash = 'tasks/manual'; });
  await page.getByRole('button', { name: '重新查询操作状态', exact: true }).click();
}
async function confirm(red = false) {
  await page.getByRole('button', { name: '核对并继续', exact: true }).last().click();
  const dialog = page.getByRole('dialog', { name: red ? '红色 · 独立强验证' : '黄色 · 明确确认', exact: true });
  await dialog.waitFor();
  if (red) {
    await dialog.getByRole('button', { name: '获取模拟短信验证码', exact: true }).click();
    await page.getByRole('dialog', { name: '模拟短信验证码', exact: true }).getByRole('button', { name: '填入验证码', exact: true }).click();
  }
  await dialog.getByRole('button', { name: red ? '确认条款并验证执行' : '确认条款并执行', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
}
async function waitAccount(id, predicate) {
  for (let i = 0; i < 80; i++) { const row = (await read('/api/accounts')).find(a => a.id === id); if (predicate(row)) return row; await page.waitForTimeout(100); }
  throw Error('Account did not reach expected state');
}
try {
  assert.equal((await read('/api/health')).test_only, true);
  await page.goto(base); await page.getByRole('button', { name: /^林晓/ }).click();
  await page.locator('#agent-message').waitFor();
  const added = await post('/api/cards', { alias: '工资卡', bank_name: '合成银行', card_number: '6222000000001011', opening_balance_minor: 200000 });
  assert.equal(added.status(), 201); const wage = (await added.json()).data;
  await page.reload(); await page.locator('#agent-message').waitFor();
  await manual();
  assert.equal((await post(`/api/cards/${wage.id}/actions/prepare`, { action: 'freeze' })).status(), 200);
  await manual(); await confirm();
  const frozen = await waitAccount(wage.account_id, a => a.status === 'frozen');
  assert.equal(frozen.balance_minor, 200000); assert.equal(frozen.available_balance_minor, 0); assert.equal(frozen.debit_blocked, true);
  passed.push('freezing-new-card-suspends-account-debits');

  await page.evaluate(() => { location.hash = 'assets/accounts'; });
  const accountCard = page.locator('.account-card').filter({ hasText: '工资卡账户' });
  await accountCard.getByText('已冻结', { exact: true }).waitFor();
  await accountCard.getByText(/账户已冻结，暂停转账/).waitFor();
  passed.push('assets-display-frozen-state-and-explanation');
  await page.screenshot({ path: process.env.FINAGENT_E2E_OUTPUT + '/frozen-account.png', fullPage: true });

  await manual(); await page.getByText('手动填写操作（可选）', { exact: true }).click();
  const source = page.getByLabel('付款账户', { exact: true });
  assert.equal(await source.locator(`option[value="${wage.account_id}"]`).count(), 0);
  await source.selectOption('a1c');
  assert.equal(await page.getByLabel('收款目标', { exact: true }).locator(`option[value="${wage.account_id}"]`).count(), 1);
  passed.push('frozen-account-hidden-for-payment-but-kept-for-receipts');
  const denied = await post('/api/transfers/prepare', { source_account_id: wage.account_id,
    target: { type: 'own_account', account_id: 'a1s' }, amount_minor: 1000 });
  assert.equal(denied.status(), 409); assert.equal((await denied.json()).error_code, 'ACCOUNT_FROZEN');
  assert.equal((await read('/api/accounts')).find(a => a.id === wage.account_id).balance_minor, 200000);
  passed.push('direct-api-cannot-bypass-disabled-payment-choice');

  assert.equal((await post('/api/transfers/prepare', { source_account_id: 'a1c',
    target: { type: 'own_account', account_id: wage.account_id }, amount_minor: 1000 })).status(), 200);
  await manual(); await confirm();
  const credited = await waitAccount(wage.account_id, a => a.balance_minor === 201000);
  assert.equal(credited.status, 'frozen'); assert.equal(credited.available_balance_minor, 0);
  passed.push('frozen-account-still-receives-funds');

  await page.reload(); await manual();
  assert.equal((await read('/api/accounts')).find(a => a.id === wage.account_id).status, 'frozen');
  passed.push('reload-preserves-freeze-restrictions');
  assert.equal((await post(`/api/cards/${wage.id}/actions/prepare`, { action: 'unfreeze' })).status(), 200);
  await manual(); await confirm(true);
  const unfrozen = await waitAccount(wage.account_id, a => a.status === 'active');
  assert.equal(unfrozen.available_balance_minor, 201000);
  await page.reload(); await manual(); await page.getByText('手动填写操作（可选）', { exact: true }).click();
  assert.equal(await source.locator(`option[value="${wage.account_id}"]`).count(), 1);
  passed.push('verified-unfreeze-restores-payment-choice');
  assert.equal((await post('/api/transfers/prepare', { source_account_id: wage.account_id,
    target: { type: 'own_account', account_id: 'a1s' }, amount_minor: 1000 })).status(), 200);
  await manual(); await confirm(); await waitAccount(wage.account_id, a => a.balance_minor === 200000);
  passed.push('new-transfer-succeeds-after-unfreeze');
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed, count: passed.length, status: 'passed', database: 'TEST-ONLY' }));
} finally { await context.close(); await browser.close(); }
