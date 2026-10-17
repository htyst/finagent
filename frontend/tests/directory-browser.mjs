import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

const base = process.env.FINAGENT_E2E_BASE_URL, output = process.env.FINAGENT_E2E_OUTPUT;
if (!base || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base) || !output?.includes('TEST-ONLY')) throw Error('Isolated services required');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const context = await browser.newContext({ viewport: { width: 1366, height: 900 } }), page = await context.newPage();
const errors = [], passed = [];
page.on('pageerror', e => errors.push(e.message));
async function data(route) { const response = await context.request.get(base + route); assert.equal(response.status(), 200); return (await response.json()).data; }
async function nav(target) { await page.evaluate(target => { location.hash = target; }, target); }
async function screenshot(name) { await page.screenshot({ path: path.join(output, name + '.png'), fullPage: true }); }
try {
  assert.equal((await data('/api/health')).test_only, true);
  assert.deepEqual((await data('/api/demo/users')).map(u => u.id), ['u1', 'u2', 'u3']);
  await page.goto(base);
  assert.equal(await page.locator('#user').count(), 1);
  await page.getByRole('button', { name: /^林晓/ }).click();
  await page.locator('#agent-message').waitFor(); passed.push('multi-user-entry-preserved');

  await nav('assets/cards'); await page.locator('.directory-add summary').click();
  let form = page.getByRole('form', { name: '添加银行卡' });
  await form.getByLabel('卡片名称', { exact: true }).fill('旅行卡');
  await form.getByLabel('模拟银行卡号').fill('6222000000008899');
  await form.getByLabel('新账户名称').fill('旅行账户');
  await form.getByLabel('初始模拟余额（元）', { exact: true }).fill('1234.56');
  await form.getByRole('button', { name: '添加银行卡', exact: true }).click();
  await page.getByText('银行卡已添加，可用于转账、冻结及模拟消费。', { exact: true }).waitFor();
  let cards = await data('/api/cards'), accounts = await data('/api/accounts');
  const card = cards.find(c => c.alias === '旅行卡'), account = accounts.find(a => a.alias === '旅行账户');
  assert(card && account && card.account_id === account.id); assert.equal(account.balance_minor, 123456);
  passed.push('new-card-creates-usable-account'); await screenshot('cards-added');

  await form.getByLabel('卡片名称', { exact: true }).fill('活期副卡');
  await form.getByLabel('模拟银行卡号').fill('6222000000008877');
  await form.getByLabel('关联账户').selectOption('a1c');
  await form.getByRole('button', { name: '添加银行卡', exact: true }).click();
  await page.waitForFunction(async () => (await (await fetch('/api/cards')).json()).data.some(c => c.alias === '活期副卡'));
  assert.equal((await data('/api/accounts')).length, 3); passed.push('card-can-use-existing-account');
  await form.getByLabel('卡片名称', { exact: true }).fill('重复卡');
  await form.getByLabel('模拟银行卡号').fill('6222000000008877');
  await form.getByRole('button', { name: '添加银行卡', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: '这张银行卡已添加' }).waitFor();
  assert.equal((await data('/api/cards')).length, 4); passed.push('duplicate-card-does-not-create-data');

  await nav('assets/recipients'); await page.locator('.directory-add summary').click();
  form = page.getByRole('form', { name: '添加收款人' });
  await form.getByLabel('收款人姓名').fill('测试收款人'); await form.getByLabel('收款账号', { exact: true }).fill('6222000000007788');
  await form.getByLabel('收款人手机号（可选）', { exact: true }).fill('13900000002');
  await form.getByLabel('收款人备注（可选）', { exact: true }).fill('旅行伙伴');
  await form.getByRole('button', { name: '添加收款人', exact: true }).click();
  await page.getByText('收款人已添加，可以按姓名、手机号或备注发起转账。', { exact: true }).waitFor();
  const recipient = (await data('/api/recipients')).find(r => r.name === '测试收款人');
  assert.equal(recipient.remark, '旅行伙伴'); assert.equal(recipient.phone_mask, '139****0002');
  await screenshot('recipient-added'); passed.push('recipient-persists-and-is-masked');
  await page.reload(); await page.getByRole('heading', { name: '收款人管理', exact: true }).waitFor();
  await page.getByText('测试收款人 · **7788', { exact: false }).waitFor(); passed.push('reload-keeps-added-directory');

  await nav('tasks/manual'); await page.getByText('手动填写操作（可选）', { exact: true }).click();
  await page.getByLabel('付款账户').selectOption(account.id);
  await page.getByLabel('目标类型').selectOption('saved_recipient');
  const target = page.locator('.operation-form select').filter({ has: page.locator(`option[value="${recipient.id}"]`) });
  await target.selectOption(recipient.id); await page.getByLabel('金额（元）', { exact: true }).fill('10');
  await page.getByRole('button', { name: '准备操作单', exact: true }).click();
  await page.getByRole('button', { name: '核对并继续', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '确认条款并执行', exact: true }).click();
  await page.getByText('模拟转账成功', { exact: true }).waitFor();
  assert.equal((await data('/api/accounts')).find(a => a.id === account.id).balance_minor, 122456);
  passed.push('added-account-and-recipient-transfer-end-to-end');

  for (const route of ['assets/cards', 'assets/recipients']) {
    await page.setViewportSize({ width: 390, height: 844 }); await nav(route);
    await page.locator('.directory-add summary').click();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await screenshot(route.endsWith('cards') ? 'cards-mobile' : 'recipients-mobile');
  }
  passed.push('mobile-directory-forms-fit'); assert.deepEqual(errors, []);
  await writeFile(path.join(output, 'result.json'), JSON.stringify({ status: 'passed', passed, errors }, null, 2));
  console.log(JSON.stringify({ status: 'passed', passed, errors }));
} catch (error) { await screenshot('failure').catch(() => {}); console.error(JSON.stringify({ passed, errors, error: String(error) })); throw error; }
finally { await context.close(); await browser.close(); }
