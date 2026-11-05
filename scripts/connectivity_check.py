#!/usr/bin/env python3
"""Personal Agent connectivity checklist — PASS/FAIL, never prints secrets.

Checks (against local uvicorn by default):
  1) /health (LLM, Feishu, OpenClaw, feishu_ws)
  2) Auto-bind + list bindings for FEISHU_BIND_OPEN_IDS (needs CHANNEL_ADMIN_TOKEN)
  3) Simulated Feishu inbound (HTTP fallback accepts the event)
  4) Draft → confirm returns a confirmed draft
  5) OpenClaw webhook bridge sample JSON
  6) uvicorn + feishu_ws process health

Usage:
  .venv/bin/python scripts/connectivity_check.py
  BASE_URL=http://127.0.0.1:8787 .venv/bin/python scripts/connectivity_check.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=False)

BASE = (os.getenv("BASE_URL") or "http://127.0.0.1:8787").rstrip("/")
TIMEOUT = float(os.getenv("CONNECTIVITY_TIMEOUT") or "180")

# Feishu open_ids expected to be bound, from FEISHU_BIND_OPEN_IDS ("open_id" or "open_id:name", comma-separated).
def _open_ids() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in (os.getenv("FEISHU_BIND_OPEN_IDS") or "").split(","):
        item = item.strip()
        if not item:
            continue
        oid, _, name = item.partition(":")
        out.append((oid.strip(), name.strip()))
    return out


DEFAULT_OPEN_IDS = _open_ids()
DEFAULT_USER_ID = "connectivity-check-user"

DRAFT_SAMPLE = "帮我写一封请假邮件，周五请一天假"

PROFILE_SAMPLE = {
    "name": "示例用户",
    "city": "上海",
    "occupation": "产品经理",
    "interests": ["跑步"],
}


class Checklist:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))

    def ok(self) -> bool:
        return all(r[1] for r in self.rows)


def _redact(s: str) -> str:
    """Never echo JWT / secrets in output."""
    if not s:
        return s
    out = s
    for needle in ("Bearer ", "eyJ"):
        if needle in out:
            # blunt redact of long tokens
            import re

            out = re.sub(r"eyJ[A-Za-z0-9_\-\.]{20,}", "[jwt]", out)
            out = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", out)
    return out[:400]


def _pgrep_patterns() -> dict[str, bool]:
    """Detect uvicorn / feishu_ws without printing cmdlines that may hold secrets."""
    found = {"uvicorn": False, "feishu_ws_supervisor": False, "feishu_ws_worker": False}
    try:
        out = subprocess.check_output(["ps", "ax", "-o", "pid=,command="], text=True)
    except Exception:
        return found
    for line in out.splitlines():
        low = line.lower()
        if "uvicorn" in low and "app.main:app" in low:
            found["uvicorn"] = True
        if "scripts/feishu_ws.py" in line:
            found["feishu_ws_supervisor"] = True
        if "feishu_ws.run_forever" in line or "Feishu WS worker" in line:
            found["feishu_ws_worker"] = True
        # worker started via python -c ... feishu_ws.run_forever
        if "feishu_ws" in low and "run_forever" in low:
            found["feishu_ws_worker"] = True
    return found


def main() -> int:
    cl = Checklist()
    uid = (os.getenv("FEISHU_BIND_USER_ID") or DEFAULT_USER_ID).strip()
    admin = os.getenv("CHANNEL_ADMIN_TOKEN") or ""
    headers: dict[str, str] = {"X-Admin-Token": admin} if admin else {}
    # User-scoped checks (profile / draft / confirm) need a server-signed session.
    try:
        from app.auth import auth_configured, mint_user_token

        if auth_configured():
            headers["X-User-Token"] = mint_user_token(uid)
    except Exception:
        pass
    client = httpx.Client(base_url=BASE, timeout=TIMEOUT, headers=headers or None)

    # --- 0) processes ---
    procs = _pgrep_patterns()
    cl.add("uvicorn process", procs["uvicorn"], "app.main:app listening" if procs["uvicorn"] else "not found")
    ws_alive = procs["feishu_ws_supervisor"] or procs["feishu_ws_worker"]
    cl.add(
        "feishu_ws process",
        ws_alive,
        "supervisor/worker alive" if ws_alive else "no feishu_ws process (start via uvicorn or scripts/feishu_ws.py)",
    )

    # --- 1) health ---
    try:
        h = client.get("/health")
        health = h.json() if h.content else {}
        cl.add("GET /health", h.status_code == 200 and bool(health.get("ok")), f"status={h.status_code}")
    except Exception as exc:
        cl.add("GET /health", False, type(exc).__name__)
        print("\nAbort: server not reachable at", BASE)
        return 1

    llm_ok = health.get("llm") == "live"
    cl.add("LLM live", llm_ok, str(health.get("llm")))

    fei = health.get("feishu") or {}
    cl.add("Feishu app configured", bool(fei.get("configured")), f"app_id_set={bool(fei.get('app_id_set'))}")

    fws = health.get("feishu_ws") or {}
    # Process may be external; health.running is best-effort from uvicorn's child
    fws_ok = bool(fws.get("running") or fws.get("connected") or ws_alive)
    cl.add(
        "feishu_ws healthy",
        fws_ok,
        f"health.running={fws.get('running')} connected={fws.get('connected')} proc={ws_alive}",
    )

    # --- 2) auto-bind ---
    try:
        r = client.post("/api/channels/auto-bind")
        data = r.json() if r.content else {}
        count = ((data.get("data") or {}).get("count"))
        cl.add("POST /api/channels/auto-bind", r.status_code == 200 and int(count or 0) >= 1, f"count={count}")
    except Exception as exc:
        cl.add("POST /api/channels/auto-bind", False, type(exc).__name__)

    try:
        r = client.get("/api/channels/bindings", params={"user_id": uid})
        bindings = ((r.json() or {}).get("data") or {}).get("bindings") or []
        by_ext = {b.get("external_user_id"): b for b in bindings if b.get("channel") == "feishu"}
        missing = [oid for oid, _ in DEFAULT_OPEN_IDS if oid not in by_ext]
        cl.add(
            "Feishu open_ids bound",
            not missing and len(by_ext) >= 1,
            f"bound={len(by_ext)} missing={len(missing)}",
        )
    except Exception as exc:
        cl.add("Feishu open_ids bound", False, type(exc).__name__)
        bindings = []

    # Ensure profile for live draft path
    try:
        pr = client.post(
            "/api/profile",
            json={"user_id": uid, "profile": PROFILE_SAMPLE},
        )
        cl.add("Upsert profile", pr.status_code == 200, f"status={pr.status_code}")
    except Exception as exc:
        cl.add("Upsert profile", False, type(exc).__name__)

    # --- 3) simulate Feishu inbound (HTTP fallback; async, reply goes out via Feishu API) ---
    open_id = DEFAULT_OPEN_IDS[0][0] if DEFAULT_OPEN_IDS else "ou_connectivity_smoke"
    feishu_payload = {
        "schema": "2.0",
        "header": {
            "event_type": "im.message.receive_v1",
            "token": os.getenv("FEISHU_VERIFICATION_TOKEN") or "",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": open_id},
                "sender_type": "user",
            },
            "message": {
                "message_id": f"om_connectivity_{int(time.time())}",
                "chat_id": "oc_connectivity_dm",
                "chat_type": "p2p",
                "content": json.dumps({"text": DRAFT_SAMPLE}, ensure_ascii=False),
            },
        },
    }
    try:
        r = client.post("/webhooks/feishu", json=feishu_payload)
        body = r.json() if r.content else {}
        cl.add(
            "Feishu webhook accepts inbound",
            r.status_code == 200 and body.get("status") == "accepted",
            f"http={r.status_code} status={body.get('status') or body.get('detail')}",
        )
    except Exception as exc:
        cl.add("Feishu webhook accepts inbound", False, _redact(str(exc)))

    # --- 4) Draft → confirm ---
    try:
        confirmed = False
        dr = client.post(
            "/api/draft",
            json={"user_id": uid, "request": DRAFT_SAMPLE},
        )
        dbody = dr.json() if dr.content else {}
        tid_d = dbody.get("thread_id") or ""
        if tid_d and dbody.get("awaiting_confirm"):
            tok = client.post(f"/api/confirm/{tid_d}/token", params={"user_id": uid})
            token = ((tok.json() if tok.content else {}).get("data") or {}).get("confirm_token") or ""
            cr2 = client.post(
                "/api/confirm",
                json={
                    "user_id": uid,
                    "thread_id": tid_d,
                    "confirmed": True,
                    "channel": "web",
                    "confirm_token": token,
                },
            )
            c2 = cr2.json() if cr2.content else {}
            confirmed = c2.get("status") == "confirmed" and bool(((c2.get("data") or {}).get("draft") or {}).get("body"))
        cl.add("Draft → confirm", confirmed, f"thread={bool(tid_d)} confirmed={confirmed}")
    except Exception as exc:
        cl.add("Draft → confirm", False, type(exc).__name__)

    # --- 5) OpenClaw webhook smoke ---
    wx_ext = f"wx_connectivity_smoke_{int(time.time())}"
    oc_payload = {
        "channel": "openclaw_wechat",
        "from": wx_ext,
        "text": "你好",
        "msg_id": f"oc_{int(time.time())}",
        "chat_type": "direct",
    }
    oc_headers = {"Authorization": f"Bearer {os.getenv('OPENCLAW_WEBHOOK_TOKEN') or ''}"}
    # Fresh external id each run: unbound → need_bind, then admin bind → normal reply
    try:
        r1 = client.post("/webhooks/openclaw/wechat", json=oc_payload, headers=oc_headers)
        b1 = r1.json() if r1.content else {}
        need = b1.get("status") == "need_bind" and "/bind?t=" in str(b1.get("reply") or "")
        client.post(
            "/api/channels/bind",
            json={
                "channel": "openclaw_wechat",
                "external_user_id": wx_ext,
                "user_id": uid,
                "display_name": "openclaw-smoke",
            },
        )
        oc_payload["msg_id"] += "b"
        r2 = client.post("/webhooks/openclaw/wechat", json=oc_payload, headers=oc_headers)
        b2 = r2.json() if r2.content else {}
        ok = (
            r1.status_code == 200
            and need
            and r2.status_code == 200
            and b2.get("status") not in ("need_bind", "bind_link", None)
            and isinstance(b2.get("reply"), str)
        )
        cl.add(
            "OpenClaw webhook bridge",
            ok,
            f"unbound={b1.get('status') or r1.status_code} bound={b2.get('status') or r2.status_code}",
        )
        client.delete("/api/channels/bind", params={"channel": "openclaw_wechat", "external_user_id": wx_ext})
    except Exception as exc:
        cl.add("OpenClaw webhook bridge", False, type(exc).__name__)

    # --- summary ---
    print()
    passed = sum(1 for _, ok, _ in cl.rows if ok)
    total = len(cl.rows)
    print(f"Result: {passed}/{total} PASS" + ("" if cl.ok() else " (has FAIL)"))
    # External blockers note (no console nag as lead)
    print("Notes: OpenClaw Gateway URL not required for inbound webhook smoke;")
    print("       live Feishu DM still needs console long-connection subscription (external).")
    return 0 if cl.ok() else 1


if __name__ == "__main__":
    raise SystemExit(main())
