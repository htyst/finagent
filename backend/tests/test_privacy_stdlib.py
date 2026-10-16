"""Authentication redaction tests requiring only the Python standard library."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.agent.privacy import scrub_auth, AUTH_TEXT


class PrivacyTests(unittest.TestCase):
    def test_explicit_auth_material_redacted(self):
        for text in ['验证码：123456', '短信码 012345', 'OTP: 543210', '密码=example-secret']:
            with self.subTest(text=text):
                self.assertNotEqual(scrub_auth(text), text)
                self.assertIn('已隐藏认证信息', scrub_auth(text))

    def test_nested_secret_keys_redacted(self):
        original = {'items': [{'code': '123456', 'confirmation_token': 'test-secret', 'amount_minor': 100000}]}
        safe = scrub_auth(original)
        self.assertNotIn('123456', str(safe))
        self.assertNotIn('test-secret', str(safe))
        self.assertEqual(safe['items'][0]['amount_minor'], 100000)
        self.assertEqual(original['items'][0]['code'], '123456')

    def test_bank_intent_and_amount_are_preserved(self):
        text = '从活期账户转给张三1000元，然后查余额'
        self.assertEqual(scrub_auth(text), text)
        self.assertIsNone(AUTH_TEXT.search(text))


if __name__ == '__main__':
    unittest.main()
