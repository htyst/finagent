import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

const base = process.env.FINAGENT_E2E_BASE_URL, output = process.env.FINAGENT_E2E_OUTPUT;
if (!base || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base) || !output?.includes('TEST-ONLY')) throw Error('Isolated services required');
await mkdir(output,{recursive:true});
const browser = await chromium.launch({channel:'msedge',headless:true});
const context = await browser.newContext({viewport:{width:1366,height:900}}), page = await context.newPage();
const errors=[], passed=[];page.on('pageerror',error=>errors.push(error.message));
async function read(route){const r=await context.request.get(base+route);assert.equal(r.status(),200);return (await r.json()).data;}
async function post(route,payload){const csrf=(await read('/api/demo/csrf')).csrf_token;return context.request.post(base+route,{headers:{Origin:base,'X-FinAgent-CSRF':csrf},...(payload?{data:payload}:{})});}
async function nav(route){await page.evaluate(route=>{location.hash=route;},route);}
async function home(){await page.getByRole('heading',{name:'把繁琐的金融操作，交给一句话。',exact:true}).waitFor();}
async function initialize(){const response=page.waitForResponse(r=>r.request().method()==='POST'&&new URL(r.url()).pathname==='/api/demo/session');await page.getByRole('button',{name:'初始化页面',exact:true}).click();assert.equal((await response).status(),200);await home();assert.equal(await read('/api/agent/tasks/current'),null);}
async function chat(message){await nav('assistant');await page.locator('#agent-message').fill(message);const response=page.waitForResponse(r=>r.request().method()==='POST'&&/\/api\/tasks(?:\/[^/]+\/messages)?$/.test(new URL(r.url()).pathname));await page.getByRole('button',{name:'发送',exact:true}).click();assert.equal((await response).status(),200);}
try {
  assert.equal((await read('/api/health')).test_only,true);await page.goto(base);
  assert(await page.getByRole('button',{name:'初始化页面',exact:true}).isDisabled());
  await page.locator('#user').selectOption('u1');await page.locator('#agent-message').waitFor();
  await initialize();
  const box=await page.getByRole('button',{name:'初始化页面',exact:true}).boundingBox();assert(box.x>1000);passed.push('top-right-initialize-enters-clean-workspace');
  assert.equal((await post('/api/cards',{alias:'测试银行卡',bank_name:'合成银行',card_number:'6222000000006655',account_alias:'测试账户',opening_balance_minor:12345})).status(),201);
  assert.equal((await post('/api/recipients',{name:'测试朋友',bank_name:'合成银行',account_number:'6222000000007788',remark:'朋友'})).status(),201);
  const before={accounts:await read('/api/accounts'),cards:await read('/api/cards'),recipients:await read('/api/recipients')};
  await chat('从活期账户转100元到储蓄账户');const pending=(await read('/api/agent/tasks/current')).proposal;
  await page.locator('#agent-message').fill('未提交的对话草稿');
  await nav('tasks/manual');await page.getByText('手动填写操作（可选）',{exact:true}).click();await page.getByLabel('金额（元）',{exact:true}).fill('321.09');
  const oldCookie=(await context.cookies()).find(c=>c.name==='finagent_demo_session').value;
  await initialize();assert.notEqual((await context.cookies()).find(c=>c.name==='finagent_demo_session').value,oldCookie);
  assert.equal(await page.locator('#agent-message').inputValue(),'');assert.equal(await page.locator('.continue-task').count(),0);
  assert.notEqual((await post('/api/proposals/'+pending.id+'/confirm',{confirmation_token:'old-token'})).status(),200);
  assert.deepEqual(await read('/api/accounts'),before.accounts);assert.deepEqual(await read('/api/cards'),before.cards);assert.deepEqual(await read('/api/recipients'),before.recipients);
  passed.push('unconfirmed-task-cleared-and-directory-preserved');
  await nav('tasks/manual');await page.getByText('手动填写操作（可选）',{exact:true}).click();assert.equal(await page.getByLabel('金额（元）',{exact:true}).inputValue(),'100.00');
  await page.reload();await page.getByText('手动填写操作（可选）',{exact:true}).waitFor();assert.equal(await read('/api/agent/tasks/current'),null);passed.push('drafts-reset-and-refresh-stays-clean');

  await initialize();await chat('从活期账户转10元到储蓄账户');await page.getByRole('button',{name:'核对并继续',exact:true}).click();
  await page.getByRole('dialog').getByRole('button',{name:'确认条款并执行',exact:true}).click();await page.getByText('模拟转账成功',{exact:true}).first().waitFor();
  const completed={accounts:await read('/api/accounts'),transactions:await read('/api/transactions')};
  await initialize();assert.deepEqual(await read('/api/accounts'),completed.accounts);assert.deepEqual(await read('/api/transactions'),completed.transactions);passed.push('completed-transaction-and-balances-preserved');

  let release,captured,delivered;const held=new Promise(resolve=>{captured=resolve;}),delivery=new Promise(resolve=>{delivered=resolve;});
  await page.route('**/api/tasks',async route=>{const response=await route.fetch();captured();await new Promise(resolve=>{release=resolve;});await route.fulfill({response});delivered();});
  await page.locator('#agent-message').fill('查询余额');await page.getByRole('button',{name:'发送',exact:true}).click();await held;
  await initialize();release();await delivery;await page.unroute('**/api/tasks');await page.waitForTimeout(250);
  assert.equal(await page.locator('.message-row').count(),0);assert.equal(await read('/api/agent/tasks/current'),null);passed.push('late-response-cannot-restore-old-conversation');

  let releaseReset,resetCaptured;const resetHeld=new Promise(resolve=>{resetCaptured=resolve;});let resetCalls=0;
  await page.route('**/api/demo/session',async route=>{if(route.request().method()!=='POST')return route.continue();resetCalls++;resetCaptured();await new Promise(resolve=>{releaseReset=resolve;});await route.continue();});
  await page.getByRole('button',{name:'初始化页面',exact:true}).click();await resetHeld;assert(await page.getByRole('button',{name:'初始化中…',exact:true}).isDisabled());
  await page.getByRole('button',{name:'初始化中…',exact:true}).dispatchEvent('click');assert.equal(resetCalls,1);releaseReset();await home();await page.unroute('**/api/demo/session');passed.push('repeated-click-does-not-create-multiple-resets');

  await page.route('**/api/demo/session',route=>route.request().method()==='POST'?route.abort('failed'):route.continue());
  await page.getByRole('button',{name:'初始化页面',exact:true}).click();await page.getByRole('alert').filter({hasText:'连接中断'}).waitFor();
  await page.unroute('**/api/demo/session');await initialize();assert.deepEqual(await read('/api/accounts'),completed.accounts);passed.push('failed-reset-can-be-retried-without-changing-data');
  for(const width of [390,320]){await page.setViewportSize({width,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));const b=await page.getByRole('button',{name:'初始化页面',exact:true}).boundingBox();assert(b&&b.y<62&&b.x+b.width<=width&&b.height>=44);await page.screenshot({path:path.join(output,'initialize-'+width+'.png'),fullPage:true});}
  passed.push('initialize-button-fits-mobile-top-right');
  await page.setViewportSize({width:1366,height:900});
  await page.locator('#user').selectOption('u2');await page.locator('#agent-message').waitFor();
  const u2Before=await read('/api/accounts');await initialize();
  assert.equal((await read('/api/demo/session')).user.id,'u2');
  assert.deepEqual(await read('/api/accounts'),u2Before);
  assert.equal((await read('/api/demo/users')).length,3);
  passed.push('initialize-keeps-selected-user-and-multi-user-data');assert.deepEqual(errors,[]);
  await writeFile(path.join(output,'result.json'),JSON.stringify({status:'passed',passed,errors},null,2));console.log(JSON.stringify({status:'passed',passed,errors}));
}catch(error){await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});console.error(JSON.stringify({passed,errors,error:String(error)}));throw error;}
finally{await context.close();await browser.close();}
