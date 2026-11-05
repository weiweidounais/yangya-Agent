import os
from urllib.parse import urljoin

from . import optional
from ..errors import NotConfigured, PolicyDenied


def fetch_feed(name, ctx, params):
    parser = optional("feedparser")
    url = params.get("feed", "")
    if name == "feed.rsshub":
        base = os.getenv("RSSHUB_BASE_URL", "")
        if not base:
            raise NotConfigured("未配置 RSSHub（需要 RSSHUB_BASE_URL）")
        if url not in ctx.router.config.get("rsshub_routes", []) or not url.startswith("/") or url.startswith("//"):
            raise PolicyDenied("RSSHub route is not in the non-login allowlist")
        url = urljoin(base.rstrip("/") + "/", url.lstrip("/"))
    response = ctx.get(url)
    if response.status_code != 200:
        from ..errors import CrawlError
        raise CrawlError("Feed fetch failed")
    # Pass bytes, never a URL: feedparser must not perform its own HTTP request.
    feed = parser.parse(response.content)
    return [{"url": entry.get("link"), "title": entry.get("title", ""),
             "snippet": entry.get("summary", ""), "published_at": entry.get("published")}
            for entry in feed.entries[:50]]
