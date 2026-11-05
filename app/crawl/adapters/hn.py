from datetime import datetime, timezone

from ..errors import CrawlError

FIREBASE = "https://hacker-news.firebaseio.com/v0"
ALGOLIA = "https://hn.algolia.com/api/v1"


def item_url(item_id):
    return "https://news.ycombinator.com/item?id=" + str(item_id)


def fetch_hn(name, ctx, params):
    query = params.get("query", "")
    if name == "hn.algolia":
        hits = ctx.json(ALGOLIA + "/search", params={"query": query, "tags": "story", "hitsPerPage": 20}).get("hits", [])
        return [{"url": x.get("url") or (item_url(x["objectID"]) if x.get("objectID") else None),
                 "title": x.get("title"), "snippet": x.get("story_text"), "published_at": x.get("created_at")}
                for x in hits]
    else:
        kind = params.get("kind", "top")
        if kind not in {"top", "best", "new"}:
            raise CrawlError("HN kind must be top, best, new, or hiring")
        ids = ctx.json(f"{FIREBASE}/{kind}stories.json")[:20]
    items = []
    for item_id in ids:
        if not str(item_id).isdigit():
            continue
        row = ctx.json(f"{FIREBASE}/item/{item_id}.json")
        if not row or row.get("deleted") or row.get("dead"):
            continue
        items.append({"url": row.get("url") or item_url(item_id),
                      "title": row.get("title") or "HN Who is hiring comment", "snippet": row.get("text", ""),
                      "published_at": datetime.fromtimestamp(row["time"], timezone.utc).isoformat() if row.get("time") else None})
    return items
