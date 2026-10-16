"""A bounded isolated process validates envelopes; trusted services own the DB."""
import json
from pathlib import Path
import subprocess
import sys
from app.domain.errors import DomainError


def validate_envelope(name, arguments):
    payload = json.dumps({'tool': name, 'arguments': arguments}, ensure_ascii=False).encode()
    if len(payload) > 65536:
        raise DomainError('SANDBOX_REJECTED', '工具输入超过受限大小')
    try:
        result = subprocess.run([sys.executable, '-I', '-S', str(Path(__file__).with_name('sandbox_worker.py'))],
            input=payload, capture_output=True, timeout=2, shell=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DomainError('SANDBOX_UNAVAILABLE', '受限工具校验超时或无法启动，未执行', 503) from error
    if result.returncode or result.stdout.strip() != b'{"accepted":true}':
        raise DomainError('SANDBOX_REJECTED', '受限工具校验拒绝执行')
