"""Run the actual G01 UI with a clearly isolated model substitute, no provider API."""
import json,os,socket,subprocess,sys,time,shutil
from pathlib import Path
from uuid import uuid4
import httpx
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/os.getenv('FINAGENT_REVIEW_DIR','var/reviews/g01-phase1-20261017');RUN=ROOT/'var/TEST-ONLY'/('g01-browser-'+uuid4().hex);RUN.mkdir(parents=True)
def port():
 with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]
a,b=port(),port();node=ROOT/'var/tools/node-v24.22.0-win-x64/node.exe'
if not node.is_file():
 node=shutil.which('node')
 if not node:raise SystemExit('Node is required')
env=dict(os.environ,PYTHONPATH=str(ROOT/'backend'),PYTHONDONTWRITEBYTECODE='1',PYTHONIOENCODING='utf-8',FINAGENT_DB_PATH=str(RUN/'bank.sqlite3'),FINAGENT_MODE='live',FINAGENT_AGENT_ENGINE='dynamic',FINAGENT_G01_BROWSER_FIXTURE='1',FINAGENT_SCHEDULER_ENABLED='0',FINAGENT_SUBSCRIPTIONS_ENABLED='0',FINAGENT_BIRTHDAYS_ENABLED='0',FINAGENT_ALLOWED_ORIGIN=f'http://127.0.0.1:{b}',FINAGENT_API_PROXY_TARGET=f'http://127.0.0.1:{a}',FINAGENT_E2E_BASE_URL=f'http://127.0.0.1:{b}',FINAGENT_E2E_OUTPUT=str(RUN))
processes=[];report=dict(output=str(RUN),mode='Edge+model_substitute+real_domain',passed=False)
import hashlib
manifest=json.loads((OUT/'backend-tested-source.json').read_text(encoding='utf-8'))
if any(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=sha for name,sha in manifest.items()):raise SystemExit('Source changed before browser evaluation')
report['source_version']=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
try:
 with (RUN/'services.log').open('w',encoding='utf-8') as log:
  subprocess.run([sys.executable,'-B','-m','app.data.cli','reset','--date','2026-09-24'],env=env,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=60)
  for cmd,cwd in [([sys.executable,'-B','-m','uvicorn','evals.g01_browser_app:app','--host','127.0.0.1','--port',str(a)],ROOT),([str(node),'node_modules/vite/bin/vite.js','--host','127.0.0.1','--port',str(b),'--strictPort'],ROOT/'frontend')]:
   processes.append(subprocess.Popen(cmd,cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0))
  with httpx.Client(timeout=1,trust_env=False) as client:
   for _ in range(100):
    if any(p.poll() is not None for p in processes):raise RuntimeError('Isolated service exited')
    try:
     if client.get(env['FINAGENT_E2E_BASE_URL']+'/api/health').json()['data']['test_only']:break
    except (httpx.HTTPError,ValueError,KeyError):pass
    time.sleep(.2)
   else:raise RuntimeError('Startup timeout')
  with (RUN/'browser.log').open('w',encoding='utf-8') as log:
   completed=subprocess.run([str(node),'tests/g01-browser.mjs'],cwd=ROOT/'frontend',env=env,stdout=log,stderr=subprocess.STDOUT,timeout=240)
  report['returncode']=completed.returncode;report['passed']=completed.returncode==0
except Exception as error:report['error']=type(error).__name__
finally:
 for process in reversed(processes):
  if process.poll() is None:process.terminate()
  try:process.wait(timeout=8)
  except subprocess.TimeoutExpired:process.kill();process.wait(timeout=8)
 report['services_stopped']=all(p.poll() is not None for p in processes)
 (OUT/'browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))