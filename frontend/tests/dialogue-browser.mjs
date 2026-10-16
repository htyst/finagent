// Actual Edge + actual DeepSeek, isolated synthetic ledger. No LLM stubs/routes.
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
const base=process.env.FINAGENT_E2E_BASE_URL,output=process.env.FINAGENT_E2E_OUTPUT;
if(!base||!/^http:\/\/127\.0\.0\.1:\d+$/.test(base)||!output?.includes('TEST-ONLY'))throw Error('Isolated target required');
await mkdir(output,{recursive:true});
const browser=await chromium.launch({channel:'msedge',headless:true});
const context=await browser.newContext();const page=await context.newPage();
page.setDefaultTimeout(90000);
const errors=[],passed=[],transcript=[];
page.on('pageerror',e=>errors.push(e.message));
async function state(){return (await (await context.request.get(base+'/api/agent/tasks/current')).json()).data;}
async function chat(text){
  await page.locator('#agent-message').fill(text);
  const response=page.waitForResponse(r=>r.request().method()==='POST'&&/\/api\/tasks(?:\/[^/]+\/messages)?$/.test(new URL(r.url()).pathname),{timeout:110000});
  await page.locator('#assistant').getByRole('button',{name:'发送',exact:true}).click();
  const result=await response;assert.equal(result.status(),200);
  // Playwright's next action waits for the actual button; avoid coupling to
  // transient text nodes or the separate details form's submit button.
  const s=await state();transcript.push({text,state:s});return s;
}
async function confirm(button){
  await button.click();const dialog=page.getByRole('dialog');await dialog.waitFor();
  await dialog.getByRole('button',{name:'确认条款并执行',exact:true}).click();await dialog.waitFor({state:'hidden'});
}
try{
  assert.equal((await(await context.request.get(base+'/api/health')).json()).data.test_only,true);
  await page.goto(base);await page.locator('#user').selectOption('u1');await page.locator('#agent-message').waitFor();
  let s=await chat('从活期账户转100元给张三');assert.equal(s.proposal,null);passed.push('Live同名消歧');
  s=await chat('选尾号3821');assert.equal(s.proposal.summary.target.id,'r1');const first=s.proposal.id;
  s=await chat('改成80元');assert.equal(s.proposal.summary.amount_minor,8000);assert.notEqual(s.proposal.id,first);passed.push('Live改参新单');
  s=await chat('先别转，查一下余额');assert.equal(s.proposal,null);assert(s.steps.some(x=>x.tool_name==='list_accounts'));passed.push('Live暂停后插问');
  s=await chat('原来那笔改成60元，再准备一下');assert.equal(s.proposal.summary.amount_minor,6000);
  await page.route('**/api/proposals/*/confirm',async route=>{await route.fetch();await route.abort('failed');});
  await confirm(page.locator('#assistant').getByRole('button',{name:'核对并继续',exact:true}));
  await page.getByText('模拟转账成功',{exact:true}).first().waitFor();
  await page.unroute('**/api/proposals/*/confirm');const receipt=(await state()).proposal.receipt.order_id;
  passed.push('Live黄色确认及响应丢失恢复');
  s=await chat('刚才那笔成功了吗');assert(s.messages.at(-1).content.includes(receipt));passed.push('Live原回执只读查询');
  s=await chat('看看本月餐饮花了多少');assert(s.steps.some(x=>x.tool_name==='summarize_spending'));
  s=await chat('那上个月呢');let query=s.steps.filter(x=>x.tool_name==='summarize_spending').at(-1).arguments;
  assert.equal(query.category,'餐饮');assert.equal(query.start,'2026-08-01');assert.equal(query.end,'2026-08-31');passed.push('Live账单省略追问');
  s=await chat('尾号1234这张卡先别让它在网上花钱');assert.equal(s.card_request.kind,'online_block');
  s=await chat('还是刚才那张卡，把单笔限额改成500元');assert.equal(s.card_request.kind,'limit');assert.equal(s.card_request.card_id,'c1a');assert.equal(s.card_request.summary.limit_minor,50000);
  await page.evaluate(() => {location.hash='assets/cards';});
  await confirm(page.getByRole('button',{name:'确认卡片办理',exact:true}));
  for(let i=0;i<60;i++){s=await state();if(s.card_request.status==='executed')break;await page.waitForTimeout(100);}
  assert.equal(s.card_request.status,'executed');passed.push('Live卡片指代改参和黄色确认');
  await page.reload();await page.locator('#card-management').waitFor();assert.equal((await state()).card_request.status,'executed');passed.push('Live刷新恢复');
  assert.deepEqual(errors,[]);
  await page.screenshot({path:path.join(output,'unified-live-three-scenes.png'),fullPage:true});
  const metrics=await(await context.request.get(base+'/api/evaluation/metrics')).json();
  await writeFile(path.join(output,'result.json'),JSON.stringify({mode:'actual_Edge_and_DeepSeek',passed,errors,metrics},null,2));
  console.log(JSON.stringify({mode:'actual_Edge_and_DeepSeek',passed,errors,metrics}));
}finally{
  await page.screenshot({path:path.join(output,'last-page.png'),fullPage:true}).catch(()=>{});
  await writeFile(path.join(output,'page-state.json'),JSON.stringify({errors,
    buttons:await page.locator('#assistant button').evaluateAll(nodes=>nodes.map(n=>({text:n.textContent,disabled:n.disabled}))).catch(()=>[])},null,2));
  const metrics=await context.request.get(base+'/api/evaluation/metrics').then(r=>r.json()).catch(()=>null);
  await writeFile(path.join(output,'usage.json'),JSON.stringify(metrics,null,2));
  await writeFile(path.join(output,'transcripts.json'),JSON.stringify(transcript,null,2));
  await browser.close();
}
