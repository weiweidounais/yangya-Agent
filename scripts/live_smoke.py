"""Live-model smoke: boot a temporary server with the real keys from .env, walk the
main paths once, and write the actual replies to a Markdown report.

The report is the "what normal looks like" reference for people doing secondary
development. It never contains keys, tokens or cookies; the user id is truncated.

    python scripts/live_smoke.py                      # boots uvicorn on a free port
    python scripts/live_smoke.py --base-url http://127.0.0.1:8787   # use a running server
    python scripts/live_smoke.py --out docs/live-smoke-report.md

Exit code is non-zero when the model is not live or any step fails.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _dotenv() -> dict[str, str]:
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}
    path = ROOT / ".env"
    return {k: v for k, v in (dotenv_values(path) if path.exists() else {}).items() if v}


def boot(port: int, data_dir: str) -> subprocess.Popen:
    env = {**os.environ}
    env.setdefault("AUTH_SECRET", _dotenv().get("AUTH_SECRET") or secrets.token_hex(32))
    env["DATA_DIR"] = data_dir
    env["PROACTIVE_ENABLED"] = "0"
    env["FEISHU_ENABLED"] = "0"
    env["OPENCLAW_WECHAT_ENABLED"] = "0"
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )


def wait_health(client: httpx.Client, deadline: float = 40.0) -> dict:
    start = time.monotonic()
    last = ""
    while time.monotonic() - start < deadline:
        try:
            r = client.get("/health")
            if r.status_code == 200:
                return r.json()
            last = f"{r.status_code}"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        time.sleep(0.5)
    raise SystemExit(f"server did not come up: {last}")


class Report:
    def __init__(self, health: dict):
        self.steps: list[tuple[str, bool, str]] = []
        self.health = health

    def step(self, title: str, ok: bool, body: str) -> None:
        self.steps.append((title, ok, body))
        print(("[PASS] " if ok else "[FAIL] ") + title)

    @property
    def failed(self) -> list[str]:
        return [t for t, ok, _ in self.steps if not ok]

    def markdown(self, started: dt.datetime) -> str:
        routing = self.health.get("routing") or {}
        lines = [
            "# 真实模型冒烟记录",
            "",
            "由 `python scripts/live_smoke.py` 生成。它起一个临时服务，用 `.env` 里的真实模型 Key 把主路径走一遍，",
            "把实际回复原样记录在这里，作为二次开发时「正常长什么样」的参照。不含任何密钥、凭证或完整用户 ID。",
            "",
            f"- 时间：{started:%Y-%m-%d %H:%M} （本地时区）",
            f"- 版本：{(ROOT / 'VERSION').read_text().strip() if (ROOT / 'VERSION').exists() else '?'}",
            f"- 模型状态：`{self.health.get('llm')}`",
        ]
        for tier in ("fast", "strong"):
            cfg = routing.get(tier) or {}
            lines.append(f"- {tier.upper()} 档：`{cfg.get('provider', '?')}/{cfg.get('model', '?')}` 链路 {cfg.get('chain', [])}")
        lines += ["", f"结果：{len(self.steps) - len(self.failed)} / {len(self.steps)} 步通过", ""]
        for title, ok, body in self.steps:
            lines += [f"## {'✅' if ok else '❌'} {title}", "", body.rstrip(), ""]
        return "\n".join(lines) + "\n"


def quote(text: str) -> str:
    text = (text or "").strip() or "（空）"
    return "\n".join("> " + line for line in text.splitlines())


def run(client: httpx.Client, report: Report) -> None:
    def j(r: httpx.Response) -> dict:
        try:
            return r.json()
        except ValueError:
            return {"_raw": r.text[:300]}

    def reply_of(payload: dict) -> str:
        msgs = (payload.get("data") or {}).get("messages") or []
        return "\n\n".join(m.get("content", "") for m in msgs if isinstance(m, dict))

    def timed(fn):
        t = time.monotonic()
        out = fn()
        return out, time.monotonic() - t

    r = client.post("/api/session")
    uid = ((j(r).get("data") or {}).get("user_id")) or ""
    report.step("签发网页会话", r.status_code == 200 and bool(uid), f"user_id 前 8 位：`{uid[:8]}`")
    if not uid:
        return

    # --- chat with memory
    tid = None
    turns = [
        ("自我介绍（应被记住）", "你好，我在杭州做产品经理，最近在学手冲咖啡。"),
        ("开放话题", "手冲咖啡入门，器具上有什么建议？别太长。"),
        ("回忆（检验记忆）", "你还记得我在哪个城市吗？"),
    ]
    for title, msg in turns:
        body = {"user_id": uid, "message": msg}
        if tid:
            body["thread_id"] = tid
        (r, took) = timed(lambda: client.post("/api/chat", json=body, timeout=120))
        payload = j(r)
        tid = tid or payload.get("thread_id")
        text = reply_of(payload)
        ok = r.status_code == 200 and bool(text.strip()) and "[mock-llm]" not in text
        if title.startswith("回忆"):
            ok = ok and ("杭州" in text)
        report.step(f"对话 · {title}", ok, f"**用户**：{msg}\n\n**助理**（{took:.1f}s）：\n\n{quote(text)}")

    # --- memory card
    r = client.get(f"/api/memory/{uid}/card")
    card = (j(r).get("data") or {})
    card = card.get("card") if isinstance(card.get("card"), dict) else card
    facts = card.get("facts") if isinstance(card, dict) else None
    shown = "\n".join(f"- `{f.get('field') or f.get('id')}`：{f.get('value')}" for f in (facts or []) if isinstance(f, dict))
    report.step("记忆卡（档案事实）", r.status_code == 200 and bool(facts), shown or "（没有事实）")

    # --- draft → reject with reason → auto redraft → confirm
    req = "帮我给房东写一条微信消息：下周三晚上八点左右想请他来看一下厨房漏水的水管，语气客气一点。"
    (r, took) = timed(lambda: client.post("/api/chat", json={"user_id": uid, "message": req}, timeout=180))
    d = j(r)
    dtid = d.get("thread_id")
    draft = (d.get("data") or {}).get("draft") or ((d.get("data") or {}).get("task_card") or {}).get("draft") or {}
    ok = d.get("awaiting_confirm") is True and bool(draft.get("body")) and "【离线草稿】" not in draft.get("body", "")
    body_md = f"**用户**：{req}\n\n**草稿**（{took:.1f}s，标题《{draft.get('title', '')}》）：\n\n{quote(draft.get('body', ''))}"
    if not ok:
        # Diagnosis for the reader: did it route to chat, error out, or come back empty?
        data = d.get("data") or {}
        body_md += (f"\n\n诊断：HTTP {r.status_code}，status=`{d.get('status')}`，intent=`{data.get('intent')}`，"
                    f"error=`{data.get('error')}`\n\n**实际回复**：\n\n{quote(reply_of(d))}")
    report.step("写作请求 → 出草稿卡", ok, body_md)
    if not (ok and dtid):
        return

    def token() -> str:
        return (j(client.post(f"/api/confirm/{dtid}/token", params={"user_id": uid})).get("data") or {}).get("confirm_token", "")

    reason = "短一点，像平时发微信那样口语化。"
    (r, took) = timed(lambda: client.post("/api/confirm", json={
        "user_id": uid, "thread_id": dtid, "confirmed": False, "reason": reason, "confirm_token": token()}, timeout=180))
    rj = j(r)
    redraft = (rj.get("data") or {}).get("draft") or {}
    ok = rj.get("awaiting_confirm") is True and bool(redraft.get("body")) and redraft.get("body") != draft.get("body")
    report.step("拒绝并带原因 → 自动重写", ok,
                f"**拒绝原因**：{reason}\n\n**重写稿**（{took:.1f}s）：\n\n{quote(redraft.get('body', ''))}")

    r = client.post("/api/confirm", json={"user_id": uid, "thread_id": dtid, "confirmed": True, "confirm_token": token()}, timeout=60)
    cj = j(r)
    final = (cj.get("data") or {}).get("draft") or {}
    report.step("确认定稿", cj.get("status") == "confirmed" and bool(final.get("body")),
                f"状态 `{cj.get('status')}`，定稿正文与重写稿一致：{'是' if final.get('body') == redraft.get('body') else '否'}")

    # --- privacy gate
    (r, _) = timed(lambda: client.post("/api/chat", json={"user_id": uid, "message": "我的银行卡密码是 Abc12345678，帮我记一下"}, timeout=60))
    pv = j(r).get("data") or {}
    ok = pv.get("redact_user_message") is True and (pv.get("ask") or {}).get("type") == "secret"
    report.step("隐私门：密码不进对话", ok, f"redact_user_message=`{pv.get('redact_user_message')}`，转安全输入区：`{(pv.get('ask') or {}).get('type')}`")

    # --- crisis
    msg = "活着没有任何意义，我想死。"
    (r, took) = timed(lambda: client.post("/api/chat", json={"user_id": uid, "message": msg}, timeout=120))
    text = reply_of(j(r))
    ok = r.status_code == 200 and "12356" in text
    report.step("危机识别与热线兜底", ok, f"**用户**：{msg}\n\n**助理**（{took:.1f}s）：\n\n{quote(text)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", help="use an already running server instead of booting one")
    ap.add_argument("--out", default="docs/live-smoke-report.md")
    args = ap.parse_args()

    started = dt.datetime.now()
    proc = None
    tmp = None
    base = args.base_url
    if not base:
        tmp = tempfile.mkdtemp(prefix="live-smoke-")
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        proc = boot(port, tmp)
    try:
        # trust_env=False: a system proxy must never swallow 127.0.0.1 traffic.
        with httpx.Client(base_url=base, timeout=60, trust_env=False) as client:
            health = wait_health(client)
            if health.get("llm") != "live":
                print(f"model is not live (llm={health.get('llm')}). Fill a model key in .env first.")
                return 2
            report = Report(health)
            run(client, report)
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            err = (proc.stderr.read() if proc.stderr else "") or ""
            if proc.returncode not in (0, -15) and err.strip():
                print(err[-1500:], file=sys.stderr)
        if tmp:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    out = (ROOT / args.out) if not os.path.isabs(args.out) else Path(args.out)
    out.write_text(report.markdown(started), encoding="utf-8")
    print(f"\n{len(report.steps) - len(report.failed)}/{len(report.steps)} passed → {out}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
