import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
const base=process.env.FINAGENT_E2E_BASE_URL,out=process.env.FINAGENT_E2E_OUTPUT;
if(!base||!/^http:\/\/127\.0\.0\.1:\d+$/.test(base)||!out?.includes('TEST-ONLY'))throw Error('Isolated TEST-ONLY browser environment required');
await mkdir(out,{recursive:true});
const browser=await chromium.launch({channel:'msedge',headless:true});const context=await browser.newContext({viewport:{width:1440,height:1000}});const page=await context.newPage();const errors=[],checks=[],traces=[];
page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(25000);
async function current(){return (await(await context.request.get(base+'/api/agent/tasks/current')).json()).data;}
async function chat(text,fresh=true){
 if(fresh&&await page.locator('#assistant').getByRole('button',{name:'新对话',exact:true}).count())await page.locator('#assistant').getByRole('button',{name:'新对话',exact:true}).click();
 await page.locator('#agent-message').fill(text);
 const response=page.waitForResponse(r=>r.request().method()==='POST'&&/\/api\/tasks(?:\/[^/]+\/messages)?$/.test(new URL(r.url()).pathname));
 await page.locator('#assistant').getByRole('button',{name:'发送',exact:true}).click();assert.equal((await response).status(),200);
 await page.getByRole('region',{name:'任务目标进度'}).waitFor();
 const s=await current();traces.push({user:text,graph:s.execution_graph});return s;
}
try{
 assert.equal((await(await context.request.get(base+'/api/health')).json()).data.test_only,true);
 await page.goto(base);await page.locator('#user').selectOption('u1');await page.locator('#agent-message').waitFor();
 let s=await chat('你好');assert.equal(s.execution_graph.nodes.length,0);assert.equal(s.execution_graph.goals[0].status,'completed');checks.push('问候没有强制工具计划');
 s=await chat('查询本人余额');assert.equal(s.execution_graph.status,'completed');checks.push('真实余额查询和完成状态');
 s=await chat('分析旅行是否可行，预算待定');assert.equal(s.execution_graph.goals[0].status,'waiting_input');await page.getByRole('region',{name:'任务目标进度'}).getByText('待补充',{exact:true}).first().waitFor();checks.push('分析缺预算显示待补充');
 const oldId=s.id,oldVersion=s.plan_version;s=await chat('预算改成八千元',false);assert.equal(s.id,oldId);assert(s.plan_version>oldVersion);assert.equal(s.execution_graph.goals[0].constraints[0].value,'八千');checks.push('同一任务跨轮改预算且保留查询');
 s=await chat('查余额，并分析预算待定的旅行');assert.deepEqual(s.execution_graph.goals.map(g=>g.status),['completed','waiting_input']);checks.push('共享查询与多目标部分完成');
 await page.screenshot({path:path.join(out,'multiple-desktop.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});await page.waitForTimeout(300);
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));await page.getByRole('region',{name:'任务目标进度'}).scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'multiple-mobile.png'),fullPage:true});checks.push('390窄屏无页面横向溢出');
 await page.setViewportSize({width:1440,height:1000});
 s=await chat('测试余额读取失败');assert.equal(s.execution_graph.goals[0].status,'failed');checks.push('工具失败保持未完成');
 s=await chat('从活期账户转100元到储蓄账户');assert.equal(s.execution_graph.goals[0].status,'waiting_authorization');
 await page.getByRole('region',{name:'任务目标进度'}).getByText('待页面授权',{exact:true}).first().waitFor();checks.push('固定单待授权不显示完成');
 await page.locator('#assistant').getByRole('button',{name:'核对并继续',exact:true}).click();const dialog=page.getByRole('dialog');await dialog.waitFor();await dialog.getByRole('button',{name:'确认条款并执行',exact:true}).click();await dialog.waitFor({state:'hidden'});
 for(let i=0;i<70;i++){s=await current();if(s.execution_graph.goals[0].receipt_refs.length)break;await page.waitForTimeout(100);}
 assert.equal(s.execution_graph.goals[0].status,'completed');assert.equal(s.execution_graph.goals[0].receipt_refs.length,1);checks.push('页面授权后同节点展示真实回执');
 await page.reload();await page.locator('#agent-message').waitFor();s=await current();assert.equal(s.execution_graph.goals[0].receipt_refs.length,1);// Refresh normally restores the current workspace directly. Open the saved task only if landing is active.
 await page.waitForFunction(()=>document.querySelector('#assistant .continue-task')||document.querySelector('[aria-label="任务目标进度"]'));if(await page.locator('#assistant .continue-task').count())await page.locator('#assistant .continue-task').click();await page.getByRole('region',{name:'任务目标进度'}).waitFor();await page.locator('.conversation-authorizations .receipt').first().waitFor();checks.push('刷新后恢复页面并展示持久回执');
 await page.screenshot({path:path.join(out,'receipt-desktop.png'),fullPage:true});
 const paidTaskId=s.id;s=await chat('查询本人余额',false);assert.equal(s.id,paidTaskId);assert(s.authorization_operations.some(o=>o.receipt));assert.equal(s.execution_graph.nodes.filter(n=>n.policy.effect==='prepare').length,1);checks.push('付款完成后追问沿用同任务和回执，不准备重复转账');
 s=await chat('先从活期账户转100元到储蓄账户，再申请虚拟卡，申请人为本人，关联活期账户');
 await page.locator('#assistant').getByRole('button',{name:'核对并继续',exact:true}).click();const nextDialog=page.getByRole('dialog');await nextDialog.waitFor();await nextDialog.getByRole('button',{name:'确认条款并执行',exact:true}).click();await nextDialog.waitFor({state:'hidden'});
 for(let i=0;i<100;i++){s=await current();if((s?.authorization_operations??[]).length===2)break;await page.waitForTimeout(100);}
 assert.equal(s.authorization_operations.length,2);assert(s.authorization_operations.some(o=>o.receipt));assert(s.authorization_operations.some(o=>o.status==='pending_confirmation'&&o.can_confirm));
 await page.locator('.conversation-authorization').nth(1).waitFor();assert.equal(await page.locator('.conversation-authorization').count(),2);assert.equal(await page.locator('.conversation-authorization .proposal-actions button').filter({hasText:'核对并继续'}).count(),1);
 await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));await page.locator('.conversation-authorization').nth(1).scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'multiple-forms-mobile.png'),fullPage:true});checks.push('两类固定单同时展示真实已执行及待确认状态，窄屏无横溢');
 await page.setViewportSize({width:1440,height:1000});await chat('测试长回复布局');await page.getByText('这是隔离浏览器的长回复布局样例，不是业务结果。',{exact:false}).first().waitFor();await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));await page.locator('#agent-message').fill('长回复后仍可输入');assert.equal(await page.locator('#agent-message').inputValue(),'长回复后仍可输入');await page.screenshot({path:path.join(out,'long-response-mobile.png'),fullPage:true});checks.push('长回复窄屏不溢出且输入框可继续使用');
 await page.goto(base+'/#assets/accounts');await page.locator('.transaction-list .transaction').first().waitFor();assert.equal(await page.locator('.transaction-list .transaction').count(),10);const beforePage=await page.locator('.transaction-list').textContent();const pageResponse=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/transactions'&&new URL(r.url()).searchParams.get('offset')==='10');await page.getByRole('button',{name:'下一页',exact:true}).click();assert.equal((await pageResponse).status(),200);await page.waitForFunction(old=>document.querySelector('.transaction-list')?.textContent!==old,beforePage);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));await page.screenshot({path:path.join(out,'ledger-list-mobile.png'),fullPage:true});checks.push('真实流水分页加载下一页，窄屏列表不导致页面横溢');
 assert.deepEqual(errors,[]);
 await writeFile(path.join(out,'report.json'),JSON.stringify({passed:true,mode:'Edge+model_substitute+real_domain',checks,traces,errors},null,2));
 console.log(JSON.stringify({passed:true,checks,errors}));
}catch(error){await writeFile(path.join(out,'report.json'),JSON.stringify({passed:false,checks,traces,errors,error:String(error)},null,2));throw error;}finally{await browser.close();}