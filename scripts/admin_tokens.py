#!/usr/bin/env python3
"""Manage per-admin tokens for the harness admin API (stored as SHA-256 only).

  python scripts/admin_tokens.py create alice --scopes "traces:read|audit:read"
  python scripts/admin_tokens.py list
  python scripts/admin_tokens.py revoke --id 3        # or --name alice

Uses the same ``DATA_DIR`` database as the app. The plaintext token is printed
once on create; store it in a password manager.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.harness import admin  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("name")
    c.add_argument("--scopes", default="*", help=f"'|'-separated; any of {', '.join(admin.ALL_SCOPES)} or '*'")
    sub.add_parser("list")
    r = sub.add_parser("revoke")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", type=int)
    g.add_argument("--name")
    args = ap.parse_args(argv)

    if args.cmd == "create":
        try:
            token = admin.create_token(args.name, args.scopes)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"name": args.name, "scopes": args.scopes, "token": token}, ensure_ascii=False))
        return 0
    if args.cmd == "list":
        print(json.dumps(admin.list_tokens(), ensure_ascii=False, indent=2))
        return 0
    n = admin.revoke_token(args.id, name=args.name or "")
    print(json.dumps({"revoked": n}))
    return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main())
