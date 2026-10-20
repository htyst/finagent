import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import {writeFile} from 'node:fs/promises';
import path from 'node:path';
const base=process.env.FINAGENT_E2E_BASE_URL, out=process.env.FINAGENT_E2E_OUTPUT;
if(!base?.startsWith('http://127.0.0.1:')||!out?.includes('TEST-ONLY'))throw Error('Isolated TEST-ONLY environment required');
const browser=await chromium.launch({channel:'msedge',headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();
const checks=[],errors=[];page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(20000);
let selected=0;
try{
  await page.goto(base);
  const response=await context.request.get(base+'/api/demo/users');assert.equal(response.status(),200);
  const users=(await response.json()).data;assert.equal(users.length,20);checks.push('real API exposes twenty annual users');
  await page.waitForFunction(()=>document.querySelectorAll('#user option').length===21);
  assert.equal(await page.locator('#user option').count(),21);checks.push('selector contains all users plus placeholder');
  for(const user of users){
    const loaded=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/accounts'&&r.request().method()==='GET');
    await page.locator('#user').selectOption(user.id);assert.equal((await loaded).status(),200);
    await page.waitForFunction(id=>{const s=document.querySelector('#user');return s?.value===id&&!s.disabled;},user.id);
    await page.locator('#agent-message').waitFor();selected++;
  }
  assert.equal(selected,20);checks.push('all twenty users can open their account workspace');
  await page.reload();await page.waitForFunction(()=>document.querySelectorAll('#user option').length===21);
  await page.locator('#agent-message').waitFor();checks.push('reload retains session and the complete user list');
  assert.deepEqual(errors,[]);checks.push('no page runtime errors');
  await writeFile(path.join(out,'browser.json'),JSON.stringify({passed:true,checks,users:20,users_selected:selected,errors},null,2));
}catch(e){
  await writeFile(path.join(out,'browser.json'),JSON.stringify({passed:false,checks,users_selected:selected,errors,error:String(e)},null,2));throw e;
}finally{await browser.close();}
