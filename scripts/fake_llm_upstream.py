"""Fake OpenAI-compatible vendor for local load tests (no real keys / cost).

Emulates a Moonshot-style account: per-account concurrency cap (429 beyond it),
configurable latency, request counters at ``GET /stats``.

    python scripts/fake_llm_upstream.py --port 9100 --max-concurrency 4 --latency 1.5

Point the app at it with ``LLM_FAST_BASE_URL=http://127.0.0.1:9100/v1`` (and STRONG).
"""

from __future__ import annotations

import argparse
import asyncio
import random
from collections import Counter

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
ARGS = argparse.Namespace(max_concurrency=4, latency=1.5, jitter=0.5, classifier_latency=0.6)
STATE = {"in_flight": 0, "peak": 0}
COUNTS: Counter = Counter()


@app.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    system = body["messages"][0]["content"]
    kind = "classifier" if "分类器" in system else ("summary" if "记忆压缩器" in system else "chat")
    COUNTS[f"req:{kind}"] += 1
    if STATE["in_flight"] >= ARGS.max_concurrency:
        COUNTS["429"] += 1
        return JSONResponse(
            {"error": {"message": "rate limit: max organization concurrency", "type": "rate_limit_reached_error"}},
            status_code=429,
            headers={"retry-after": "1"},
        )
    STATE["in_flight"] += 1
    STATE["peak"] = max(STATE["peak"], STATE["in_flight"])
    try:
        base = ARGS.classifier_latency if kind == "classifier" else ARGS.latency
        await asyncio.sleep(max(0.05, base + random.uniform(-ARGS.jitter, ARGS.jitter)))
    finally:
        STATE["in_flight"] -= 1
    COUNTS["200"] += 1
    text = "NO" if kind == "classifier" else ("用户在聊日常安排。" if kind == "summary" else "我在呢，慢慢说。")
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 400, "completion_tokens": 40},
    }


@app.get("/stats")
def stats():
    return {"counts": dict(COUNTS), "peak_in_flight": STATE["peak"], "max_concurrency": ARGS.max_concurrency}


@app.post("/reset")
def reset():
    COUNTS.clear()
    STATE["peak"] = 0
    return {"ok": True}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=9100)
    p.add_argument("--max-concurrency", type=int, default=4)
    p.add_argument("--latency", type=float, default=1.5)
    p.add_argument("--jitter", type=float, default=0.5)
    p.add_argument("--classifier-latency", type=float, default=0.6)
    a = p.parse_args()
    ARGS.max_concurrency, ARGS.latency, ARGS.jitter, ARGS.classifier_latency = (
        a.max_concurrency, a.latency, a.jitter, a.classifier_latency,
    )
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
