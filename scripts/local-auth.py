"""OS-local simulated inbox/operator credential. Never import into a model tool."""
import argparse
import json
from pathlib import Path
import re
import secrets

parser = argparse.ArgumentParser()
parser.add_argument('action', choices=['sms', 'operator'])
parser.add_argument('--challenge', default='')
args = parser.parse_args()
directory = Path(__file__).resolve().parents[1] / 'var'
if args.action == 'operator':
    directory.mkdir(parents=True, exist_ok=True)
    key = directory / 'operator.key'
    if not key.exists():
        key.write_text(secrets.token_urlsafe(32))
        key.chmod(0o600)
    print('LOCAL SIMULATED OPERATOR - cannot authorize banking operations')
    print(key.read_text().strip())
else:
    outbox = directory / 'auth-simulator'
    if args.challenge and not re.fullmatch('[0-9a-f]{32}', args.challenge):
        raise SystemExit('Invalid challenge identifier')
    path = outbox / (args.challenge + '.json') if args.challenge else max(outbox.glob('*.json'), key=lambda p: p.stat().st_mtime, default=None)
    if path is None:
        raise SystemExit('Request a simulated SMS from the page first')
    record = json.loads(path.read_text(encoding='utf-8'))
    print('LOCAL SMS SIMULATOR - not real SMS or bank identity verification')
    print('Challenge:', record['challenge_id'], 'Expires:', record['expires_at'])
    print('Code:', record['code'])
