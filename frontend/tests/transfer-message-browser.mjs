import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import {execFileSync} from 'node:child_process';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
const base=process.env.FINAGENT_E2E_BASE_URL,output=process.env.FINAGENT_E2E_OUTPUT;
assert(/^http:\/\/127\.0\.0\.1:\d+$/.test(base)&&output?.includes('TEST-ONLY'));await mkdir(output,{recursive:true});
const browser=await chromium.launch({channel:'msedge',headless:true}),context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage();
const errors=[],posts=[],passed=[];let sid;
page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()==='POST')posts.push(new URL(r.url()).pathname);});
const run=(...args)=>execFileSync(process.env.FINAGENT_TEST_PYTHON,[process.env.FINAGENT_TEST_SEED,sid,...args],{encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'}}).trim();
const state=async()=>(await(await context.request.get(base+'/api/agent/tasks/current')).json()).data;
async function login(seed=false){await page.goto('about:blank');await context.request.post(base+'/api/demo/session',{data:{user_id:'u1'}});sid=(await context.cookies()).find(c=>c.name==='finagent_demo_session').value;if(seed)run();await page.goto(base);await page.locator('#agent-message').waitFor();}
async function hold(route){let release,captured,finished;const started=new Promise(resolve=>{captured=resolve;});const done=new Promise(resolve=>{finished=resolve;});const gate=new Promise(resolve=>{release=resolve;});await page.route(route,async r=>{captured();await gate;try{await r.continue();}catch(e){if(!/already handled|closed|cancel/i.test(e.message))throw e;}finally{finished();}});return {started,release,done};}
try{
 assert.equal((await(await context.request.get(base+'/api/health')).json()).data.test_only,true);
 await login();let held=await hold('**/api/tasks');
 await page.locator('#agent-message').fill('你好');await page.getByRole('button',{name:'发送',exact:true}).click();await held.started;
 await page.getByTestId('outgoing-message').filter({hasText:'你好'}).waitFor();assert.equal(await page.locator('#agent-message').inputValue(),'');
 await page.locator('#agent-message').fill('下一条尚未发送的草稿');held.release();await page.getByTestId('outgoing-message').waitFor({state:'hidden'});
 assert.equal(await page.locator('#agent-message').inputValue(),'下一条尚未发送的草稿');assert.equal(await page.locator('.message-row.from-user').count(),1);await page.unroute('**/api/tasks');
 passed.push('new-question-visible-before-request-finishes-and-next-draft-survives');

 held=await hold('**/api/tasks');await page.locator('#agent-message').fill('谢谢你');await page.getByRole('button',{name:'发送',exact:true}).click();await held.started;
 await page.getByTestId('outgoing-message').filter({hasText:'谢谢你'}).waitFor();assert.equal(await page.locator('.message-row.from-user').count(),2);
 held.release();await page.getByTestId('outgoing-message').waitFor({state:'hidden'});await page.unroute('**/api/tasks');passed.push('new-task-keeps-existing-conversation-visible-while-waiting');

 await login();let secretStart=posts.length;
 await page.locator('#agent-message').fill('验证码：000000');await page.getByRole('button',{name:'发送',exact:true}).click();await page.getByRole('alert').filter({hasText:'认证信息'}).waitFor();
 assert.equal(posts.length,secretStart);assert.equal(await page.getByTestId('outgoing-message').count(),0);passed.push('authentication-material-is-neither-rendered-as-chat-nor-sent');

 await login(true);const task=await state();held=await hold('**/api/tasks/'+task.id+'/messages');
 await page.locator('#agent-message').fill('再问一次');await page.getByRole('button',{name:'发送',exact:true}).click();await held.started;
 await page.getByTestId('outgoing-message').filter({hasText:'再问一次'}).waitFor();held.release();await page.getByTestId('outgoing-message').waitFor({state:'hidden'});await page.unroute('**/api/tasks/'+task.id+'/messages');
 assert.equal((await state()).messages.filter(m=>m.role==='user'&&m.content==='再问一次').length,1);passed.push('follow-up-visible-immediately-and-not-duplicated-after-response');

 const form=page.getByRole('form',{name:'新收款人资料'});await form.waitFor();
 assert.equal(await form.getByLabel('转账金额（元）',{exact:true}).inputValue(),'200.00');
 assert.equal(await form.getByLabel('付款账户',{exact:true}).inputValue(),'');
 const accounts=(await(await context.request.get(base+'/api/accounts')).json()).data;const payer=accounts.find(a=>a.status==='active');assert(payer);
 await form.getByLabel('付款账户',{exact:true}).selectOption(payer.id);await form.getByLabel('开户银行',{exact:true}).fill('演示银行');await form.getByLabel('收款账号',{exact:true}).fill('2131244534');
 await page.waitForTimeout(2200);assert.equal(await form.getByLabel('开户银行',{exact:true}).inputValue(),'演示银行');assert.equal(await form.getByLabel('付款账户',{exact:true}).inputValue(),payer.id);
 await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:path.join(output,'mobile-transfer-form.png'),fullPage:true});
 const before=JSON.parse(run('--state')).transfers;
 const submitted=page.waitForResponse(r=>r.url().endsWith('/transfer-details'));await form.getByRole('button',{name:'提交资料并继续'}).click();assert.equal((await submitted).status(),200);await form.waitFor({state:'hidden'});
 let current=await state();assert.equal(current.proposal.summary.amount_minor,20000);assert.equal(current.proposal.summary.source.alias,payer.alias);assert.equal(JSON.parse(run('--state')).transfers,before);
 passed.push('payer-dropdown-and-existing-amount-submit-to-one-fixed-transfer-without-model');
 await page.getByRole('button',{name:'核对并继续',exact:true}).click();const dialog=page.getByRole('dialog');await dialog.waitFor();
 const a=current.authorization_operations[0];
 if(a.level==='red'){const issued=page.waitForResponse(r=>r.url().endsWith('/challenge'));await dialog.getByRole('button',{name:'获取模拟短信验证码'}).click();const challenge=(await(await issued).json()).data;await dialog.getByLabel('六位验证码').fill(run('--code',challenge.challenge_id));}
 await dialog.getByRole('button',{name:a.level==='red'?'确认条款并验证执行':'确认条款并执行',exact:true}).click();await page.getByText('模拟转账成功',{exact:true}).first().waitFor();
 assert.equal(JSON.parse(run('--state')).transfers,before+1);assert.equal((await state()).proposal.status,'executed');passed.push('complete-transfer-from-chat-to-authorized-receipt-on-mobile');
 await page.setViewportSize({width:1440,height:900});

 await login();let start=posts.length;
 await page.route('**/api/tasks',async r=>{await r.fetch();await r.abort('failed');});
 await page.locator('#agent-message').fill('你好');await page.getByRole('button',{name:'发送',exact:true}).click();await page.getByRole('status').filter({hasText:'问题已在服务端保存'}).waitFor();
 assert.equal(posts.slice(start).filter(p=>p==='/api/tasks').length,1);assert.equal(await page.getByTestId('outgoing-message').count(),0);assert.equal((await state()).messages.filter(m=>m.role==='user'&&m.content==='你好').length,1);await page.unroute('**/api/tasks');
 passed.push('lost-response-reconciles-server-message-without-reposting');

 await login();start=posts.length;await page.route('**/api/tasks',r=>r.abort('internetdisconnected'));
 await page.locator('#agent-message').fill('这条消息暂未送达');await page.getByRole('button',{name:'发送',exact:true}).click();await page.getByTestId('outgoing-message').filter({hasText:'送达状态待核实'}).waitFor();
 assert.equal(await page.locator('#agent-message').inputValue(),'这条消息暂未送达');await page.getByRole('button',{name:'核对发送状态'}).click();assert.equal(posts.slice(start).filter(p=>p==='/api/tasks').length,1);await page.unroute('**/api/tasks');passed.push('failed-delivery-retains-question-and-status-retry-is-read-only');

 await login();held=await hold('**/api/tasks');await page.locator('#agent-message').fill('只属于原用户的问题');await page.getByRole('button',{name:'发送',exact:true}).click();await held.started;await page.getByTestId('outgoing-message').waitFor();
 await page.locator('#user').selectOption('u2');await page.waitForFunction(()=>document.querySelector('#user')?.value==='u2');held.release();await held.done;await page.unroute('**/api/tasks');
 assert.equal(await page.getByTestId('outgoing-message').count(),0);assert.equal(await page.locator('#agent-message').inputValue(),'');assert.equal(await state(),null);passed.push('switching-user-clears-pending-question-and-draft');
 assert.deepEqual(errors,[]);await writeFile(path.join(output,'result.json'),JSON.stringify({status:'passed',passed,errors,real_model_calls:0},null,2));console.log(JSON.stringify({status:'passed',passed,errors}));
}catch(e){await page.screenshot({path:path.join(output,'failure.png'),fullPage:true});await writeFile(path.join(output,'result.json'),JSON.stringify({status:'failed',passed,errors,message:e.message},null,2));throw e;}
finally{await browser.close();}
