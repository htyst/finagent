import {chromium} from 'playwright-core';
import {writeFile} from 'node:fs/promises';
const base='http://127.0.0.1:5176';
const browser=await chromium.launch({channel:'msedge',headless:true}),context=await browser.newContext(),page=await context.newPage();
const results=[];
async function fixture(kind,value){await page.evaluate(({kind,value})=>window.renderCase(kind,value),{kind,value});}
async function caseRun(name,check){try{results.push({name,bug:await check()});}catch(e){results.push({name,error:String(e)});}}
try{
 await context.request.post(base+'/api/demo/session',{data:{user_id:'u1'}});
 await page.goto(base+'/tests/ui-fixtures.html');await page.waitForFunction(()=>window.renderCase);
 const get=async p=>(await(await context.request.get(base+p)).json()).data;
 await caseRun('category-options-missing',async()=>{await fixture('spending',{demoDate:'2026-09-24',accounts:await get('/api/accounts'),revision:0});await page.getByRole('heading',{name:'消费统计'}).waitFor();return await page.getByLabel('类别',{exact:true}).locator('option[value="订阅续费"]').count()===0;});
 const one=await get('/api/spending?start=2026-07-01&end=2026-07-31&category=餐饮&comparison_mode=none&evidence_limit=1');
 const two=await get('/api/spending?start=2026-08-01&end=2026-08-31&category=餐饮&comparison_mode=none&evidence_limit=1');
 await caseRun('spending-prop-update-retains-old-evidence',async()=>{await fixture('report',one);await page.locator('.analytics-evidence').waitFor();await fixture('report',two);await page.waitForTimeout(100);return (await page.locator('.analytics-evidence').innerText()).includes(one.current.evidence.items[0].transaction_id);});
 await caseRun('financial-prop-update-entirely-stale',async()=>{await fixture('financial',{analysis_kind:'cashflow',basis:'旧范围',summary:{income_minor:100},items:[]});await page.getByText('旧范围',{exact:true}).waitFor();await fixture('financial',{analysis_kind:'cashflow',basis:'新范围',summary:{income_minor:200},items:[]});await page.waitForTimeout(100);return await page.getByText('旧范围',{exact:true}).count()>0;});
 await caseRun('subscription-pagination-missing',async()=>{await fixture('financial',{contracts:[],contracts_total:200,contract_limit:2,contract_offset:0,has_more_contracts:true,summary:{},billing_events:[],billing_total:200,limit:2,offset:0,has_more:true});await page.waitForTimeout(100);return (await page.getByRole('button',{name:'下一页合同'}).count())===0&&(await page.getByRole('button',{name:'下一页扣费'}).count())===0;});
 await caseRun('double-send-not-guarded',async()=>{let calls=0;await page.route('**/api/agent/tasks/current',r=>r.fulfill({json:{data:null}}));await page.route('**/api/tasks',async r=>{calls++;await r.fulfill({json:{data:{id:'t',status:'completed',proposal:null,messages:[],steps:[],candidates:[]}}});});await fixture('hook',{});await page.getByText('ready',{exact:true}).waitFor();await page.evaluate(()=>Promise.all([window.workspace.sendMessage('查余额'),window.workspace.sendMessage('查余额')]));return calls>1;});
 await writeFile(process.env.FINAGENT_DEBUG_OUT,JSON.stringify(results,null,2));console.log(JSON.stringify(results));
}finally{await context.close();await browser.close();}
