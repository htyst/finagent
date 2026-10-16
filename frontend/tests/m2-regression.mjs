import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';

// Exercise the built UI with controlled response ordering, without a live model
// or writes to the demo database. API correctness is covered by backend tests.
const dist = resolve(dirname(fileURLToPath(import.meta.url)), '../dist');
let server, browser, baseUrl;
before(async () => {
  if (!existsSync(resolve(dist, 'index.html'))) throw new Error('请先运行 npm run build');
  server = createServer((req, res) => {
    const pathname = new URL(req.url, 'http://localhost').pathname;
    const file = resolve(dist, '.' + (pathname === '/' ? '/index.html' : pathname));
    if (!file.startsWith(dist + sep) || !existsSync(file)) { res.writeHead(404).end(); return; }
    res.setHeader('Content-Type', {'.js':'text/javascript', '.css':'text/css', '.html':'text/html'}[extname(file)] || 'application/octet-stream');
    res.end(readFileSync(file));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  baseUrl = `http://127.0.0.1:${server.address().port}`;
  const executablePath = [process.env.FINAGENT_BROWSER_PATH,
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(p => p && existsSync(p));
  if (!executablePath) throw new Error('未找到 Edge；请设置 FINAGENT_BROWSER_PATH');
  browser = await chromium.launch({executablePath, headless:true});
});
after(async () => {
  await browser?.close();
  if (server) await new Promise(resolve => server.close(resolve));
});

const users = [{id:'u1',display_name:'林晓',profile:'演示一'}, {id:'u2',display_name:'陈予',profile:'演示二'}];
const account = owner => ({id:`${owner}-account`,alias:`${owner}账户`,account_mask:'**4082',balance_minor:owner === 'u1' ? 1000000 : 680000,status:'active'});
const card = owner => ({id:`${owner}-card`,alias:`${owner}卡片`,brand:'银联',card_mask:owner === 'u1' ? '**1234' : '**2233',account_id:`${owner}-account`,status:'active'});
const transaction = owner => ({id:`${owner}-transaction`,account_id:`${owner}-account`,card_id:`${owner}-card`,type:'purchase',direction:'debit',amount_minor:100,category:'餐饮',merchant:`${owner}商户`,posted_at_utc:'2026-09-08T04:00:00+00:00',status:'posted'});

test('助手展示本次流水，翻页保留条件，新任务不残留旧明细', async () => {
  const page = await browser.newPage();
  await fixture(page);
  const filters = {start:'2026-08-01',end:'2026-08-31',category:'餐饮',account_id:'u1-account',card_id:'u1-card'};
  const rows = Array.from({length:21}, (_, i) => ({...transaction('u1'), id:`august-${i}`,merchant:`八月餐饮${i + 1}`,posted_at_utc:'2026-08-15T04:00:00+00:00'}));
  let task = {id:'query-august',mode:'mock',intent:'transactions',status:'completed',proposal:null,
    plan_version:1,candidates:[],messages:[{role:'assistant',content:'以下展示前20条。'}],
    steps:[{ordinal:1,tool_name:'list_transactions',status:'succeeded',arguments:filters,
      result:{items:rows.slice(0,20),total:21,limit:20,offset:0}}]};
  const requested = [];
  await page.route('**/api/agent/tasks/current', route => route.fulfill({json:{status:'ok',data:task}}));
  await page.route('**/api/transactions?**', route => {
    const params = new URL(route.request().url()).searchParams;
    if (params.get('limit') !== '20') return route.fallback();
    requested.push(params);
    const offset = Number(params.get('offset'));
    return route.fulfill({json:{status:'ok',data:{items:rows.slice(offset,offset + 20),total:21,limit:20,offset}}});
  });
  try {
    await page.goto(baseUrl);
    const evidence = page.locator('.agent-transaction-evidence');
    await evidence.waitFor();
    assert.match(await evidence.innerText(), /2026-08-01 至 2026-08-31 · 餐饮/);
    assert.equal(await evidence.locator('.transaction').count(), 20);
    assert.match(await evidence.innerText(), /八月餐饮1/);
    assert.doesNotMatch(await evidence.innerText(), /u1商户/);
    // A separate page-level form must not change the assistant query's scope.
    await page.locator('.spending-filters select').nth(1).selectOption('购物');
    await evidence.getByRole('button', {name:'下一页流水'}).click();
    await evidence.getByText('八月餐饮21', {exact:true}).waitFor();
    assert.equal(await evidence.locator('.transaction').count(), 1);
    assert.equal(requested.at(-1).get('offset'), '20');
    for (const [name, value] of Object.entries(filters)) assert.equal(requested.at(-1).get(name), value);
    assert.equal(await evidence.getByRole('button', {name:'下一页流水'}).isDisabled(), true);
    await evidence.getByRole('button', {name:'上一页流水'}).click();
    await evidence.getByText('八月餐饮1', {exact:true}).waitFor();
    assert.equal(requested.at(-1).get('offset'), '0');
    task = {...task,id:'query-new',steps:[{...task.steps[0],arguments:{category:'购物'},
      result:{items:[{...rows[0],id:'new-row',category:'购物',merchant:'新的购物记录'}],total:1,limit:20,offset:0}}]};
    await page.locator('.agent-section').getByRole('button', {name:'重新查询状态'}).click();
    await evidence.getByText('新的购物记录', {exact:true}).waitFor();
    assert.doesNotMatch(await evidence.innerText(), /八月餐饮/);
    assert.equal(await evidence.locator('.transaction').count(), 1);
    // A completed task must restore its own evidence after a full page reload.
    await page.reload();
    await evidence.getByText('新的购物记录', {exact:true}).waitFor();
  } finally { await page.close(); }
});

function spending(params, owner) {
  const category = params.get('category');
  const offset = Number(params.get('evidence_offset') || 0);
  const total = category === '全部' ? 8 : 3;
  const net = category === '全部' ? 135500 : 120000;
  const rows = Array.from({length:total}, (_, i) => ({transaction_id:`e${i}`,type:'purchase',amount_minor:100,signed_minor:100,category:'餐饮',merchant:`${owner}商户`,posted_at_utc:'2026-09-08T04:00:00+00:00',original_transaction_id:null}));
  return {account_ids:[`${owner}-account`],category:category === '全部' ? null : category,
    rules_version:'spending-v1',dataset_version:'regression',comparison:null,
    current:{period:{start_date:'2026-09-01',end_exclusive_date:'2026-09-25'},
      coverage:{status:'complete',missing_account_ids:[]},
      totals:{purchase_minor:net+5000,refund_minor:5000,net_minor:net,purchase_count:total-1,refund_count:1,is_complete:true},
      categories:[],evidence:{total,limit:5,offset,items:rows.slice(offset,offset+5)}}};
}

async function fixture(page, delayed = '') {
  let owner = 'u1', held = false, release, notifyHeld;
  const gate = new Promise(resolve => { release = resolve; });
  const waiting = new Promise(resolve => { notifyHeld = resolve; });
  const spendingRequests = [];
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url()), requestOwner = owner;
    let data, status = 200;
    if (url.pathname === '/api/demo/info') data = {demo_date:'2026-09-24',dataset_version:'regression',seed_version:'regression',mode:'mock'};
    else if (url.pathname === '/api/demo/users') data = users;
    else if (url.pathname === '/api/demo/session') {
      if (request.method() === 'POST') owner = request.postDataJSON().user_id;
      data = {user:users.find(u => u.id === owner)};
      if (delayed === 'session' && request.method() === 'GET' && !held) {
        held = true; notifyHeld(); await gate;
      }
    } else if (url.pathname === '/api/accounts') {
      data = [account(requestOwner)];
      if (delayed.startsWith('accounts') && requestOwner === 'u1' && !held) {
        held = true; notifyHeld(); await gate;
        if (delayed === 'accounts-error') status = 500;
      }
    } else if (url.pathname === '/api/cards') data = [card(requestOwner)];
    else if (url.pathname === '/api/recipients') data = [];
    else if (url.pathname === '/api/tasks/current') data = null;
    else if (url.pathname === '/api/agent/tasks/current') data = null;
    else if (url.pathname === '/api/transactions') data = {items:[transaction(requestOwner)],total:1,limit:10,offset:0};
    else if (url.pathname === '/api/spending') {
      spendingRequests.push(url.searchParams);
      data = spending(url.searchParams, requestOwner);
    } else throw new Error(`Unexpected API ${url.pathname}`);
    await route.fulfill({status,json:{status:status === 200 ? 'ok' : 'error',data,error_code:status === 200 ? null : 'DATA_UNAVAILABLE',message:status === 200 ? '' : '旧请求失败',request_id:'regression'}});
  });
  return {release,waiting,spendingRequests};
}

