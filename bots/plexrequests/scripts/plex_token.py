#!/usr/bin/env python3
"""Fetch a Plex account token via the official plex.tv/link flow.

    python bots/plexrequests/scripts/plex_token.py              # prints the token
    python bots/plexrequests/scripts/plex_token.py --env .env   # also writes PLEX_TOKEN= into that file

It prints a 4-character code. Open https://plex.tv/link signed in as the Plex server owner, enter the code,
and the token is printed (and saved when --env is given). Paste it into the `plexrequests` service settings
(web UI or config/periscope.yaml) — it is the PLEX_TOKEN the service and its Test button use.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PRODUCT = "periscope"


def write_token(env_file: Path, token: str) -> None:
    text = env_file.read_text() if env_file.exists() else ""
    if re.search(r"^PLEX_TOKEN=.*$", text, flags=re.M):
        text = re.sub(r"^PLEX_TOKEN=.*$", f"PLEX_TOKEN={token}", text, flags=re.M)
    else:
        text += ("" if text.endswith("\n") or not text else "\n") + f"PLEX_TOKEN={token}\n"
    env_file.write_text(text)


def main() -> int:
    ap = argparse.ArgumentParser(description="Plex token via plex.tv/link")
    ap.add_argument("--env", type=Path, help="write PLEX_TOKEN into this .env file")
    ap.add_argument("--timeout", type=int, default=300, help="seconds to wait for the link (default 300)")
    args = ap.parse_args()
    try:
        from plexapi.myplex import MyPlexPinLogin
    except ImportError:
        print("plexapi is not installed: pip install plexapi", file=sys.stderr)
        return 2

    pinlogin = MyPlexPinLogin(headers={"X-Plex-Product": PRODUCT}, oauth=False)
    print("\n  1. Open  https://plex.tv/link  (sign in as the Plex server owner)")
    print(f"  2. Enter this code:  {pinlogin.pin}\n")
    print(f"Waiting for you to link (up to {args.timeout // 60} minutes)...")
    pinlogin.run(timeout=args.timeout)
    pinlogin.waitForLogin()
    if not pinlogin.token:
        print("Timed out or link failed. Run this script again.")
        return 1
    if args.env:
        write_token(args.env, pinlogin.token)
        print(f"Token received and saved to {args.env} (PLEX_TOKEN).")
    else:
        print(f"PLEX_TOKEN={pinlogin.token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
