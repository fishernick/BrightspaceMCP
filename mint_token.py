#!/usr/bin/env python3
"""
mint_token.py - mint an inbound MCP auth token and put it in the environment.

The server's ``RequireToken`` middleware (see ``src/brightspacemcp/server.py``)
rejects any request whose ``Authorization: Bearer <token>`` header does not match
``MCP_INBOUND_TOKEN`` with a constant-time compare. That value lives in ``.env``,
which ``brightspacemcp.auth`` loads via ``python-dotenv`` at import (so it is in
``os.environ`` by the time ``main()`` reads it).

This tool generates a fresh cryptographically-random token, upserts it into
``.env`` (every other line preserved), also drops it into this process's
``os.environ``, and prints the string you paste into your MCP client:

    Copy this whole token for input: "Bearer <token>"

Usage:
    python mint_token.py                generate, write .env, print the Bearer line
    python mint_token.py --raw          print only the bare token (for scripts)
    python mint_token.py --show         print the token already in .env; mint nothing
    python mint_token.py --force        replace an existing token instead of reusing
    python mint_token.py --env PATH     target a different .env file
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

VAR = "MCP_INBOUND_TOKEN"
DEFAULT_ENV = Path(__file__).resolve().parent / ".env"


def read_env(path: Path) -> dict[str, str]:
    """Parse a dotenv file into a plain dict (no interpolation, last wins)."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        values[key.strip()] = val.strip()
    return values


def upsert_env(path: Path, key: str, value: str) -> None:
    """Set ``key=value`` in the dotenv file, leaving every other line untouched."""
    new_line = f"{key}={value}"
    lines = path.read_text().splitlines() if path.exists() else []
    for i, existing in enumerate(lines):
        head = existing.split("=", 1)[0].strip()
        if head == key:
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    path.write_text("\n".join(lines) + "\n")


def mint() -> str:
    # 32 bytes -> 43-char url-safe string; plenty of entropy for a bearer secret.
    return secrets.token_urlsafe(32)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mint an inbound MCP auth token and store it in .env",
    )
    parser.add_argument("--raw", action="store_true",
                        help="print only the bare token")
    parser.add_argument("--show", action="store_true",
                        help="print the token already in .env; do not mint a new one")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing token instead of reusing it")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV,
                        help=f"path to the .env file (default: {DEFAULT_ENV})")
    args = parser.parse_args(argv)

    env_path: Path = args.env
    existing = read_env(env_path).get(VAR, "")

    if args.show:
        if not existing:
            print(f"no {VAR} in {env_path}", file=sys.stderr)
            return 1
        token = existing
    elif existing and not args.force:
        token = existing
        if not args.raw:
            print(f"{env_path} already has {VAR}; reusing it "
                  f"(pass --force to replace)", file=sys.stderr)
    else:
        token = mint()
        upsert_env(env_path, VAR, token)
        if not args.raw:
            print(f"wrote {VAR} to {env_path}", file=sys.stderr)

    # Put it in the environment for anything this process goes on to spawn.
    os.environ[VAR] = token

    if args.raw:
        print(token)
    else:
        print()
        print(f'Copy this whole token for input: "Bearer {token}"')
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
