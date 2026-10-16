import os
import subprocess
import sys
from app.config import database_path

path = database_path()
port = int(os.getenv('PORT', '8000'))
if not 1 <= port <= 65535:
    raise SystemExit('PORT must be between 1 and 65535')
entry = os.getenv('FINAGENT_APP_ENTRY', 'app.main:app')
if entry not in {'app.main:app', 'app.cloud:app_factory'}:
    raise SystemExit('Unsupported application entry')
action = 'migrate' if path.exists() else 'reset'
subprocess.run([sys.executable, '-m', 'app.data.cli', action], check=True)
command = [sys.executable, '-m', 'uvicorn', entry, '--host', '0.0.0.0', '--port', str(port)]
if entry.endswith(':app_factory'):
    command.append('--factory')
os.execv(sys.executable, command)
