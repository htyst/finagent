import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';
const base=process.env.FINAGENT_E2E_BASE_URL;
if(!base||!/^http:\/\/127\.0\.0\.1:\d+$/.test(base))throw Error('Isolated TEST-ONLY services required');
const browser=await chromium.launch({channel:'msedge',headless:true});
const context=await browser.newContext(),page=await context.newPage();
const errors=[],posts=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('request',r=>{if(r.method()==='POST')posts.push(new URL(r.url()).pathname);});
async function read(route){const r=await context.request.get(base+route);assert.equal(r.status(),200);return (await r.json()).data;}
try{
  assert.equal((await read('/api/health')).test_only,true);
  await page.goto(base);await page.locator('#user').selectOption('u1');await page.locator('#agent-message').waitFor();
  const before=await read('/api/accounts');
  await page.locator('#agent-message').fill('从活期账户转2000元到储蓄账户');
  await page.getByRole('button',{name:'发送',exact:true}).click();
  const card=page.locator('#assistant .conversation-authorization[data-operation-kind="proposal"]');
  await card.getByRole('button',{name:'核对并继续',exact:true}).click();
  const task=await read('/api/agent/tasks/current');
  const path=task.authorization_operations.find(o=>o.resource_kind==='proposal').authorization_path;
  const gate=page.getByRole('dialog',{name:'红色 · 独立强验证',exact:true});
  await gate.getByRole('button',{name:'获取模拟短信验证码',exact:true}).click();
  const popup=page.getByRole('dialog',{name:'模拟短信验证码',exact:true});
  await popup.getByRole('button',{name:'填入验证码',exact:true}).click();
  assert.match(await gate.getByLabel('六位验证码',{exact:true}).inputValue(),/^\d{6}$/);
  assert.equal(posts.filter(p=>p.endsWith('/simulated-sms')).length,1);
  assert(posts.includes(path+'/simulated-sms'));
  assert.deepEqual(await read('/api/accounts'),before);
  const receipt=page.waitForResponse(r=>r.request().method()==='POST'&&new URL(r.url()).pathname===path+'/confirm');
  await gate.getByRole('button',{name:'确认条款并验证执行',exact:true}).click();
  assert.equal((await receipt).status(),200);
  await page.getByText('模拟转账成功',{exact:true}).first().waitFor();
  const after=await read('/api/accounts');
  assert.equal(after.find(a=>a.id==='a1c').balance_minor,before.find(a=>a.id==='a1c').balance_minor-200000);
  assert.equal(after.find(a=>a.id==='a1s').balance_minor,before.find(a=>a.id==='a1s').balance_minor+200000);
  assert.equal(posts.filter(p=>p===path+'/confirm').length,1);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({status:'passed',scenarios:3,checks:['task-bound-popup-and-one-click-fill','no-payment-before-explicit-confirm','single-confirm-exact-balance-change'],real_model:false,errors}));
}finally{await context.close();await browser.close();}
