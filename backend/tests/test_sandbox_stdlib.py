"""These tests run even before third-party backend dependencies are installed."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.agent.sandbox import validate_envelope
from app.domain.errors import DomainError

WORKER = Path(__file__).resolve().parents[1] / 'app' / 'agent' / 'sandbox_worker.py'


class SandboxTests(unittest.TestCase):
    def test_known_read(self):
        validate_envelope('list_accounts', {})

    def test_known_prepare_does_not_execute_payment(self):
        validate_envelope('prepare_transfer', {'amount_minor': 200000})

    def test_shell_tool_rejected(self):
        with self.assertRaises(DomainError):
            validate_envelope('shell', {'command': 'whoami'})

    def test_confirmation_tool_not_available(self):
        with self.assertRaises(DomainError):
            validate_envelope('confirm_proposal', {})

    def test_unknown_mfa_tool_not_available(self):
        with self.assertRaises(DomainError):
            validate_envelope('verify_mfa', {'code': '123456'})

    def test_payload_limit(self):
        with self.assertRaises(DomainError):
            validate_envelope('list_accounts', {'text': 'x' * 65537})

    def test_invalid_json(self):
        result = subprocess.run([sys.executable, '-I', '-S', str(WORKER)], input=b'not json', capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_timeout_fails_closed(self):
        with patch('app.agent.sandbox.subprocess.run', side_effect=subprocess.TimeoutExpired('sandbox', 2)):
            with self.assertRaises(DomainError) as error:
                validate_envelope('list_accounts', {})
            self.assertEqual(error.exception.code, 'SANDBOX_UNAVAILABLE')

    def test_file_access_denied(self):
        self.probe("open('private.txt', 'w')")

    def test_network_access_denied(self):
        self.probe("socket.socket()")

    def test_generated_code_denied(self):
        self.probe("eval('1 + 1')")

    def probe(self, expression):
        # Import trusted code before enabling the same audit policy as the worker.
        script = ("import importlib.util,sys,socket; "
                  "spec=importlib.util.spec_from_file_location('worker'," + repr(str(WORKER)) + "); "
                  "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); sys.addaudithook(m.deny)\n"
                  "try:\n " + expression + "\nexcept PermissionError:\n print('DENIED')\n")
        result = subprocess.run([sys.executable, '-I', '-S', '-c', script], capture_output=True, timeout=2)
        self.assertEqual(result.stdout.strip(), b'DENIED', result.stderr.decode(errors='replace'))


if __name__ == '__main__':
    unittest.main()
