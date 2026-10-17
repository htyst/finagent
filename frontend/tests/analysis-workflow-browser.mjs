import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import {execFileSync} from 'node:child_process';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
const base=process.env.FINAGENT_E2E_BASE_URL,output=process.env.FINAGENT_E2E_OUTPUT;
assert(/^http:\/\/127\.0\.0\.1:\d+$/.test(base)&&output?.includes('TEST-ONLY'));
await mkdir(output,{recursive:true});
const browser=await chromium.launch({channel:'msedge',headless:true}),context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage();
const errors=[],passed=[];page.on('pageerror',e=>errors.push(e.message));
async function seed(kind){
 await page.goto('about:blank');await context.request.post(base+'/api/demo/session',{data:{user_id:'u1'}});
 const sid=(await context.cookies()).find(c=>c.name==='finagent_demo_session').value;
 const result=JSON.parse(execFileSync(process.env.FINAGENT_TEST_PYTHON,[process.env.FINAGENT_TEST_SEED,sid,kind],{encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'}}));
 assert.equal(result.status,kind==='protected'?'awaiting_confirmation':'completed');
 await page.goto(base);await page.locator('#agent-message').waitFor();
 await page.locator('.conversation-scroll').waitFor();
 await page.getByLabel('分析工作流').waitFor();return result;
}
try{
 assert.equal((await(await context.request.get(base+'/api/health')).json()).data.test_only,true);
 await seed('completed');let graph=page.getByLabel('分析工作流');await graph.locator('summary').click();
 assert((await graph.textContent()).includes('依赖：财务底稿'));assert((await graph.textContent()).includes('只读分析'));
 passed.push('desktop-displays-persisted-dag-and-dependencies');
 await page.screenshot({path:path.join(output,'desktop-analysis-workflow.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 await page.screenshot({path:path.join(output,'mobile-analysis-workflow.png'),fullPage:true});passed.push('workflow-readable-on-mobile');
 await page.setViewportSize({width:1440,height:900});await seed('protected');graph=page.getByLabel('分析工作流');await graph.locator('summary').click();
 assert((await graph.textContent()).includes('待手动确认'));assert((await graph.textContent()).includes('待处理'));
 passed.push('protected-read-is-checkpointed-before-synthesis');
 // The existing confirmation card must stay in the conversation, not require navigating to another business page.
 await page.getByRole('button',{name:'确认查询我的订阅',exact:true}).waitFor();passed.push('manual-query-confirmation-visible-in-conversation');
 assert.deepEqual(errors,[]);await writeFile(path.join(output,'result.json'),JSON.stringify({status:'passed',passed,errors,real_model_calls:0},null,2));console.log(JSON.stringify({status:'passed',passed}));
}catch(e){await page.screenshot({path:path.join(output,'failure.png'),fullPage:true});await writeFile(path.join(output,'result.json'),JSON.stringify({status:'failed',passed,errors,message:e.message},null,2));throw e;}finally{await browser.close();}