for (const delayed of ['accounts', 'session', 'accounts-error']) {
  test(`用户切换后忽略旧响应：${delayed}`, async () => {
    const page = await browser.newPage();
    const control = await fixture(page, delayed);
    try {
      await page.goto(baseUrl);
      await control.waiting;
      await page.locator('#user').selectOption('u2');
      await page.waitForFunction(() => document.querySelector('.account-card strong')?.textContent.includes('6,800.00'));
      const oldResponse = page.waitForResponse(r => r.url().endsWith(delayed === 'session' ? '/api/demo/session' : '/api/accounts') && r.request().method() === 'GET');
      control.release();
      await (await oldResponse).finished();
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await page.locator('#user').inputValue(), 'u2');
      assert.match(await page.locator('.user-box small').innerText(), /陈予/);
      assert.match(await page.locator('.account-card').innerText(), /6,800\.00/);
      assert.match(await page.locator('.bank-card').innerText(), /2233/);
      assert.match(await page.locator('.transaction-list').innerText(), /u2商户/);
      assert.equal(await page.locator('.alert').count(), 0);
      assert.doesNotMatch(await page.locator('main').innerText(), /10,000\.00|u1商户/);
    } finally { control.release(); await page.close(); }
  });
}

test('未提交的筛选条件不影响证据翻页，新查询从第一页开始', async () => {
  const page = await browser.newPage();
  const control = await fixture(page);
  try {
    await page.goto(baseUrl);
    await page.locator('.spending-summary').waitFor();
    const category = page.locator('.spending-filters select').nth(1);
    const pager = page.locator('.evidence-list + .pager');
    await category.selectOption('全部');
    await page.locator('.spending-filters button').click();
    await page.waitForFunction(() => document.querySelector('.evidence-head').textContent.includes('8 条'));
    await category.selectOption('餐饮');
    await page.locator('.spending-filters input[type=date]').first().fill('2026-09-08');
    await pager.getByRole('button', {name:'下一页'}).click();
    await page.waitForFunction(() => document.querySelector('.evidence-list + .pager').textContent.includes('2 / 2'));
    const paged = control.spendingRequests.at(-1);
    assert.equal(paged.get('category'), '全部');
    assert.equal(paged.get('start'), null);
    assert.equal(paged.get('comparison_mode'), 'previous_corresponding');
    assert.equal(paged.get('evidence_offset'), '5');
    assert.equal(await page.locator('.evidence-item').count(), 3);
    assert.match(await page.locator('.spending-summary').innerText(), /全部类别/);
    await page.locator('.spending-filters button').click();
    await page.waitForFunction(() => document.querySelector('.evidence-list + .pager').textContent.includes('1 / 1'));
    const applied = control.spendingRequests.at(-1);
    assert.equal(applied.get('category'), '餐饮');
    assert.equal(applied.get('start'), '2026-09-08');
    assert.equal(applied.get('comparison_mode'), 'none');
    assert.equal(applied.get('evidence_offset'), '0');
    assert.equal(await page.locator('.evidence-item').count(), 3);
  } finally { await page.close(); }
});


