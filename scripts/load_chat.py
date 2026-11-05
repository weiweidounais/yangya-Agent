"""Concurrent /api/chat load test (QA scenario: 8 concurrency, 34 requests).

    python scripts/load_chat.py --base http://127.0.0.1:8787 -c 8 -n 34

Prints status histogram, latency percentiles and (if reachable) the
``/api/llm/usage`` totals and fake-upstream ``/stats``.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

MESSAGES = [
    "今天方案又被打回了，有点难受",
    "最近有什么好看的电影推荐吗",
    "改了二十多版都没通过，好累",
    "帮我看看周末怎么安排比较好",
    "周末想出去走走，北京有什么地方推荐",
    "你能做什么",
    "感觉自己什么都做不好，算了",
    "周报怎么写比较清楚",
]


FALLBACK_MARK = "刚才我这边有点卡"
CRISIS_MARK = "010-82951332"


def _reply_kind(r: httpx.Response) -> str:
    try:
        msgs = r.json()["data"]["messages"] or []
        text = "".join(str(m.get("content") or "") for m in msgs)
    except Exception:
        return "unparsed"
    if FALLBACK_MARK in text:
        return "fallback_reply"
    if CRISIS_MARK in text:
        return "crisis_footer"
    if text.startswith("[mock-llm]"):
        return "mock"
    return "normal"


def one(client: httpx.Client, base: str, i: int) -> tuple[int, float, str, str]:
    body = {"user_id": f"load-{i % 8}", "thread_id": uuid.uuid4().hex, "message": MESSAGES[i % len(MESSAGES)]}
    t0 = time.monotonic()
    try:
        r = client.post(base + "/api/chat", json=body)
        return r.status_code, time.monotonic() - t0, "", _reply_kind(r)
    except httpx.HTTPError as e:
        return 0, time.monotonic() - t0, type(e).__name__, ""


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8787")
    p.add_argument("--upstream", default="http://127.0.0.1:9100")
    p.add_argument("-c", "--concurrency", type=int, default=8)
    p.add_argument("-n", "--requests", type=int, default=34)
    p.add_argument("--timeout", type=float, default=120.0)
    a = p.parse_args()

    with httpx.Client(timeout=a.timeout) as client:
        try:
            client.post(a.upstream + "/reset")
        except httpx.HTTPError:
            pass
        t0 = time.monotonic()
        with ThreadPoolExecutor(a.concurrency) as ex:
            results = list(ex.map(lambda i: one(client, a.base, i), range(a.requests)))
        wall = time.monotonic() - t0
        lat = sorted(r[1] for r in results)
        out = {
            "requests": a.requests,
            "concurrency": a.concurrency,
            "wall_s": round(wall, 1),
            "status": dict(Counter(r[0] for r in results)),
            "client_errors": dict(Counter(r[2] for r in results if r[2])),
            "replies": dict(Counter(r[3] for r in results if r[3])),
            "latency_s": {
                "p50": round(statistics.median(lat), 2),
                "p95": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 2),
                "max": round(lat[-1], 2),
            },
        }
        try:
            out["upstream"] = client.get(a.upstream + "/stats").json()
        except httpx.HTTPError:
            pass
        try:
            data = client.get(a.base + "/api/llm/usage", params={"limit": 0}).json()["data"]
            out["gateway_totals"] = data["totals"]
            out["gateway_limits"] = data["limits"]
        except Exception:
            pass
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
