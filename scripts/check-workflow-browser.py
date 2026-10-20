"""Isolated UI verification: model substitute, synthetic data, no real API calls."""
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
import argparse

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--test',choices=['workflow','g01'],default='workflow')
args=parser.parse_args()

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'var/TEST-ONLY' / (args.test+'-browser-' + uuid4().hex)
RUN.mkdir(parents=True)


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


api_port, ui_port = port(), port()
node = shutil.which('node')
if not node:
    raise SystemExit('Node required')
env = dict(os.environ, PYTHONPATH=str(ROOT/'backend'), PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
    FINAGENT_DB_PATH=str(RUN/'bank.sqlite3'), FINAGENT_MODE='live', FINAGENT_AGENT_ENGINE='dynamic',
    FINAGENT_G01_BROWSER_FIXTURE='1', FINAGENT_SCHEDULER_ENABLED='0', FINAGENT_SUBSCRIPTIONS_ENABLED='0',
    FINAGENT_BIRTHDAYS_ENABLED='0', FINAGENT_WORKFLOWS_ENABLED='0',
    FINAGENT_ALLOWED_ORIGIN=f'http://127.0.0.1:{ui_port}', FINAGENT_API_PROXY_TARGET=f'http://127.0.0.1:{api_port}',
    FINAGENT_E2E_BASE_URL=f'http://127.0.0.1:{ui_port}', FINAGENT_E2E_OUTPUT=str(RUN), FINAGENT_E2E_PYTHON=sys.executable)
processes = []
report = dict(passed=False, output=str(RUN), mode='Edge+deterministic model substitute+real domain')
try:
    with (RUN/'services.log').open('w', encoding='utf-8') as log:
        subprocess.run([sys.executable,'-B','-m','app.data.cli','reset','--date','2026-09-24'],cwd=ROOT,
            env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=60)
        for cmd,cwd in [([sys.executable,'-B','-m','uvicorn','evals.g01_browser_app:app','--host','127.0.0.1','--port',str(api_port)],ROOT),
            ([node,'node_modules/vite/bin/vite.js','--host','127.0.0.1','--port',str(ui_port),'--strictPort'],ROOT/'frontend')]:
            processes.append(subprocess.Popen(cmd,cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0))
        with httpx.Client(timeout=1,trust_env=False) as client:
            for _ in range(100):
                if any(p.poll() is not None for p in processes):
                    raise RuntimeError('Isolated service exited')
                try:
                    if client.get(env['FINAGENT_E2E_BASE_URL']+'/api/health').status_code==200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.2)
            else:
                raise RuntimeError('Startup timeout')
        with (RUN/'browser.log').open('w',encoding='utf-8') as log:
            completed=subprocess.run([node,'tests/'+args.test+'-browser.mjs'],cwd=ROOT/'frontend',env=env,
                stdout=log,stderr=subprocess.STDOUT,timeout=180)
        report['passed']=completed.returncode==0
        report['returncode']=completed.returncode
except Exception as error:
    report['error']=str(error)
finally:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    report['services_stopped']=all(p.poll() is not None for p in processes)
    (RUN/'runner.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)
raise SystemExit(0 if report['passed'] else 1)
