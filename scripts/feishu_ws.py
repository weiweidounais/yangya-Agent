#!/usr/bin/env python3
"""Feishu long-connection worker.

Each connect runs in a fresh Python process so lark_oapi's module-level
asyncio loop is never reused after a failed/closed session (which otherwise
surfaces as transient 1000040345 / 'Event loop is closed').

Usage:
  python scripts/feishu_ws.py
  # or: uvicorn starts this as a subprocess when FEISHU_WS_ENABLED is on
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WORKER = """
import logging, sys
from pathlib import Path
ROOT = Path({root!r})
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
from app.channels import feishu_client, feishu_ws
if not feishu_client.is_configured():
    raise SystemExit("FEISHU_APP_ID / FEISHU_APP_SECRET required")
print("Feishu WS worker connecting; app_id_prefix=", feishu_client.app_id()[:16], flush=True)
feishu_ws.run_forever()
""".format(root=str(ROOT))


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    from app.channels import feishu_ws

    if not feishu_ws.ws_enabled():
        print("Feishu disabled: set FEISHU_ENABLED=1 and FEISHU_APP_ID / FEISHU_APP_SECRET", flush=True)
        return 2
    delay = float(os.getenv("FEISHU_WS_RECONNECT_DELAY") or "15")
    print("Feishu WS supervisor starting (Ctrl+C to stop)", flush=True)
    while True:
        try:
            rc = subprocess.call([sys.executable, "-c", WORKER], cwd=str(ROOT))
            print(f"worker exited rc={rc}; reconnect in {delay:.1f}s", flush=True)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        time.sleep(delay)
        delay = min(delay * 1.3, 60.0)


if __name__ == "__main__":
    raise SystemExit(main())
