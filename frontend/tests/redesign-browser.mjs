import assert from 'node:assert/strict';
import { chromium } from 'playwright-core';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
const base = process.env.FINAGENT_E2E_BASE_URL, output = process.env.FINAGENT_E2E_OUTPUT;
if (!base || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base) || !output?.includes('TEST-ONLY')) throw Error('Isolated services required');
await mkdir(output, {recursive:true});
const browser = await chromium.launch({channel:'msedge',headless:true});
const context = await browser.newContext({viewport:{width:1440,height:900}}), page = await context.newPage();
const errors=[], passed=[], posts=[], reads=[];
page.on('pageerror', e=>errors.push(e.message));
page.on('request', r=>{if(r.method()==='POST')posts.push(new URL(r.url()).pathname);if(r.method()==='GET')reads.push(new URL(r.url()).pathname);});
async function nav(target) { await page.evaluate(target=>{location.hash=target;},target); await page.waitForTimeout(120); }
async function state() { return (await(await context.request.get(base+'/api/agent/tasks/current')).json()).data; }
async function post(route,payload) {const csrf=(await(await context.request.get(base+'/api/demo/csrf')).json()).data.csrf_token;return context.request.post(base+route,{headers:{'X-FinAgent-CSRF':csrf,'Origin':base},...(payload ? {data:payload}: {})});}
async function chat(text) {
  await nav('assistant'); await page.locator('#agent-message').fill(text);
  const response=page.waitForResponse(r=>r.request().method()==='POST' && /\/api\/tasks(?:\/[^/]+\/messages)?$/.test(new URL(r.url()).pathname));
  await page.getByRole('button',{name:'发送',exact:true}).click(); const result=await response;assert.equal(result.status(),200);
  await page.getByRole('button',{name:'发送',exact:true}).isDisabled();
  return state();
}
async function shot(name) {await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});}
async function noOverflow() {assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Page must not scroll horizontally');}
try {
  assert.equal((await(await context.request.get(base+'/api/health')).json()).data.test_only,true);
  await page.goto(base); await page.getByRole('heading',{name:'让每一步金融操作清楚可控'}).waitFor();
  assert.equal(await page.locator('#user').inputValue(),'');assert(!reads.includes('/api/subscriptions'));passed.push('anonymous-no-auto-session-or-yellow-query');
  await page.locator('#user').selectOption('u1');await page.getByRole('heading',{name:'把繁琐的金融操作，交给一句话。'}).waitFor();
  assert(!reads.includes('/api/subscriptions'));
  assert.equal(await page.locator('.continue-task').count(),0);passed.push('real-empty-home-no-fictional-history');
  for(const [width,height] of [[1440,900],[1366,768],[1024,768],[390,844]]){await page.setViewportSize({width,height});await noOverflow();await shot('home-'+width);}
  await page.setViewportSize({width:1440,height:900});
  const before=posts.length;await page.getByRole('button',{name:'给小林转 200 元',exact:false}).click();
  assert.equal(await page.locator('#agent-message').inputValue(),'给小林转200元');assert.equal(posts.length,before);passed.push('quick-example-fills-only');
  await page.locator('#agent-message').dispatchEvent('compositionstart');await page.locator('#agent-message').press('Enter');await page.locator('#agent-message').dispatchEvent('compositionend');assert.equal(posts.length,before);passed.push('IME-does-not-send');
  let t=await chat('从活期账户转100元到储蓄账户');assert.equal(t.proposal.summary.amount_minor,10000);const old=t.proposal.id;
  await page.getByRole('button',{name:'核对并继续',exact:true}).waitFor();await page.waitForFunction(()=>document.querySelector('[data-testid="operation-card"] button')?.disabled===false);await shot('transfer-1440');
  await page.locator('#agent-message').fill('暂存的输入');const count=posts.length;
  await nav('assets/accounts');await page.getByRole('heading',{name:'我的账户',exact:true}).waitFor();await shot('assets-1440');
  await nav('tasks/current');await page.getByRole('button',{name:'返回对话办理'}).waitFor();await shot('tasks-1440');
  await page.getByText('执行记录与版本',{exact:true}).click();await page.getByRole('button',{name:'查看服务端审计记录'}).click();await page.locator('.execution-record').getByText(/策略版本/).waitFor();passed.push('server-audit-is-reachable');
  await nav('assistant');assert.equal(await page.locator('#agent-message').inputValue(),'暂存的输入');assert.equal((await state()).proposal.id,old);assert.equal(posts.length,count);passed.push('navigation-keeps-task-draft-no-POST');
  t=await chat('改成200元');assert.equal(t.proposal.summary.amount_minor,20000);assert.notEqual(t.proposal.id,old);
  assert.notEqual((await post('/api/proposals/'+old+'/confirmation-token')).status(),200);passed.push('edit-invalidates-old-proposal');
  await page.getByRole('button',{name:'核对并继续',exact:true}).click();let dialog=page.getByRole('dialog');await dialog.waitFor();assert.match(await dialog.innerText(),/黄色/);await shot('yellow-security');
  for(let i=0;i<12;i++){await page.keyboard.press('Tab');assert(await dialog.evaluate(el=>el.contains(document.activeElement)));}
  await page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});assert.equal((await state()).proposal.status,'pending_confirmation');passed.push('yellow-cancel-focus-trap-no-write');
  await page.route('**/api/proposals/*/confirm',async route=>{await route.fetch();await route.abort('failed');});
  await page.getByRole('button',{name:'核对并继续',exact:true}).click();dialog=page.getByRole('dialog');await dialog.getByRole('button',{name:'确认条款并执行',exact:true}).click();
  await page.getByText('模拟转账成功',{exact:true}).first().waitFor();await page.unroute('**/api/proposals/*/confirm');assert.equal((await state()).proposal.status,'executed');assert.equal(posts.filter(p=>/\/proposals\/[^/]+\/confirm$/.test(p)).length,1);passed.push('lost-response-recovers-original-receipt-once');
  await page.reload();await page.locator('.receipt').waitFor();assert.equal((await state()).proposal.id,t.proposal.id);passed.push('reload-restores-original-receipt');
  const writesBeforeRecovery=posts.filter(p=>p.endsWith('/confirm')).length;
  await page.route('**/api/agent/tasks/current',r=>r.abort('internetdisconnected'));
  await page.evaluate(()=>window.dispatchEvent(new Event('online')));
  await page.locator('.workspace-alert').filter({hasText:'写操作已暂停'}).waitFor();
  await page.unroute('**/api/agent/tasks/current');
  await page.locator('.workspace-alert').getByRole('button',{name:'重新查询状态',exact:true}).click();
  await page.locator('.workspace-alert').filter({hasText:'写操作已暂停'}).waitFor({state:'hidden'});
  assert.equal((await state()).proposal.id,t.proposal.id);assert.equal(posts.filter(p=>p.endsWith('/confirm')).length,writesBeforeRecovery);passed.push('read-failure-only-retries-read');
  await nav('tasks/manual');await page.getByText('手动填写操作（可选）',{exact:true}).waitFor();
  await page.locator('.operation-section details summary').click();await page.getByLabel('金额（元）',{exact:true}).fill('321.09');await nav('security');await nav('tasks/manual');
  await page.locator('.operation-section details summary').click();assert.equal(await page.getByLabel('金额（元）',{exact:true}).inputValue(),'321.09');passed.push('manual-form-draft-survives-navigation');
  const red=await post('/api/transfers/prepare',{source_account_id:'a1c',target:{type:'own_account',account_id:'a1s'},amount_minor:200000});assert.equal(red.status(),200);
  await page.getByRole('button',{name:'重新查询操作状态',exact:true}).click();await page.getByRole('button',{name:'核对并继续',exact:true}).click();dialog=page.getByRole('dialog');assert.match(await dialog.innerText(),/红色/);await shot('red-security');await dialog.getByRole('button',{name:'取消',exact:true}).click();passed.push('red-cancel-keeps-unexecuted');
  const routes=[['assets/bills','消费统计'],['assets/cards','模拟卡片管理'],['assets/wealth','模拟理财'],['assets/subscriptions','订阅与银行代扣'],['tasks/birthdays','生日安排'],['tasks/schedules','预约与 AA 收款'],['assets/recipients','收款人备注'],['demo/operator',null]];
  for(const [target,heading] of routes){await nav(target);if(heading)await page.getByRole('heading',{name:heading,exact:true}).waitFor();else await page.getByText('本机模拟坐席队列',{exact:true}).waitFor();await noOverflow();}
  passed.push('all-existing-business-entries-reachable');
  await nav('assets/subscriptions');await page.getByRole('button',{name:'明确确认订阅查询',exact:true}).click();await page.getByText('订阅查询已确认',{exact:true}).waitFor();passed.push('yellow-subscription-query-preserved');
  await nav('assistant');
  for(const [width,height] of [[1366,768],[1024,768],[390,844]]){await page.setViewportSize({width,height});await noOverflow();await shot('workspace-'+width);const composer=await page.locator('.conversation-composer').boundingBox();assert(composer && composer.y+composer.height<=height-(width<768 ? 64:0)+2);if(width<768){const target=await page.getByRole('button',{name:'发送',exact:true}).boundingBox();assert(target.width>=44&&target.height>=44);}}
  await page.getByRole('button',{name:'当前任务',exact:true}).click();await page.getByRole('dialog').waitFor();await shot('mobile-task');await page.getByRole('button',{name:'关闭详情'}).click();passed.push('responsive-workspace-and-task-drawer');
  await nav('demo');await page.getByLabel('切换演示用户',{exact:true}).selectOption('u2');await page.getByRole('heading',{name:'运行状态',exact:true}).waitFor();await nav('assistant');assert.equal(await page.locator('#agent-message').inputValue(),'');assert.equal((await state()),null);passed.push('user-switch-clears-task-and-drafts');
  await page.setViewportSize({width:1440,height:900});await page.locator('#user').selectOption('u1');await page.locator('#agent-message').waitFor();
  let release, captured;
  const held=new Promise(resolve=>{captured=resolve;});
  await page.route('**/api/tasks',async route=>{const response=await route.fetch();captured();await new Promise(resolve=>{release=resolve;});await route.fulfill({response});});
  await page.locator('#agent-message').fill('查一下余额');await page.getByRole('button',{name:'发送',exact:true}).click();await held;
  await page.locator('#user').selectOption('u2');await page.locator('#agent-message').waitFor();release();await page.waitForTimeout(350);
  assert.equal(await page.locator('.agent-proposal').count(),0);assert.equal(await page.locator('.message-row').count(),0);assert.equal((await state()),null);passed.push('late-chat-response-cannot-cross-users');await page.unroute('**/api/tasks');
  assert.deepEqual(errors,[]);
  await writeFile(path.join(output,'result.json'),JSON.stringify({passed,errors,status:'passed',mode:'mock',real_model:false},null,2));console.log(JSON.stringify({passed,errors,status:'passed'}));
} catch(error){await shot('failure').catch(()=>{});console.error(JSON.stringify({passed,errors,error:String(error)}));throw error;}
finally{await context.close();await browser.close();}
