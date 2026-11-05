"""Backfill legacy memory; no writes or embedding requests unless --apply."""
import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview only (default)")
    mode.add_argument("--apply", action="store_true", help="Embed and persist per-user batches")
    parser.add_argument("--user", action="append", help="Restrict to this owner ID (repeatable)")
    parser.add_argument("--limit", type=int, help="Maximum number of users")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("limit must be nonnegative")
    if args.user and any(not uid.strip() for uid in args.user):
        parser.error("user IDs must be nonempty")
    failed = False
    try:
        from app.agent.memory.migration import migrate_users

        for report in migrate_users(args.user, apply=args.apply, limit=args.limit):
            print(json.dumps(report, sort_keys=True))
            failed |= report["failed"]
    except Exception:
        # Enumeration/setup failures must not expose paths, SQL parameters or PII.
        print(json.dumps({"failed": True}), file=sys.stderr)
        return 1
    return int(failed)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
