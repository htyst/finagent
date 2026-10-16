"""Real local server smoke test against a newly created TEST-ONLY database.

Run with the project venv Python. Never touches the normal demo database.
Authentication values remain in process memory and the local simulator inbox.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4
import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    run = ROOT / 'var' / 'TEST-ONLY' / ('server-smoke-' + uuid4().hex)
    run.mkdir(parents=True)
    database = run / 'server.sqlite3'
    env = dict(os.environ, PYTHONPATH=str(ROOT / 'backend'), PYTHONIOENCODING='utf-8',
               FINAGENT_DB_PATH=str(database), FINAGENT_MODE='mock',
               FINAGENT_SCHEDULER_ENABLED='0', FINAGENT_SUBSCRIPTIONS_ENABLED='0', FINAGENT_BIRTHDAYS_ENABLED='0')
    result = {'passed': [], 'status': 'running', 'database': str(database)}
    server = None
    delivered = []
    try:
        with (run / 'reset.log').open('w', encoding='utf-8') as log:
            subprocess.run([sys.executable, '-m', 'app.data.cli', 'reset', '--date', '2026-10-15'],
                           cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=60)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        env['FINAGENT_ALLOWED_ORIGIN'] = base
        with (run / 'server.log').open('w', encoding='utf-8') as log:
            server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            with httpx.Client(base_url=base, trust_env=False, timeout=5) as client:
                for _ in range(60):
                    if server.poll() is not None:
                        raise RuntimeError('Backend exited; see server.log')
                    try:
                        if client.get('/api/health').json()['data']['test_only'] is True:
                            break
                    except (httpx.HTTPError, ValueError, KeyError):
                        pass
                    time.sleep(0.25)
                else:
                    raise RuntimeError('Isolated server did not become ready')
                client.post('/api/demo/session', json={'user_id': 'u1'}).raise_for_status()
                csrf = client.get('/api/demo/csrf').json()['data']['csrf_token']
                client.headers.update({'Origin': base, 'X-FinAgent-CSRF': csrf, 'X-FinAgent-Expected-User': 'u1'})

                def post(path, payload=None, expected=200):
                    response = client.post(path, json=payload)
                    body = response.json()
                    if response.status_code != expected:
                        raise AssertionError(f'{path}: expected {expected}, got {response.status_code}, code={body.get("error_code")}')
                    return body.get('data') if expected == 200 else body

                def balance():
                    return next(row['balance_minor'] for row in client.get('/api/accounts').json()['data'] if row['id'] == 'a1c')

                def prepare(amount):
                    return post('/api/transfers/prepare', {'source_account_id': 'a1c',
                        'target': {'type': 'own_account', 'account_id': 'a1s'}, 'amount_minor': amount})['proposal']['id']

                before = balance()
                result['passed'].append('real-server-session-and-green-read')
                yellow = prepare(10000)
                token = post(f'/api/proposals/{yellow}/confirmation-token')['confirmation_token']
                receipt = post(f'/api/proposals/{yellow}/confirm', {'confirmation_token': token})
                assert receipt['status'] == 'executed' and balance() == before - 10000
                assert post(f'/api/proposals/{yellow}/confirm', {'confirmation_token': token})['order_id'] == receipt['order_id']
                result['passed'].append('yellow-transfer-and-idempotent-receipt')
                red = prepare(200000)
                token = post(f'/api/proposals/{red}/confirmation-token')['confirmation_token']
                denied = post(f'/api/proposals/{red}/confirm', {'confirmation_token': token}, expected=403)
                assert denied['error_code'] == 'MFA_REQUIRED' and balance() == before - 10000
                result['passed'].append('red-transfer-denied-with-ordinary-token')
                challenge = post(f'/api/security/operations/proposal/{red}/challenge')
                assert not {'code', 'code_hash', 'secret'} & set(challenge)
                inbox = ROOT / 'var' / 'auth-simulator' / (challenge['challenge_id'] + '.json')
                delivered.append(inbox)
                code = json.loads(inbox.read_text(encoding='utf-8'))['code']
                post(f'/api/security/operations/proposal/{red}/verify', {'challenge_id': challenge['challenge_id'], 'code': code})
                assert post(f'/api/proposals/{red}/confirm', {'confirmation_token': token})['status'] == 'executed'
                assert balance() == before - 210000
                result['passed'].append('independent-simulated-proof-and-red-execution')
                assert post(f'/api/security/operations/proposal/{red}/verify',
                    {'challenge_id': challenge['challenge_id'], 'code': code}, expected=409)['error_code'] == 'INVALID_ORDER_STATE'
                result['passed'].append('used-proof-cannot-be-verified-again')
                query = client.get('/api/subscriptions')
                assert query.status_code == 403
                post('/api/subscriptions/query-confirmation')
                assert client.get('/api/subscriptions').status_code == 200
                result['passed'].append('yellow-subscription-query')
                third = prepare(10000)
                challenge = post(f'/api/security/operations/proposal/{third}/challenge')
                inbox = ROOT / 'var' / 'auth-simulator' / (challenge['challenge_id'] + '.json')
                delivered.append(inbox)
                actual = json.loads(inbox.read_text(encoding='utf-8'))['code']
                wrong = str((int(actual[0]) + 1) % 10) + actual[1:]
                for _ in range(5):
                    assert post(f'/api/security/operations/proposal/{third}/verify',
                        {'challenge_id': challenge['challenge_id'], 'code': wrong}, expected=403)['error_code'] == 'MFA_INVALID'
                assert post(f'/api/security/operations/proposal/{third}/challenge', expected=423)['error_code'] == 'SECURITY_LOCKED'
                assert balance() == before - 210000
                assert post(f'/api/proposals/{third}/cancel')['status'] == 'cancelled'
                result['passed'].append('persistent-lock-green-read-and-cancellation')
            result['status'] = 'passed'
    except BaseException as error:
        result.update(status='failed', error=type(error).__name__ + ': ' + str(error))
        raise
    finally:
        if server and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        for inbox in delivered:
            inbox.unlink(missing_ok=True)  # Only challenges created by this test.
        (run / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'report': str(run / 'result.json'), **result}, ensure_ascii=False))


if __name__ == '__main__':
    main()
