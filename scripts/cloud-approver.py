"""Print/create a separate simulated approver credential in the server shell."""
from pathlib import Path
import secrets

path = Path(__file__).resolve().parents[1] / 'var/approver.key'
path.parent.mkdir(parents=True, exist_ok=True)
if not path.exists():
    path.write_text(secrets.token_urlsafe(32), encoding='utf-8')
    path.chmod(0o600)
print('LOCAL SIMULATED APPROVER - not bank authentication')
print(path.read_text(encoding='utf-8').strip())
