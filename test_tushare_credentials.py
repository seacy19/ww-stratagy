from __future__ import annotations

import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from tushare_credentials import (
    KEYCHAIN_SERVICE,
    TUSHARE_HTTPS_URL,
    install_tushare_https_transport,
    load_tushare_token,
)


class TushareCredentialTests(unittest.TestCase):
    def test_environment_token_takes_precedence(self):
        with patch.dict(os.environ, {"TUSHARE_TOKEN": "test-token"}), patch(
            "tushare_credentials.subprocess.run"
        ) as run:
            self.assertEqual(load_tushare_token(), "test-token")
            run.assert_not_called()

    @patch("tushare_credentials.sys.platform", "darwin")
    @patch("tushare_credentials.getuser", return_value="tester")
    @patch("tushare_credentials.subprocess.run")
    def test_reads_from_keychain_without_printing_or_returning_errors(
        self, run, _user
    ):
        run.return_value.stdout = "keychain-token\n"
        run.return_value.returncode = 0
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_tushare_token(), "keychain-token")
        args = run.call_args.args[0]
        self.assertIn(KEYCHAIN_SERVICE, args)
        self.assertEqual(run.call_args.kwargs["capture_output"], True)

    @patch("tushare_credentials.sys.platform", "darwin")
    @patch("tushare_credentials.subprocess.run")
    def test_missing_keychain_entry_has_safe_guidance(self, run):
        run.return_value.stdout = ""
        run.return_value.returncode = 1
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "configure_tushare.py"):
                load_tushare_token()

    def test_sdk_query_uses_official_https_root_and_returns_frame(self):
        class FakePro:
            _DataApi__token = "unit-test-token"
            _DataApi__timeout = 10
            _DataApi__http_url = "http://api.tushare.pro"

            def query(self, api_name, fields="", **params):
                raise AssertionError("SDK's default transport must be replaced")

        calls = []

        def fake_post(url, *, json, timeout):
            calls.append((url, json, timeout))
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "code": 0,
                    "data": {
                        "fields": ["cal_date", "is_open"],
                        "items": [["20261007", "0"]],
                    },
                },
            )

        pro = install_tushare_https_transport(FakePro(), post=fake_post)
        result = pro.query("trade_cal", exchange="SSE", start_date="20261007")
        self.assertEqual(calls[0][0], TUSHARE_HTTPS_URL)
        self.assertEqual(calls[0][1]["api_name"], "trade_cal")
        self.assertEqual(calls[0][1]["token"], "unit-test-token")
        self.assertEqual(result.iloc[0]["cal_date"], "20261007")

    def test_api_error_does_not_propagate_provider_message(self):
        class FakePro:
            _DataApi__token = "unit-test-token"
            _DataApi__timeout = 10

        response = SimpleNamespace(
            status_code=200,
            json=lambda: {"code": 40101, "msg": "secret-bearing provider message"},
        )
        pro = install_tushare_https_transport(FakePro(), post=lambda *a, **k: response)
        with self.assertRaisesRegex(RuntimeError, "code 40101") as caught:
            pro.query("trade_cal")
        self.assertNotIn("secret-bearing", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
