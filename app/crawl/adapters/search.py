import os
from datetime import timedelta
from urllib.parse import quote

from ..errors import NotConfigured


def search(name, ctx, params):
    query = str(params.get("query", ""))
    if name == "search.searxng":
        base = os.getenv("SEARXNG_URL", "")
        if not base:
            raise NotConfigured("未配置 SearXNG（需要 SEARXNG_URL）")
        days = params.get("recency_days")
        period = None if days is None else "day" if days <= 1 else "week" if days <= 7 else "month" if days <= 31 else "year"
        rows = ctx.json(base.rstrip("/") + "/search", params={"q": query, "format": "json", "time_range": period}).get("results", [])
    else:
        headers = {"Accept": "application/json"}
        if os.getenv("JINA_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["JINA_API_KEY"]
        if params.get("recency_days"):
            query += " after:" + (ctx.router.date().date() - timedelta(days=params["recency_days"])).isoformat()
        rows = ctx.json("https://s.jina.ai/" + quote(query, safe=""), headers=headers).get("data", [])
    return [{"url": x.get("url"), "title": x.get("title"),
             "snippet": x.get("content") or x.get("description", ""),
             "published_at": x.get("publishedDate")} for x in rows[:20]]
