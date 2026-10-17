import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import {execFileSync} from 'node:child_process';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
const base=process.env.FINAGENT_E2E_BASE_URL,output=process.env.FINAGENT_E2E_OUTPUT;
assert(/^http:\/\/127\.0\.0\.1:\d+$/.test(base)&&output?.includes('TEST-ONLY'));
await mkdir(output,{recursive:true});
const browser=await chromium.launch({channel:'msedge',headless:true}),context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage();
const errors=[],posts=[],passed=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()==='POST')posts.push(new URL(r.url()).pathname);});
try{
 const health=(await(await context.request.get(base+'/api/health')).json()).data;assert.equal(health.test_only,true);
 assert.equal((await context.request.post(base+'/api/demo/session',{data:{user_id:'u1'}})).status(),200);
 const sid=(await context.cookies()).find(c=>c.name==='finagent_demo_session').value;
 const task=JSON.parse(execFileSync(process.env.FINAGENT_TEST_PYTHON,[process.env.FINAGENT_TEST_SEED,sid],{encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'}}));
 const grants=()=>JSON.parse(execFileSync(process.env.FINAGENT_TEST_PYTHON,[process.env.FINAGENT_TEST_SEED,sid,'--grants'],{encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'}}));const grantsBefore=grants();
 assert.equal((await context.request.get(base+'/api/subscription-analysis')).status(),403);
 await page.goto(base);await page.getByRole('button',{name:'确认查询我的订阅',exact:true}).waitFor();
 assert.equal(posts.filter(p=>p.endsWith('/subscription-query-confirmation')).length,0);passed.push('blocked-query-does-not-auto-authorize');
 await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:path.join(output,'inline-consent-mobile.png'),fullPage:true});
 await page.setViewportSize({width:1440,height:900});await page.evaluate(()=>location.hash='tasks/current');await page.getByRole('button',{name:'确认查询我的订阅',exact:true}).waitFor();
 await page.screenshot({path:path.join(output,'task-query-consent.png'),fullPage:true});passed.push('same-action-reachable-in-task-center-and-mobile');
 const response=page.waitForResponse(r=>r.url().endsWith(`/api/tasks/${task.id}/subscription-query-confirmation`));
 await page.getByRole('button',{name:'确认查询我的订阅',exact:true}).evaluate(el=>{el.click();el.click();});assert.equal((await response).status(),200);
 await page.getByRole('button',{name:'确认查询我的订阅',exact:true}).waitFor({state:'hidden'});
 assert.equal(posts.filter(p=>p.endsWith('/subscription-query-confirmation')).length,1);assert.equal(posts.filter(p=>p==='/api/tasks'||p.endsWith('/messages')).length,0);
 await page.evaluate(()=>location.hash='assistant');await page.getByText(/已确认查询范围，继续刚才的订阅查询/).waitFor();await page.getByText(/折算月均56.50元/).waitFor();
 await page.screenshot({path:path.join(output,'continued-query-result.png'),fullPage:true});passed.push('one-explicit-click-resumes-original-query-without-model-or-retyping');
 assert.equal((await context.request.get(base+'/api/subscription-analysis')).status(),200);
 assert.deepEqual(grants(),grantsBefore);
 passed.push('read-confirmation-does-not-authorize-background-debits');
 await page.getByRole('button',{name:'查看报告与逐笔证据'}).click();await page.getByRole('dialog').getByRole('article',{name:'订阅与自动续费分析'}).waitFor();await page.getByRole('button',{name:'关闭详情'}).click();
 await page.locator('#user').selectOption('u2');await page.waitForFunction(()=>document.querySelector('#user')?.value==='u2');assert.equal(await page.getByRole('button',{name:'确认查询我的订阅',exact:true}).count(),0);
 passed.push('result-evidence-is-reachable-and-user-switch-clears-action');assert.deepEqual(errors,[]);
 await writeFile(path.join(output,'result.json'),JSON.stringify({status:'passed',passed,errors,external_model_calls:0},null,2));console.log(JSON.stringify({status:'passed',passed,errors}));
}catch(error){await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});console.error(JSON.stringify({passed,errors,error:String(error)}));throw error;}
finally{await context.close();await browser.close();}
