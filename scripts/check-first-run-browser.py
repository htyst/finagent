"""Verify a clean annual bank in the real UI, isolated ports, Mock only."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4
import httpx

ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/'var/TEST-ONLY'/('annual-first-run-browser-'+uuid4().hex)
RUN.mkdir(parents=True)
def port():
    with socket.socket() as connection:
        connection.bind(('127.0.0.1',0))
        return connection.getsockname()[1]
api_port,ui_port=port(),port()
node=shutil.which('node')
if not node:raise SystemExit('Node required')
env=dict(os.environ,PYTHONPATH=str(ROOT/'backend'),PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1',
         FINAGENT_DB_PATH=str(RUN/'bank.sqlite3'),FINAGENT_MODE='mock',FINAGENT_WORKFLOWS_ENABLED='0',
         FINAGENT_SCHEDULER_ENABLED='0',FINAGENT_SUBSCRIPTIONS_ENABLED='0',FINAGENT_BIRTHDAYS_ENABLED='0',
         FINAGENT_ALLOWED_ORIGIN=f'http://127.0.0.1:{ui_port}',FINAGENT_API_PROXY_TARGET=f'http://127.0.0.1:{api_port}',
         FINAGENT_E2E_BASE_URL=f'http://127.0.0.1:{ui_port}',FINAGENT_E2E_OUTPUT=str(RUN))
processes=[]
report=dict(passed=False,mode='Mock + real annual bank + Edge',real_model_calls=0)
try:
    with (RUN/'services.log').open('w',encoding='utf-8') as log:
        subprocess.run([sys.executable,'-B','scripts/initialize-demo.py'],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=180)
        for cmd,cwd in [([sys.executable,'-B','-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(api_port)],ROOT),
                        ([node,'node_modules/vite/bin/vite.js','--host','127.0.0.1','--port',str(ui_port),'--strictPort'],ROOT/'frontend')]:
            processes.append(subprocess.Popen(cmd,cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0))
        with httpx.Client(timeout=1,trust_env=False) as client:
            for _ in range(120):
                if any(p.poll() is not None for p in processes):raise RuntimeError('Isolated service exited')
                try:
                    if client.get(env['FINAGENT_E2E_BASE_URL']+'/api/health').status_code==200:break
                except httpx.HTTPError:pass
                time.sleep(.2)
            else:raise RuntimeError('Startup timeout')
        with (RUN/'browser.log').open('w',encoding='utf-8') as browser_log:
            result=subprocess.run([node,'tests/first-run-browser.mjs'],cwd=ROOT/'frontend',env=env,
                                  stdout=browser_log,stderr=subprocess.STDOUT,timeout=180)
        report['returncode']=result.returncode
        report['passed']=result.returncode==0
except Exception as error:
    report['error']=type(error).__name__+': '+str(error)
finally:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)
    report['services_stopped']=all(p.poll() is not None for p in processes)
    report['output']=str(RUN)
    (RUN/'runner.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))
raise SystemExit(0 if report['passed'] and report['services_stopped'] else 1)