test('失败后重试保留本次查询条件，未提交的表单修改不影响重试', async () => {
  const page = await browser.newPage();
  const control = await fixture(page);
  try {
    await page.goto(baseUrl);
    await page.locator('.spending-summary').waitFor();
    const filters = page.locator('.spending-filters');
    await filters.getByRole('combobox', {name:'类别', exact:true}).selectOption('全部');
    await filters.locator('input[type=date]').first().fill('2026-09-08');
    await page.route('**/api/spending?**', route => route.fulfill({status:503,json:{status:'error',data:null,error_code:'DATA_UNAVAILABLE',message:'临时统计故障'}}), { times: 1 });
    await filters.getByRole('button', {name:'查询', exact:true}).click();
    await page.locator('.spending-section [role=alert]').getByText('临时统计故障', {exact:false}).waitFor();
    await filters.getByRole('combobox', {name:'类别', exact:true}).selectOption('购物');
    await page.getByRole('button', {name:'重试统计查询'}).click();
    await page.locator('.spending-summary').waitFor();
    const retried = control.spendingRequests.at(-1);
    assert.equal(retried.get('category'), '全部');
    assert.equal(retried.get('start'), '2026-09-08');
    assert.equal(retried.get('comparison_mode'), 'none');
    assert.match(await page.locator('.spending-summary').innerText(), /全部类别/);
  } finally { await page.close(); }
});
