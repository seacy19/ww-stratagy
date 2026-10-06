"""Load the WW project's Tushare token without storing it in project files."""
from __future__ import annotations

import os
import subprocess
import sys
from getpass import getuser
from types import MethodType

import pandas as pd


KEYCHAIN_SERVICE = "ww-strategy-tushare"
TUSHARE_HTTPS_URL = "https://api.tushare.pro"


def load_tushare_token() -> str:
    """Prefer an explicitly provided process token, then macOS Keychain."""
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token
    if sys.platform != "darwin":
        raise RuntimeError(
            "Tushare token is not configured; set TUSHARE_TOKEN in the process environment"
        )
    result = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-a",
            getuser(),
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise RuntimeError(
            "Tushare token is not configured in macOS Keychain. "
            "Run `python3 configure_tushare.py --store` in the project directory."
        )
    return token


def install_tushare_https_transport(pro, *, post=None):
    """Bind an SDK client to Tushare's HTTPS REST root for this instance only.

    Recent SDK releases append the API name to ``__http_url``. The documented
    REST endpoint accepts the SDK JSON request at the root path, so replace
    this instance's query method rather than monkey-patching global requests.
    """
    if post is None:
        import requests

        post = requests.post
    token = getattr(pro, "_DataApi__token", None)
    timeout = getattr(pro, "_DataApi__timeout", 10)
    if not token:
        raise RuntimeError("Tushare SDK client has no configured token")
    setattr(pro, "_DataApi__http_url", TUSHARE_HTTPS_URL)

    def query(_self, api_name, fields="", **params):
        payload = {
            "api_name": api_name,
            "token": token,
            "params": params,
            "fields": fields,
        }
        try:
            response = post(TUSHARE_HTTPS_URL, json=payload, timeout=timeout)
        except Exception as exc:
            raise RuntimeError("Tushare HTTPS transport failed") from exc
        if response.status_code != 200:
            raise RuntimeError(f"Tushare HTTPS transport returned HTTP {response.status_code}")
        try:
            result = response.json()
        except Exception as exc:
            raise RuntimeError("Tushare HTTPS response was not valid JSON") from exc
        if result.get("code") != 0:
            raise RuntimeError(f"Tushare API {api_name} returned code {result.get('code')}")
        data = result.get("data") or {}
        return pd.DataFrame(data.get("items", ()), columns=data.get("fields", ()))

    pro.query = MethodType(query, pro)
    return pro


def create_tushare_client(token: str | None = None):
    """Create an SDK client whose credential is sent only to official HTTPS."""
    import tushare as ts

    resolved_token = token or load_tushare_token()
    return install_tushare_https_transport(ts.pro_api(resolved_token))
