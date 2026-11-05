import os
from decimal import Decimal, InvalidOperation

from ..errors import NotConfigured, PolicyDenied


def search_x(ctx, params):
    token = os.getenv("X_BEARER_TOKEN")
    if not token:
        raise NotConfigured("未配置 X API（需要 X_BEARER_TOKEN）")
    try:
        cap = int(Decimal(os.getenv("X_MONTHLY_SPEND_CAP_USD", "50")) * 1000000)
    except (InvalidOperation, ValueError, OverflowError):
        raise NotConfigured("Invalid X monthly spend cap") from None
    period = ctx.router.date().strftime("%Y-%m")
    # Reserve the maximum page cost first, atomically, across threads/processes.
    # Retain reservations on transport/parse failure (the provider may have billed).
    reserve = 10 * 5000
    if not ctx.router.store.reserve("x_spend_microusd", period, reserve, cap):
        raise PolicyDenied("X monthly spend budget exhausted")
    rows = ctx.json("https://api.x.com/2/tweets/search/recent", params={
        "query": params.get("query", ""), "max_results": 10, "tweet.fields": "created_at"},
        headers={"Authorization": "Bearer " + token}).get("data", [])
    ctx.router.store.refund("x_spend_microusd", period, max(0, reserve - len(rows) * 5000))
    return [{"url": "https://x.com/i/web/status/" + str(x["id"]), "title": x.get("text", ""),
             "snippet": x.get("text", ""), "published_at": x.get("created_at")}
            for x in rows if str(x.get("id", "")).isdigit()]
