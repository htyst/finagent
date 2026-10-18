"""Rehearse Git checkout + fresh dependencies + README startup in an owned copy."""
import argparse,hashlib,json,os,shutil,socket,sqlite3,subprocess,sys,tempfile,time,urllib.request,zipfile
from pathlib import Path
from contextlib import closing
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--timeout',type=int,default=600);args=parser.parse_args()
 identifier=uuid4().hex[:8];out=ROOT/'var/reviews/github-readiness-20261018'/('rehearsal-'+identifier);out.mkdir(parents=True)
 temp=Path(tempfile.mkdtemp(prefix='finagent-gh-'))/'TEST-ONLY';temp.mkdir();stage=temp/'source';clone=temp/'克隆演练 finagent'
 node=ROOT/'var/tools/node-v24.22.0-win-x64/node.exe'
 if not node.exists():node=Path(shutil.which('node') or '')
 if not node.is_file():raise SystemExit('A supported Node installation is required for the rehearsal')
 env=dict(os.environ,PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1',PATH=str(node.parent)+os.pathsep+os.environ['PATH'],FINAGENT_SCHEDULER_ENABLED='0',FINAGENT_SUBSCRIPTIONS_ENABLED='0',FINAGENT_BIRTHDAYS_ENABLED='0')
 for key in ['MODEL_API_KEY','FINAGENT_DB_PATH','FINAGENT_MODE','FINAGENT_IMPORT_TEST_DB','FINAGENT_REVIEW_DIR','PYTHONPATH']:env.pop(key,None)
 report=dict(passed=False,mode='same_machine_fresh_Git_checkout_and_independent_venv_and_npm_ci',clone=str(clone),checks=[],services_stopped=True,live_api_tested=False)
 def dump():(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
 def command(name,argv,cwd=ROOT,timeout=120):
  with (out/(name+'.log')).open('w',encoding='utf-8') as log:result=subprocess.run(list(map(str,argv)),cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=timeout)
  if result.returncode:raise RuntimeError(name+' failed, see isolated '+name+'.log')
  report['checks'].append(name);dump();print(json.dumps(dict(check=name,passed=True)),flush=True)
 process=None;service_log=None
 def listening(port):
  with socket.socket() as s:s.settimeout(.3);return s.connect_ex(('127.0.0.1',port))==0
 try:
  if any(listening(p) for p in (8000,5173)):raise RuntimeError('Existing service occupies 8000/5173; no service was stopped')
  archive='FinAgent-source-rehearsal-'+identifier+'.zip'
  command('package',[sys.executable,'-B',ROOT/'scripts/package-source.py','--name',archive])
  with zipfile.ZipFile(ROOT/'var/artifacts'/archive) as bundle:
   bundle.extractall(temp/'archive')
  shutil.copytree(temp/'archive/finagent',stage)
  assert not (stage/'.env').exists() and not (stage/'var').exists() and not (stage/'.venv').exists() and not (stage/'frontend/node_modules').exists()
  command('git-init',['git','init','--initial-branch=main'],stage)
  for name,value in [('user.name','FinAgent Source Check'),('user.email','source-check@example.invalid'),('core.autocrlf','true')]:command('git-config-'+name,['git','config',name,value],stage)
  (stage/'.env').write_text('# TEST-ONLY ignored local configuration\n',encoding='utf-8');(stage/'var').mkdir();(stage/'var/private-marker.txt').write_text('TEST-ONLY');(stage/'FinAgent.exe').write_bytes(b'TEST-ONLY not an executable')
  command('git-add',['git','add','--all'],stage)
  tracked=subprocess.check_output(['git','ls-files','-z'],cwd=stage).decode().split('\0')
  assert '.env' not in tracked and 'var/private-marker.txt' not in tracked and 'FinAgent.exe' not in tracked
  command('git-commit',['git','commit','-m','Verify clean source distribution'],stage)
  command('git-clone',['git','-c','core.autocrlf=true','clone',str(stage),str(clone)],temp)
  command('source-readiness',[sys.executable,'-B',clone/'scripts/check-github-readiness.py','--report',out/'clone-readiness.json'],clone)
  assert json.loads((out/'clone-readiness.json').read_text(encoding='utf-8'))['git_index_checked']
  assert not (clone/'.env').exists() and not (clone/'var').exists()
  command('setup',['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',clone/'scripts/setup.ps1','-Python',sys._base_executable],clone,args.timeout)
  py=clone/'.venv/Scripts/python.exe';assert py.exists()
  command('annual-import',['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',clone/'scripts/import-financial-demo.ps1'],clone,240)
  command('test-data',[py,'-B',clone/'scripts/prepare-test-data.py'],clone,240)
  command('imported-data-regression',[py,'-B','-m','pytest','backend/tests/test_financial_import.py','-q','-p','no:cacheprovider','--junitxml='+str(out/'imported-data.xml')],clone,240)
  with closing(sqlite3.connect(clone/'var/finagent.sqlite3')) as db:
   report['database']=dict(users=db.execute('select count(*) from users').fetchone()[0],transactions=db.execute('select count(*) from transactions').fetchone()[0],migration=db.execute('select version_num from alembic_version').fetchone()[0]);expected_version=db.execute('select dataset_version from demo_settings').fetchone()[0]
  assert report['database']==dict(users=20,transactions=25471,migration='0017_servicing')
  service_log=(out/'dev.log').open('w',encoding='utf-8')
  process=subprocess.Popen(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(clone/'scripts/dev.ps1'),'-Mode','mock'],cwd=clone,env=env,stdout=service_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
  report['services_stopped']=False;dump()
  opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
  for _ in range(120):
   if process.poll() is not None:raise RuntimeError('README dev.ps1 exited; inspect dev.log')
   try:
    with opener.open('http://127.0.0.1:8000/api/demo/info',timeout=2) as response:info=json.load(response)['data']
    with opener.open('http://127.0.0.1:5173',timeout=2) as response:html=response.read()
    if info['dataset_version']==expected_version and info['mode']=='mock' and b'root' in html:break
   except (OSError,ValueError,KeyError):pass
   time.sleep(.5)
  else:raise RuntimeError('README startup timeout')
  report['checks'].append('readme-dev-startup');dump()
  browser=r"""import assert from 'node:assert/strict';import {createRequire} from 'node:module';import {writeFile} from 'node:fs/promises';const require=createRequire(process.env.FINAGENT_CLONE+'/frontend/package.json');const {chromium}=require('playwright-core');const browser=await chromium.launch({channel:'msedge',headless:true});try{const page=await browser.newPage({viewport:{width:1280,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto('http://127.0.0.1:5173');await page.locator('#user').selectOption('u1');await page.locator('#agent-message').fill('查询我的余额');const result=page.waitForResponse(r=>r.request().method()==='POST'&&new URL(r.url()).pathname==='/api/tasks');await page.locator('#assistant').getByRole('button',{name:'发送',exact:true}).click();const response=await result;assert.equal(response.status(),200);const data=(await response.json()).data;assert.equal(data.mode,'mock');assert(data.steps.some(s=>s.tool_name==='list_accounts'&&s.status==='succeeded'));await page.screenshot({path:process.env.FINAGENT_SHOT,fullPage:true});assert.deepEqual(errors,[]);await writeFile(process.env.FINAGENT_BROWSER_REPORT,JSON.stringify({passed:true,mode:'normal_Mock_no_model_substitute',query_steps:data.steps.map(s=>({tool:s.tool_name,status:s.status})),console_errors:errors},null,2));}finally{await browser.close();}"""
  harness=out/'clone-browser.mjs';harness.write_text(browser,encoding='utf-8');env.update(FINAGENT_CLONE=str(clone),FINAGENT_SHOT=str(out/'mock-first-query.png'),FINAGENT_BROWSER_REPORT=str(out/'browser.json'))
  command('browser-first-query',[node,harness],clone,120)
  with closing(sqlite3.connect(clone/'var/finagent.sqlite3')) as db:assert db.execute('select count(*) from transfer_orders').fetchone()[0]==0
  report['checks'].append('zero_payment_writes');report['passed']=True
 except Exception as error:report['error']=str(error);report['error_type']=type(error).__name__
 finally:
  if process and process.poll() is None:
   subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
   try:process.wait(timeout=10)
   except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)
  if service_log:service_log.close()
  if process:
   for _ in range(30):
    if not any(listening(p) for p in (8000,5173)):break
    time.sleep(.2)
   report['services_stopped']=not any(listening(p) for p in (8000,5173))
  report['passed']=report['passed'] and report['services_stopped'];dump()
  print(json.dumps(dict(report=str(out/'report.json'),passed=report['passed'],services_stopped=report['services_stopped'],error=report.get('error')),ensure_ascii=False),flush=True)
 return 0 if report['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
