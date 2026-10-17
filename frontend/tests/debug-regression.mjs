import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
const base=process.env.FINAGENT_E2E_BASE_URL,output=process.env.FINAGENT_E2E_OUTPUT;
assert(/^http:\/\/127\.0\.0\.1:\d+$/.test(base)&&output?.includes('TEST-ONLY'));
await mkdir(output,{recursive:true});
const browser=await chromium.launch({channel:'msedge',headless:true}),context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage();
const passed=[],errors=[];page.on('pageerror',e=>errors.push(e.message));
const money=n=>new Intl.NumberFormat('zh-CN',{style:'currency',currency:'CNY'}).format(n/100);
async function get(route){const res=await context.request.get(base+route);assert.equal(res.status(),200,route);return (await res.json()).data;}
async function session(id){assert.equal((await context.request.post(base+'/api/demo/session',{data:{user_id:id}})).status(),200);}
async function grant(){const csrf=(await get('/api/demo/csrf')).csrf_token;assert.equal((await context.request.post(base+'/api/subscriptions/query-confirmation',{headers:{Origin:base,'X-FinAgent-CSRF':csrf}})).status(),200);}
async function fixture(kind,value,filters={}){await page.evaluate(({kind,value,filters})=>window.renderCase(kind,value,filters),{kind,value,filters});}
async function fixturePage(){await page.goto(base+'/tests/ui-fixtures.html');await page.waitForFunction(()=>typeof window.renderCase==='function');}
async function shot(name){await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});}
async function overflow(){assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));}
try{
 assert.equal((await get('/api/health')).test_only,true);
 await session('u2');await fixturePage();
 await fixture('spending',{demoDate:'2026-09-24',accounts:await get('/api/accounts'),cards:await get('/api/cards'),revision:0});
 await page.getByRole('heading',{name:'消费统计',exact:true}).waitFor();
 for(const category of ['订阅续费','水电通信','保险保障','经营支出','未分类'])assert.equal(await page.getByLabel('类别',{exact:true}).locator(`option[value="${category}"]`).count(),1);
 await page.getByLabel('开始日期',{exact:true}).fill('2026-08-01');await page.getByLabel('结束日期',{exact:true}).fill('2026-08-31');await page.getByLabel('类别',{exact:true}).selectOption('全部');
 await page.getByRole('button',{name:'查询',exact:true}).click();await page.locator('.spending-summary').getByText(money(1126808),{exact:true}).waitFor();
 passed.push('all-16-categories-and-cash-credit-august-total');
 const cards=await get('/api/cards'),credit=cards.find(c=>c.card_type==='credit');
 await page.getByLabel('银行卡',{exact:true}).selectOption(credit.id);await page.getByRole('button',{name:'查询',exact:true}).click();
 await page.locator('.spending-summary').getByText(money(108451),{exact:true}).first().waitFor();
 const next=page.waitForRequest(r=>r.url().includes('/api/spending?')&&r.url().includes('evidence_offset=5'));
 await page.getByRole('button',{name:'下一页',exact:true}).click();assert.equal(new URL((await next).url()).searchParams.get('card_id'),credit.id);
 await page.getByLabel('商户',{exact:true}).fill('尚未应用的商户');await page.getByRole('button',{name:'上一页',exact:true}).click();
 await page.locator('.spending-summary').getByText(money(108451),{exact:true}).first().waitFor();passed.push('card-filter-and-pagination-keeps-applied-filter');
 await session('u1');
 await fixturePage();await fixture('spending',{demoDate:'2026-09-24',accounts:await get('/api/accounts'),cards:await get('/api/cards'),revision:0});
 await page.getByRole('heading',{name:'消费统计',exact:true}).waitFor();
 await page.getByLabel('开始日期',{exact:true}).fill('2026-08-01');await page.getByLabel('结束日期',{exact:true}).fill('2026-08-31');await page.getByLabel('类别',{exact:true}).selectOption('水电通信');
 await page.getByRole('button',{name:'查询',exact:true}).click();await page.locator('.spending-summary').getByText(money(34711),{exact:true}).first().waitFor();
 await page.getByLabel('开始日期',{exact:true}).fill('2025-09-25');await page.getByLabel('结束日期',{exact:true}).fill('2026-09-24');await page.getByLabel('类别',{exact:true}).selectOption('未分类');
 await page.getByRole('button',{name:'查询',exact:true}).click();await page.locator('.spending-summary').getByText(money(1100),{exact:true}).first().waitFor();
 passed.push('utilities-347-11-and-unclassified-11-00-match-independent-benchmarks');
 const one=await get('/api/spending?start=2026-07-01&end=2026-07-31&category=餐饮&comparison_mode=none&evidence_limit=1');
 const two=await get('/api/spending?start=2026-08-01&end=2026-08-31&category=餐饮&comparison_mode=none&evidence_limit=1');
 await fixture('report',one);await page.locator('.analytics-evidence').getByText(one.current.evidence.items[0].transaction_id,{exact:true}).waitFor();
 await fixture('report',two);await page.locator('.analytics-evidence').getByText(two.current.evidence.items[0].transaction_id,{exact:true}).waitFor();assert.equal(await page.locator('.analytics-evidence').getByText(one.current.evidence.items[0].transaction_id,{exact:true}).count(),0);
 passed.push('new-report-replaces-old-evidence');
 // A single cash account in the returned directory does not imply an account filter.
 await fixture('report',{...two,account_ids:[two.account_ids[0]]});
 const sameScope=page.waitForRequest(r=>r.url().includes('/api/spending?')&&r.url().includes('evidence_offset=1'));
 await page.getByRole('button',{name:'下一页依据',exact:true}).click();assert.equal(new URL((await sameScope).url()).searchParams.has('account_id'),false);
 passed.push('single-account-directory-does-not-narrow-all-source-evidence');
 await fixture('financial',{analysis_kind:'cashflow',basis:'原查询',summary:{income_minor:100},items:[]});await page.getByText('原查询',{exact:true}).waitFor();
 await fixture('financial',{analysis_kind:'cashflow',basis:'更新查询',summary:{income_minor:200},items:[]});await page.getByText('更新查询',{exact:true}).waitFor();assert.equal(await page.getByText('原查询',{exact:true}).count(),0);passed.push('financial-report-prop-updates');
 const {annual}=JSON.parse(await readFile(new URL('./financial-fixtures.json',import.meta.url),'utf8'));
 await fixture('report',annual);await page.getByText(/首尾部分月份/).waitFor();assert.equal(await page.locator('.analytics-months tbody tr').count(),13);
 await page.getByText('2025-09-25 至 2025-09-30 · 部分月份',{exact:true}).waitFor();await page.getByText('2026-09-01 至 2026-09-24 · 部分月份',{exact:true}).waitFor();
 passed.push('rolling-year-13-fragments-actual-boundary-dates-and-ranking-scope');
 await fixture('report',{...two,current:{...two.current,evidence:{...two.current.evidence,items:[]}}});
 await page.getByRole('button',{name:'加载逐笔依据',exact:true}).click();await page.locator('.analytics-evidence').getByText(two.current.evidence.items[0].transaction_id,{exact:true}).waitFor();passed.push('unloaded-evidence-is-not-labelled-empty');
 await grant();
 const filters={start:'2026-01-01',end:'2026-09-24',status:'paid'};
 const sub=await get('/api/subscription-analysis?start=2026-01-01&end=2026-09-24&status=paid&limit=2&contract_limit=1');
 await fixture('financial',sub,filters);await page.getByRole('button',{name:'下一页扣费',exact:true}).waitFor();
 const billingRequest=page.waitForRequest(r=>r.url().includes('/api/subscription-analysis?')&&new URL(r.url()).searchParams.get('offset')==='2');
 await page.getByRole('button',{name:'下一页扣费',exact:true}).click();const billing=new URL((await billingRequest).url());assert.equal(billing.searchParams.get('start'),filters.start);assert.equal(billing.searchParams.get('end'),filters.end);assert.equal(billing.searchParams.get('status'),'paid');
 await page.getByRole('button',{name:'下一页扣费',exact:true}).waitFor({state:'visible'});
 const contractRequest=page.waitForRequest(r=>r.url().includes('/api/subscription-analysis?')&&new URL(r.url()).searchParams.get('contract_offset')==='1');
 await page.getByRole('button',{name:'下一页合同',exact:true}).click();const contract=new URL((await contractRequest).url());assert.equal(contract.searchParams.get('offset'),'2');assert.equal(contract.searchParams.get('status'),'paid');
 passed.push('independent-contract-and-billing-pagination-preserves-dates-and-status');
 const imported=(await get('/api/subscriptions')).imported;
 await fixture('imported',imported);await page.getByText(/查看历史扣费尝试/).click();await page.getByRole('button',{name:'下一页扣费记录'}).waitFor();
 assert.equal(await page.locator('.imported-subscriptions details tbody tr').count(),10);
 await page.getByRole('button',{name:'下一页扣费记录'}).click();await page.getByText(/按扣费日期从新到旧/).waitFor();
 await page.getByLabel('扣费月份',{exact:true}).fill('2026-08');await page.getByLabel('扣费结果',{exact:true}).selectOption('paid');await page.getByRole('button',{name:'查询扣费历史'}).click();
 await page.getByText(/范围 2026-08-01 至 2026-08-31/).waitFor();
 const scope=page.locator('.imported-subscriptions details');assert((await scope.innerText()).includes('2026-08-'));await scope.getByRole('button',{name:'查看关联流水'}).first().click();
 await page.getByRole('dialog').getByText('现金账户流水',{exact:true}).waitFor();await page.getByRole('button',{name:'关闭详情'}).click();passed.push('subscription-month-status-service-name-and-cash-evidence');
 const creditContract=imported.contracts.find(c=>c.payment_method==='credit_card_autopay');assert(creditContract);
 await page.getByLabel('扣费月份',{exact:true}).fill('');await page.getByLabel('订阅服务',{exact:true}).selectOption(creditContract.subscription_id);await page.getByRole('button',{name:'查询扣费历史'}).click();
 await scope.getByRole('button',{name:'查看关联流水'}).first().click();await page.getByRole('dialog').getByText('信用卡账单',{exact:true}).waitFor();await page.getByRole('button',{name:'关闭详情'}).click();passed.push('subscription-credit-event-drilldown');
 await page.route('**/api/subscription-analysis?**',r=>r.fulfill({status:503,json:{status:'error',error_code:'TEST_OFFLINE',message:'测试网络失败'}}));
 await page.getByRole('button',{name:'查询扣费历史'}).click();await page.getByText('测试网络失败',{exact:false}).waitFor();assert.equal(await scope.locator('tbody tr').count(),0);
 await page.unroute('**/api/subscription-analysis?**');await page.getByRole('button',{name:'重试扣费历史'}).click();await scope.getByRole('button',{name:'查看关联流水'}).first().waitFor();passed.push('history-read-failure-clears-stale-table-and-read-only-retry');
 await page.setViewportSize({width:390,height:844});await overflow();await shot('subscription-filters-mobile');await page.setViewportSize({width:1440,height:900});
 // A hook-level race must send exactly one POST even before React renders busy=true.
 await fixturePage();let calls=0;
 await page.route('**/api/agent/tasks/current',r=>r.fulfill({json:{data:null}}));await page.route('**/api/tasks',async r=>{calls++;await r.fulfill({json:{data:{id:'fixture-task',status:'completed',proposal:null,messages:[],steps:[],candidates:[]}}});});
 await fixture('hook',{});await page.getByText('ready',{exact:true}).waitFor();await page.evaluate(()=>Promise.all([window.workspace.sendMessage('查余额'),window.workspace.sendMessage('查余额')]));assert.equal(calls,1);passed.push('double-send-only-one-post');
 await page.unrouteAll();await page.goto(base);await page.locator('#user').selectOption('u1');await page.waitForFunction(()=>document.querySelector('#user')?.value==='u1');
 await page.evaluate(()=>location.hash='assets/profile');await page.locator('#financial-profile').waitFor();await page.getByText('查询历史账单与资产变动',{exact:true}).click();
 await page.getByText('当前展示 10 条，共 13 条。',{exact:true}).waitFor();await page.getByRole('button',{name:'下一页档案'}).click();await page.getByText('当前展示 3 条，共 13 条。',{exact:true}).waitFor();
 await page.getByLabel('历史资料',{exact:true}).selectOption('credit_card_statements');await page.getByLabel('历史银行卡',{exact:true}).selectOption((await get('/api/cards')).find(c=>c.card_type==='credit').id);await page.getByRole('button',{name:'查询金融历史'}).click();
 await page.getByText('当前展示 10 条，共 13 条。',{exact:true}).waitFor();await shot('financial-history-desktop');
 await page.setViewportSize({width:390,height:844});await overflow();await shot('financial-history-mobile');passed.push('profile-history-13-snapshots-credit-card-statements-mobile');
 assert.deepEqual(errors,[]);await writeFile(path.join(output,'result.json'),JSON.stringify({status:'passed',passed,errors,real_model_calls:0},null,2));console.log(JSON.stringify({status:'passed',passed,errors}));
}catch(error){await shot('failure').catch(()=>{});console.error(JSON.stringify({passed,errors,error:String(error)}));throw error;}
finally{await context.close();await browser.close();}
