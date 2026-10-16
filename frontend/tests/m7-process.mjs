import { chromium } from 'playwright-core';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
const executablePath = [process.env.FINAGENT_BROWSER_PATH, 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', 'C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(p => p && existsSync(p));
const browser = await chromium.launch({ executablePath, headless: true });
const results = [];
const output = `../var/process-${Date.now()}.json`;
let page;
try {
  for (const scenario of ['same-name-transfer', 'unknown-purchase', 'conditional-transfer']) {
    for (const method of (scenario === 'unknown-purchase' ? ['agent','form'] : ['form','agent'])) {
      execFileSync(resolve('../.venv/Scripts/python.exe'), [resolve('../scripts/reset-process.py'), 'reset', '--date', '2026-09-24'], { env: process.env, stdio: 'pipe' });
      page = await browser.newPage({ viewport: { width: 1360, height: 1000 }, locale: 'zh-CN' });
      page.setDefaultTimeout(90000);
      await page.goto('http://127.0.0.1:5173', {waitUntil:'networkidle'});
      await page.locator('#user').selectOption('u1');
      await page.locator('.account-card > strong').filter({hasText:'¥10,000.00'}).waitFor();
      const agent = page.locator('.agent-section');
      const op = page.locator('.operation-section');
      const result = { scenario, method, mode: process.env.FINAGENT_MODE, passed:false, interactions:0, confirmation_wait_seconds:null, human_comprehension:null };
      results.push(result);
      const start = performance.now();
      const click = async locator => { await locator.click(); result.interactions++; };
      const select = async (locator,value) => { await locator.selectOption(value); result.interactions++; };
      const fill = async (locator,value) => { await locator.fill(value); result.interactions++; };
      const send = async message => { await fill(agent.locator('#agent-message'), message); await click(agent.getByRole('button',{name:'发送',exact:true})); };
      if (method === 'agent') {
        if (scenario === 'same-name-transfer') {
          await send('从活期账户给张三转100元');
          await agent.getByText('任务状态：待补充信息',{exact:true}).waitFor();
          await send('选尾号3821');
        } else if (scenario === 'unknown-purchase') {
          await send('昨晚有笔不认识的消费，帮我查是哪张卡；如果是尾号1234，帮我临时冻结它，再告诉我账户可用余额。');
        } else await send('如果本月餐饮超过500元，就从活期转100元到储蓄账户。');
      } else {
        if (scenario === 'unknown-purchase') {
          const row = page.locator('.transaction').filter({hasText:'待核对商户'});
          await row.getByText('卡尾号 1234',{exact:false}).waitFor();
          result.read_evidence = await row.innerText();
        }
        if (scenario === 'conditional-transfer') {
          const spending = page.locator('.spending-section');
          await select(spending.getByRole('combobox',{name:'类别',exact:true}), '餐饮');
          await click(spending.getByRole('button',{name:'查询',exact:true}));
          await spending.locator('.spending-summary > div > strong').filter({hasText:'¥1,200.00'}).waitFor();
          result.read_evidence = 'Net spending 120000 minor > 50000; manual judgment; no server condition attached to manual proposal';
        }
        await click(page.locator('.manual-form summary'));
        if (scenario === 'unknown-purchase') {
          await select(op.getByRole('combobox',{name:'操作类型'}), 'card_action');
          await select(op.getByRole('combobox',{name:'卡片',exact:true}), 'c1a');
        } else {
          await select(op.getByRole('combobox',{name:'付款账户'}), 'a1c');
          if (scenario === 'same-name-transfer') await select(op.getByRole('combobox',{name:'目标类型'}), 'saved_recipient');
          await select(op.getByRole('combobox',{name:'收款目标'}), scenario === 'same-name-transfer' ? 'r1' : 'a1s');
          await fill(op.getByLabel('金额（元）'), '100.00');
        }
        await click(op.getByRole('button',{name:'准备操作单'}));
      }
      const area = method === 'agent' ? agent : op;
      await area.getByRole('button',{name:'确认执行',exact:true}).waitFor();
      const prepared = performance.now();
      await click(area.getByRole('button',{name:'确认执行',exact:true}));
      await area.locator('.receipt').getByText(scenario === 'unknown-purchase' ? '卡片已冻结' : '模拟转账成功').waitFor();
      await page.locator('.account-card > strong').filter({hasText:scenario === 'unknown-purchase' ? '¥10,000.00' : '¥9,900.00'}).waitFor();
      if (method === 'agent') await agent.getByText('任务状态：已完成',{exact:true}).waitFor();
      result.preparation_seconds=(prepared-start)/1000;
      result.total_seconds=(performance.now()-start)/1000;
      result.business = JSON.parse(execFileSync(resolve('../.venv/Scripts/python.exe'), [resolve('../scripts/process-snapshot.py')], { env:process.env,encoding:'utf8' }));
      if (result.business.business_results !== 1 || result.business.source_balance !== (scenario === 'unknown-purchase' ? 1000000 : 990000) || result.business.card !== (scenario === 'unknown-purchase' ? 'frozen' : 'active') || result.business.savings_balance !== (scenario === 'conditional-transfer' ? 2010000 : 2000000)) throw new Error('DB business result mismatch');
      result.passed=true;
      await page.close();
    }
  }
} catch(error) { results.at(-1).error=error.message; throw error; }
finally {
  await browser.close();
  mkdirSync('../var',{recursive:true});
  writeFileSync(output, JSON.stringify({kind:'developer automated process experiment; not human research',results},null,2));
  console.log(output);
}
