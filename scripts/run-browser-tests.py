"""Launch only isolated TEST-ONLY services and run the actual browser suites."""
import json
import argparse
import os
from pathlib import Path
import socket
import subprocess
import shutil
import sys
import time
from uuid import uuid4
import httpx

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); return sock.getsockname()[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--suites',default='p0-browser.mjs,p1-browser.mjs')
    parser.add_argument('--mode',choices=['mock','live'],default='mock')
    parser.add_argument('--api-budget',type=int,default=30)
    args=parser.parse_args()
    suites=args.suites.split(',')
    if not set(suites)<={'p0-browser.mjs','p1-browser.mjs','dialogue-browser.mjs','redesign-browser.mjs',
                          'directory-browser.mjs','freeze-browser.mjs','initialize-browser.mjs','sms-popup-browser.mjs','task-sms-browser.mjs'}:parser.error('Unknown suite')
    if args.mode=='live' and suites!=['dialogue-browser.mjs']:parser.error('Live uses only the budgeted dialogue suite')
    if 'dialogue-browser.mjs' in suites and args.mode!='live':parser.error('Dialogue browser suite requires actual Live')
    run = ROOT / 'var/TEST-ONLY' / ('p1-browser-' + uuid4().hex)
    run.mkdir(parents=True)
    node = ROOT / 'var/tools/node-v24.22.0-win-x64/node.exe'
    if not node.is_file():
        installed = shutil.which('node')
        if not installed: raise SystemExit('Node.js is required; run setup-node.ps1 or install Node 24')
        node = Path(installed)
    report = {'run': str(run), 'status': 'running', 'suites': []}
    try:
        for suite in suites:
            directory = run / suite.removesuffix('.mjs'); directory.mkdir()
            api_port, ui_port = port(), port()
            env = dict(os.environ, PYTHONPATH=str(ROOT / 'backend'), PYTHONIOENCODING='utf-8',
                FINAGENT_DB_PATH=str(directory / 'browser.sqlite3'), FINAGENT_MODE=args.mode,
                FINAGENT_E2E_API_BUDGET=str(args.api_budget),
                FINAGENT_SCHEDULER_ENABLED='0', FINAGENT_SUBSCRIPTIONS_ENABLED='0', FINAGENT_BIRTHDAYS_ENABLED='0',
                FINAGENT_ALLOWED_ORIGIN=f'http://127.0.0.1:{ui_port}', FINAGENT_API_PROXY_TARGET=f'http://127.0.0.1:{api_port}',
                FINAGENT_E2E_BASE_URL=f'http://127.0.0.1:{ui_port}', FINAGENT_E2E_OUTPUT=str(directory))
            approver = directory / 'approver.key'
            approver.write_text('TEST-ONLY-' + uuid4().hex, encoding='utf-8')
            env['FINAGENT_APPROVER_KEY_FILE'] = str(approver)
            env['FINAGENT_E2E_APPROVER_KEY'] = approver.read_text(encoding='utf-8')
            processes = []
            with (directory / 'services.log').open('w', encoding='utf-8') as log:
                try:
                    subprocess.run([sys.executable, '-m', 'app.data.cli', 'reset', '--date', '2026-09-24'],
                        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=60)
                    entry='evals.browser_app:app' if args.mode=='live' else 'app.main:app'
                    for cmd, cwd in [([sys.executable, '-m', 'uvicorn', entry, '--host', '127.0.0.1', '--port', str(api_port)], ROOT),
                        ([str(node), 'node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', str(ui_port), '--strictPort'], ROOT / 'frontend')]:
                        processes.append(subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0))
                    with httpx.Client(timeout=1, trust_env=False) as client:
                        for _ in range(100):
                            if any(p.poll() is not None for p in processes): raise RuntimeError('Isolated services exited; inspect services.log')
                            try:
                                if client.get(env['FINAGENT_E2E_BASE_URL']+'/api/health').json()['data']['test_only']: break
                            except (httpx.HTTPError, ValueError, KeyError): pass
                            time.sleep(.2)
                        else: raise RuntimeError('Isolated service startup timed out')
                    with (directory / 'browser.log').open('w', encoding='utf-8') as output:
                        completed = subprocess.run([str(node), 'tests/' + suite], cwd=ROOT / 'frontend', env=env,
                            stdout=output, stderr=subprocess.STDOUT, timeout=600 if args.mode=='live' else 240)
                    report['suites'].append({'suite': suite, 'exit_code': completed.returncode, 'log': str(directory/'browser.log')})
                    if completed.returncode: raise RuntimeError('Browser suite failed: ' + suite)
                finally:
                    for process in reversed(processes):
                        if process.poll() is None: process.terminate()
                        try: process.wait(timeout=5)
                        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        report['status'] = 'passed'
    except Exception as error:
        report.update(status='failed', error=type(error).__name__ + ': ' + str(error))
    finally:
        (run / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report['status'] == 'passed' else 1)


if __name__ == '__main__': main()
