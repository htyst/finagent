import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import {execFileSync} from 'node:child_process';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';

const base=process.env.FINAGENT_E2E_BASE_URL,output=process.env.FINAGENT_E2E_OUTPUT;
assert(/^http:\/\/127\.0\.0\.1:\d+$/.test(base)&&output?.includes('TEST-ONLY'));
await mkdir(output,{recursive:true});
const browser=await chromium.launch({channel:'msedge',headless:true}),context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage();
const errors=[],posts=[],passed=[];
page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()==='POST')posts.push(new URL(r.url()).pathname);});
let sid,fixture;
const helper=(...args)=>execFileSync(process.env.FINAGENT_TEST_PYTHON,[process.env.FINAGENT_TEST_SEED,sid,...args],{encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'}}).trim();
const current=async()=>(await(await context.request.get(base+'/api/agent/tasks/current')).json()).data;
async function seed(kind){
  await page.goto('about:blank');
  assert.equal((await context.request.post(base+'/api/demo/session',{data:{user_id:'u1'}})).status(),200);
  sid=(await context.cookies()).find(c=>c.name==='finagent_demo_session').value;
  fixture=JSON.parse(helper(kind));
  await page.goto(base);await page.locator('.conversation-scroll').waitFor();
  return fixture;
}
async function authorize({wrongCode=false,doubleClick=false,lostResponse=false}={}){
  const a=(await current()).authorization_operations.find(o=>o.status==='pending_confirmation');assert(a);
  const before=JSON.parse(helper('--state'));
  const selector='**'+a.authorization_path+'/confirm';
  if(lostResponse)await page.route(selector,async route=>{const response=await route.fetch();assert.equal(response.status(),200);await route.abort('connectionreset');});
  const start=posts.length;
  await page.getByRole('button',{name:'核对并继续',exact:true}).click();
  const dialog=page.getByRole('dialog');await dialog.waitFor();
  assert.equal(posts.slice(start).filter(p=>p.endsWith('/confirmation-token')||p.endsWith('/confirm')).length,0);
  if(a.level==='red'){
    const challenge=page.waitForResponse(r=>r.url().endsWith(a.authorization_path+'/challenge'));
    await dialog.getByRole('button',{name:'获取模拟短信验证码',exact:true}).click();
    const sent=(await(await challenge).json()).data;
    const actual=helper('--code',sent.challenge_id);
    const input=dialog.getByLabel('六位验证码');
    if(wrongCode){
      const invalid=(actual[0]==='0'?'1':'0')+actual.slice(1);
      await input.fill(invalid);await dialog.getByRole('button',{name:'确认条款并验证执行',exact:true}).click();
      await dialog.getByRole('alert').filter({hasText:'验证码错误'}).waitFor();
      assert.equal(posts.slice(start).filter(p=>p.endsWith('/confirm')).length,0);
    }
    await input.fill(actual);
  }
  const confirmButton=dialog.getByRole('button',{name:a.level==='red'?'确认条款并验证执行':'确认条款并执行',exact:true});
  if(doubleClick)await confirmButton.evaluate(el=>{el.click();el.click();});else await confirmButton.click();
  await dialog.waitFor({state:'hidden'});
  if(lostResponse){await page.getByRole('status').filter({hasText:'办理结果已恢复'}).waitFor();await page.unroute(selector);}
  await page.locator('[data-operation-kind]').filter({has:page.getByRole('button',{name:'核对并继续',exact:true})}).waitFor({state:'hidden'});
  const result=await current(),after=JSON.parse(helper('--state'));
  const operation=result.authorization_operations.find(o=>o.resource_id===a.resource_id);
  assert(['executed','settled','submitted','scheduled','collecting'].includes(operation.status),operation.status);
  assert.equal(posts.slice(start).filter(p=>p===a.authorization_path+'/confirm').length,1);
  if(a.resource_kind==='proposal'&&a.action==='transfer')assert.equal(after.transfer_orders-before.transfer_orders,1);
  return result;
}

try{
  assert.equal((await(await context.request.get(base+'/api/health')).json()).data.test_only,true);
  await seed('proposal');
  let initial=JSON.parse(helper('--state')),start=posts.length;
  await page.getByRole('button',{name:'核对并继续',exact:true}).click();
  await page.getByRole('dialog').getByRole('button',{name:'取消',exact:true}).click();
  await page.getByRole('status').filter({hasText:'已暂缓授权'}).waitFor();
  assert.deepEqual(JSON.parse(helper('--state')),initial);
  assert.equal(posts.slice(start).filter(p=>p.endsWith('/confirmation-token')||p.endsWith('/confirm')).length,0);
  passed.push('cancel-security-dialog-keeps-unexecuted-draft');
  await authorize({doubleClick:true});passed.push('yellow-chat-confirmation-executes-once-after-explicit-click');

  await seed('proposal');await authorize({lostResponse:true});passed.push('lost-response-restores-receipt-without-replaying-payment');
  await seed('red');await authorize({wrongCode:true,doubleClick:true});passed.push('red-verification-wrong-code-recovery-and-double-click-guard');

  await seed('risk');initial=JSON.parse(helper('--state'));
  const form=page.getByRole('form',{name:'对话风险评估'});await form.waitFor();
  await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.screenshot({path:path.join(output,'risk-assessment-mobile.png'),fullPage:true});await page.setViewportSize({width:1440,height:900});
  const selects=form.locator('select');for(let i=0;i<4;i++)await selects.nth(i).selectOption('0');
  await form.getByRole('button',{name:'提交评估并继续准备'}).click();await form.waitFor({state:'hidden'});
  assert.deepEqual(JSON.parse(helper('--state')),initial);
  await page.screenshot({path:path.join(output,'wealth-pending-in-chat.png'),fullPage:true});
  await authorize();passed.push('risk-assessment-resumes-wealth-preparation-and-requires-separate-strong-confirmation');

  await seed('subscription-continuation');
  await page.getByRole('button',{name:'确认查询并继续准备',exact:true}).click();
  await page.getByRole('button',{name:'核对并继续',exact:true}).waitFor();
  assert.equal((await current()).subscription_request.status,'pending_confirmation');
  await authorize();passed.push('subscription-query-consent-resumes-original-revoke-then-separately-authorizes');

  for(const [kind,expected] of [['reminder','executed'],['card','submitted'],['schedule','scheduled'],['collection','collecting'],['birthday','executed'],['loss','executed']]){
    await seed(kind);const result=await authorize();
    assert.equal(result.authorization_operations[0].status,expected);
    await page.screenshot({path:path.join(output,kind+'-result-in-chat.png'),fullPage:true});
    passed.push(kind+'-fixed-terms-verification-and-result-stay-in-chat');
  }

  await seed('proposal');initial=JSON.parse(helper('--state'));
  await page.getByRole('button',{name:'核对并继续',exact:true}).click();await page.getByRole('dialog').waitFor();
  await page.locator('#user').selectOption('u2');await page.getByRole('dialog').waitFor({state:'hidden'});
  assert.deepEqual(JSON.parse(helper('--state')),initial);
  assert(!posts.some(p=>p.endsWith('/messages')));passed.push('switch-user-closes-confirmation-and-never-sends-authentication-to-chat');
  assert.deepEqual(errors,[]);
  await writeFile(path.join(output,'result.json'),JSON.stringify({status:'passed',passed,errors},null,2));
  console.log(JSON.stringify({status:'passed',passed,errors}));
}catch(error){
  await page.screenshot({path:path.join(output,'failure.png'),fullPage:true});
  await writeFile(path.join(output,'result.json'),JSON.stringify({status:'failed',passed,errors,message:error.message},null,2));throw error;
}finally{await browser.close();}
