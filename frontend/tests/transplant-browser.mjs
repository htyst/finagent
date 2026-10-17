import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
const base=process.env.FINAGENT_E2E_BASE_URL??'http://127.0.0.1:5176';
const output=process.env.FINAGENT_E2E_OUTPUT;
assert(/^http:\/\/127\.0\.0\.1:\d+$/.test(base)&&output?.includes('TEST-ONLY'));
await mkdir(output,{recursive:true});
const browser=await chromium.launch({channel:'msedge',headless:true});
const context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage();
const errors=[],failed=[],passed=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('response',r=>{if(r.url().includes('/api/')&&r.status()>=400&&r.status()!==403&&!(r.status()===401&&r.url().endsWith('/api/demo/session')))failed.push({url:r.url(),status:r.status()});});
const money=n=>new Intl.NumberFormat('zh-CN',{style:'currency',currency:'CNY'}).format(n/100);
async function get(route){const response=await context.request.get(base+route);assert.equal(response.status(),200,route);return (await response.json()).data;}
async function nav(hash){await page.evaluate(hash=>location.hash=hash,hash);}
async function overflow(){assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Horizontal overflow');}
async function shot(name){await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});}
try {
  assert.equal((await get('/api/health')).test_only,true);
  await page.goto(base);await page.locator('#user option').nth(1).waitFor({state:'attached'});
  const users=await get('/api/demo/users');assert.equal(users.length,20);
  for(const user of users){
    await page.locator('#user').selectOption(user.id);
    await page.waitForFunction(id=>document.querySelector('#user')?.value===id,user.id);
    await nav('assets/profile');await page.locator('#financial-profile').waitFor();
    const profile=await get('/api/financial-profile');
    await page.locator('.financial-totals article').filter({has:page.getByText('净资产',{exact:true})}).getByText(money(profile.balances.net_worth_minor),{exact:true}).waitFor();
    await overflow();
    if(user.id==='u1')await shot('financial-profile-desktop');
    await nav('assets/accounts');await page.locator('.bank-card').first().waitFor();
    assert.equal(await page.locator('.bank-card').count(),profile.cards.length,user.id+' cards');
    await nav('assets/cards');await page.getByRole('heading',{name:'模拟卡片管理',exact:true}).waitFor();
    await page.waitForFunction(count=>document.querySelectorAll('.card-management table tbody tr').length===count,profile.cards.length);
    if(user.id==='u16'){const expired=profile.cards.find(c=>c.status==='expired');assert(expired);await page.locator('.card-management tr').filter({hasText:expired.card_last4}).getByText('已过期',{exact:true}).waitFor();}
    await nav('assets/wealth');await page.getByRole('heading',{name:'模拟理财',exact:true}).waitFor();
    if(profile.investments.length){await page.getByRole('heading',{name:'导入基金持仓（快照，只读）'}).waitFor();assert.equal(await page.getByText('暂无持仓',{exact:true}).count(),0);}
    await nav('assets/subscriptions');await page.getByRole('heading',{name:'订阅与银行代扣',exact:true}).waitFor();
    await page.getByRole('button',{name:'明确确认订阅查询',exact:true}).click();
    await page.getByText('订阅查询已确认',{exact:true}).waitFor();
    const data=await get('/api/subscriptions');
    if(data.imported.contracts.length){await page.getByRole('heading',{name:`年度案例订阅合同 · ${data.imported.contracts.length}份`}).waitFor();assert.equal(await page.locator('.imported-subscriptions tbody').first().locator('tr').count(),data.imported.contracts.length);}
    await overflow();
    if(user.id==='u14'){
      await shot('subscriptions-desktop');await page.setViewportSize({width:390,height:844});await overflow();await shot('subscriptions-mobile');
      await nav('assets/profile');await page.locator('#financial-profile').waitFor();await overflow();await shot('financial-profile-mobile');await page.setViewportSize({width:1440,height:900});
    }
  }
  passed.push('20-users-financial-profiles-all-cards-imported-wealth-subscriptions-and-no-overflow');
  await page.locator('#user').selectOption('u2');await page.waitForFunction(()=>document.querySelector('#user')?.value==='u2');
  const spending=await get('/api/spending?start=2026-08-01&end=2026-08-31&comparison_mode=none&evidence_limit=2');
  assert.equal(spending.current.totals.net_minor,1126808);
  const analysis=await get('/api/financial-analysis?kind=expense_by_merchant&start=2026-08-01&end=2026-08-31');
  const history=await get('/api/financial-history?table=monthly_financial_snapshots&limit=2');
  const task={id:'test-only-ui-read',status:'completed',mode:'mock',intent:'summarize_spending',plan_version:1,proposal:null,candidates:[],messages:[{role:'user',content:'查看8月消费和资产历史'},{role:'assistant',content:'查询结果已返回。'}],steps:[{ordinal:1,tool_name:'summarize_spending',status:'succeeded',arguments:{},result:spending},{ordinal:2,tool_name:'analyze_finances',status:'succeeded',arguments:{},result:analysis},{ordinal:3,tool_name:'read_financial_history',status:'succeeded',arguments:{},result:history}]};
  await page.route('**/api/agent/tasks/current',route=>route.fulfill({json:{status:'ok',data:task,error_code:null,message:''}}));
  await page.reload();await nav('assistant');await page.getByRole('button',{name:'查看报告与逐笔证据'}).click();
  const dialog=page.getByRole('dialog');await dialog.getByRole('article',{name:'金融收支分析'}).waitFor();
  await dialog.getByRole('article',{name:'金融历史档案'}).waitFor();
  assert((await dialog.innerText()).includes(money(1126808)));
  await dialog.getByRole('button',{name:'下一页档案'}).click();await dialog.getByText('2 / 7',{exact:true}).waitFor();
  await shot('assistant-analysis-evidence');await dialog.getByRole('button',{name:'关闭详情'}).click();
  passed.push('real-annual-api-facts-render-in-new-assistant-evidence-and-history-pagination');
  await page.unroute('**/api/agent/tasks/current');
  // Preserve the card and merchant scope on the next evidence page.
  const credit=(await get('/api/cards')).find(c=>c.card_type==='credit');assert(credit);
  const cardSpending=await get(`/api/spending?start=2025-09-25&end=2026-09-24&card_id=${credit.id}&comparison_mode=none&evidence_limit=1`);
  assert(cardSpending.current.evidence.total>1);
  task.steps=[{ordinal:1,tool_name:'summarize_spending',status:'succeeded',result:cardSpending}];
  await page.route('**/api/agent/tasks/current',route=>route.fulfill({json:{status:'ok',data:task,error_code:null,message:''}}));
  await page.reload();await page.getByRole('button',{name:'查看报告与逐笔证据'}).click();
  const request=page.waitForRequest(r=>r.url().includes('/api/spending?')&&r.url().includes('evidence_offset=1'));
  await page.getByRole('button',{name:'下一页依据',exact:true}).click();const next=new URL((await request).url());assert.equal(next.searchParams.get('card_id'),credit.id);
  passed.push('credit-card-evidence-pagination-preserves-filter');
  assert.deepEqual(errors,[]);assert.deepEqual(failed,[]);
  await writeFile(path.join(output,'result.json'),JSON.stringify({status:'passed',passed,users:20,errors,failed,real_model_calls:0},null,2));
  console.log(JSON.stringify({status:'passed',passed,errors,failed}));
} catch(error){await shot('failure').catch(()=>{});console.error(JSON.stringify({passed,errors,failed,error:String(error)}));throw error;}
finally {await context.close();await browser.close();}
