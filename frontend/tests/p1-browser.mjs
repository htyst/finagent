import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';
import { readFile, mkdir } from 'node:fs/promises';
import path from 'node:path';

const base = process.env.FINAGENT_E2E_BASE_URL;
const output = process.env.FINAGENT_E2E_OUTPUT;
if (!base || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base) || !output || !output.includes('TEST-ONLY')) throw Error('Isolated server and output required');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const context = await browser.newContext();
const page = await context.newPage();
const errors = [], passed = [];
page.on('pageerror', e => errors.push(e.message));
async function state() { return (await (await context.request.get(base + '/api/agent/tasks/current')).json()).data; }
async function chat(text) {
  await page.evaluate(() => { location.hash='assistant'; });
  await page.locator('#agent-message').fill(text);
  const response=page.waitForResponse(r=>r.request().method()==='POST' && /\/api\/tasks(?:\/[^/]+\/messages)?$/.test(new URL(r.url()).pathname));
  await page.locator('#assistant').getByRole('button', { name: '发送', exact: true }).click();
  assert.equal((await response).status(),200);
  for (let i=0;i<80;i++) { const s=await state(); if(s?.messages?.some(m=>m.role==='user' && m.content===text) && s.messages.at(-1)?.role==='assistant') return s; await page.waitForTimeout(150); }
  throw Error('Chat response not recovered: '+text);
}
async function cancelTask() {
  const task=await state();
  if(task && !['completed','cancelled','failed','expired'].includes(task.status)) {
    await page.evaluate(async id => { const csrf=(await (await fetch('/api/demo/csrf')).json()).data.csrf_token; const r=await fetch(`/api/tasks/${id}/cancel`,{method:'POST',headers:{'X-FinAgent-CSRF':csrf}}); if(!r.ok)throw Error('Cancel failed'); },task.id);
    await page.reload(); await page.locator('#agent-message').waitFor();
  }
}
async function yellowConfirm(button) {
  await button.click(); const dialog=page.getByRole('dialog'); await dialog.waitFor();
  await dialog.getByRole('button',{name:'确认条款并执行',exact:true}).click();
  await dialog.waitFor({state:'hidden'});
}
try {
  assert.equal((await (await context.request.get(base+'/api/health')).json()).data.test_only,true);
  await page.goto(base); await page.locator('#user').selectOption('u1'); await page.locator('#agent-message').waitFor();
  let task=await chat('从活期账户转100元给张三');
  assert.equal(task.status,'gathering_info'); assert.equal(task.proposal,null); passed.push('transfer-ambiguity');
  await cancelTask();
  task=await chat('从活期账户转100元给13800000001'); assert.equal(task.proposal.summary.target.phone_mask,'138****0001');
  await page.reload(); await page.locator('#agent-message').waitFor();
  assert.equal((await state()).proposal.id,task.proposal.id); passed.push('transfer-reload');
  await page.route('**/api/proposals/*/confirm', async route => { await route.fetch(); await route.abort('failed'); });
  await yellowConfirm(page.locator('#assistant').getByRole('button',{name:'核对并继续',exact:true}));
  await page.getByText('模拟转账成功',{exact:true}).first().waitFor(); passed.push('transfer-phone-success','transfer-lost-response-recovered');
  await page.unroute('**/api/proposals/*/confirm');
  for (const [text, target] of [['从活期账户转100元给李文','r3'],['从活期账户转100元给备注房东','r_phone_demo']]) {
    task=await chat(text); assert.equal(task.proposal.summary.target.id,target);
    await yellowConfirm(page.locator('#assistant').getByRole('button',{name:'核对并继续',exact:true}));
    for(let i=0;i<60 && (await state()).proposal.status!=='executed';i++)await page.waitForTimeout(150);
    assert.equal((await state()).proposal.status,'executed');
    passed.push('transfer-'+target+'-success');
  }
  task=await chat('从活期账户转100元，收款户名合成测试人，开户银行测试银行，收款账号6222000000000000000');
  assert.equal(task.proposal.summary.target.mask,'**0000');
  await yellowConfirm(page.locator('#assistant').getByRole('button',{name:'核对并继续',exact:true}));
  for(let i=0;i<60 && (await state()).proposal.status!=='executed';i++)await page.waitForTimeout(150);
  assert.equal((await state()).proposal.status,'executed'); passed.push('transfer-new-account-success');
  task=await chat('从活期账户转6000元到储蓄账户'); assert.equal(task.proposal,null); assert.notEqual(task.status,'completed'); passed.push('transfer-limit-rejected');
  await cancelTask();
  // Assessment, clarification and risk rejection are visible in the same UI used by users.
  task=await chat('申购P1'); assert.equal(task.status,'gathering_info'); passed.push('wealth-missing-slots'); await cancelTask();
  await page.evaluate(() => {location.hash='assets/wealth';});
  const assessment=page.locator('.risk-questionnaire');
  await assessment.locator('select').first().waitFor();
  for(let i=0;i<4;i++) await assessment.locator('select').nth(i).selectOption('0');
  await assessment.getByRole('button',{name:'提交风险评估'}).click(); await page.getByText('有效风险评估：R1',{exact:true}).waitFor();
  task=await chat('从活期账户申购100元P3'); assert.equal(task.wealth_order,null); assert.match(task.messages.at(-1).content,/风险/); passed.push('wealth-risk-rejected'); await cancelTask();
  task=await chat('从活期账户申购100元P1'); assert.equal(task.wealth_order.status,'pending_confirmation');
  await page.reload(); await page.locator('#agent-message').waitFor(); assert.equal((await state()).wealth_order.id,task.wealth_order.id); passed.push('wealth-reload');
  await page.evaluate(() => {location.hash='assets/wealth';});
  await page.getByRole('button',{name:'确认理财交易',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'红色 · 独立强验证',exact:true}); await dialog.waitFor(); assert.match(await dialog.innerText(),/红色/);
  await dialog.getByRole('button',{name:'获取模拟短信验证码'}).click(); await dialog.getByText(/挑战编号：/).waitFor();
  const id=(await dialog.innerText()).match(/挑战编号：([0-9a-f]{32})/)[1];
  const code=JSON.parse(await readFile(path.resolve(import.meta.dirname,'../../var/auth-simulator',id+'.json'),'utf8')).code;
  await page.getByRole('dialog',{name:'模拟短信验证码',exact:true}).getByRole('button',{name:'填入验证码',exact:true}).click();
  assert.equal(await dialog.getByLabel('六位验证码',{exact:true}).inputValue(),code); await dialog.getByRole('button',{name:'确认条款并验证执行',exact:true}).click();
  await page.locator('.wealth-receipt').waitFor(); passed.push('wealth-red-success');
  task=await chat('申请一张虚拟卡'); assert.equal(task.status,'gathering_info'); passed.push('card-missing-info'); await cancelTask();
  task=await chat('申请一张虚拟卡，申请人是本人，关联活期账户'); assert.equal(task.card_request.status,'pending_confirmation');
  await page.evaluate(() => {location.hash='assets/cards';});
  await yellowConfirm(page.getByRole('button',{name:'确认卡片办理',exact:true}));
  await page.getByText(/申请已受理，尚未发卡/).waitFor(); passed.push('card-user-submission');
  const rid=task.card_request.id;
  const denied=await page.evaluate(async rid=>{const csrf=(await(await fetch('/api/demo/csrf')).json()).data.csrf_token;return (await fetch(`/api/card-management/requests/${rid}/approve`,{method:'POST',headers:{'X-FinAgent-CSRF':csrf}})).status;},rid);
  assert.equal(denied,403); passed.push('card-user-cannot-approve');
  await page.reload(); await page.locator('#card-management').waitFor(); assert.equal((await state()).card_request.status,'submitted'); passed.push('card-reload');
  await page.evaluate(() => {location.hash='demo/cards';});
  await page.getByText('本机独立模拟审批',{exact:true}).click();
  await page.getByLabel('模拟审批员凭据',{exact:true}).fill('wrong-test-only-key');
  await page.getByRole('button',{name:'模拟审批通过并发卡',exact:true}).click();
  await page.getByText(/须由持独立本机审批凭据的模拟审批员处理/).waitFor(); passed.push('card-wrong-approval-rejected-in-ui');
  await page.getByLabel('模拟审批员凭据',{exact:true}).fill(process.env.FINAGENT_E2E_APPROVER_KEY);
  await page.getByRole('button',{name:'模拟审批通过并发卡',exact:true}).click();
  await page.getByText('已记录独立模拟审批，请核对发卡回执。',{exact:true}).waitFor();
  await page.evaluate(() => {location.hash='assets/cards';});
  await page.locator('.card-receipt').getByText('模拟审批通过，已发卡',{exact:true}).waitFor(); passed.push('card-independent-approval-and-issuance');
  assert.deepEqual(errors,[]);
  await page.screenshot({path:path.join(output,'p1-three-scenes.png'),fullPage:true});
  console.log(JSON.stringify({passed,denominator:passed.length,status:'passed',mode:'mock',real_model:false}));
} catch(error) {
  await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});
  console.error(JSON.stringify({passed,status:'failed',error:String(error)})); throw error;
} finally { await context.close(); await browser.close(); }
