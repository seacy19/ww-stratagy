"""Store or check the WW project's Tushare token in macOS Keychain."""
from __future__ import annotations

import argparse
import subprocess
import sys
from getpass import getuser

from tushare_credentials import KEYCHAIN_SERVICE, load_tushare_token


def store() -> None:
    if sys.platform != "darwin":
        raise SystemExit("Keychain setup is available on macOS only.")
    print("macOS will prompt for your Tushare token without echoing it.")
    print("The token is stored in Login Keychain, not in this project.")
    result = subprocess.run(
        [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-a",
            getuser(),
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
        ],
        check=False,
    )
    if result.returncode:
        raise SystemExit("Keychain did not save the token.")
    # Validate presence, while keeping the value only in process memory.
    load_tushare_token()
    print("Tushare token is stored and readable from Keychain.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", action="store_true", help="securely prompt and store in Keychain")
    parser.add_argument("--check", action="store_true", help="check whether a token is available")
    args = parser.parse_args()
    if args.store:
        store()
    elif args.check:
        try:
            load_tushare_token()
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1)
        print("Tushare token is available; its value was not displayed.")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
