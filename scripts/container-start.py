import os
import subprocess
import sys
from app.config import database_path

path = database_path()
action = 'migrate' if path.exists() else 'reset'
subprocess.run([sys.executable, '-m', 'app.data.cli', action], check=True)
os.execv(sys.executable, [sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8000'])
