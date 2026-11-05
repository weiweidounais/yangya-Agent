"""All adapter payloads, optional libraries and network responses are synthetic."""
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.crawl import adapters, http
from app.crawl.errors import NotConfigured
from app.crawl.policy import get_policy
from tests.test_crawl_router import crawl  # shared isolated network-denying fixture


def test_x_missing_token_no_items_or_network(crawl):
    router, _, calls, _ = crawl
    out = router.route("social.x_api", query="synthetic")
    assert out["status"] == "not_configured" and out["items"] == []
    assert out["errors"] == ["未配置 X API（需要 X_BEARER_TOKEN）"]
    assert not calls


def test_x_api_urls_cost_and_cache(crawl, monkeypatch):
    router, handlers, calls, _ = crawl
    monkeypatch.setenv("X_BEARER_TOKEN", "synthetic-x-token")
    handlers["https://api.x.com/2/tweets/search/recent"] = {"data": [
        {"id": "1234", "text": "Synthetic post", "created_at": "2026-01-01T00:00:00Z"}]}
    out = router.route("social.x_api", query="synthetic")
    assert out["status"] == "ok" and out["items"][0]["url"] == "https://x.com/i/web/status/1234"
    assert calls[-1][2]["headers"]["Authorization"] == "Bearer synthetic-x-token"
    with router.store.connect() as db:
        assert db.execute("SELECT used FROM crawl_quota WHERE scope='x_spend_microusd'").fetchone()[0] == 5000
    assert router.route("social.x_api", query="synthetic")["cached"]
    assert len(calls) == 2
    monkeypatch.setenv("X_MONTHLY_SPEND_CAP_USD", "0.005")
    denied = router.route("social.x_api", query="another query")
    assert denied["status"] == "disabled_by_policy" and not denied["items"]
    assert len(calls) == 2


def test_x_budget_preflight_zero_requests(crawl, monkeypatch):
    router, _, calls, _ = crawl
    monkeypatch.setenv("X_BEARER_TOKEN", "synthetic-x-token")
    monkeypatch.setenv("X_MONTHLY_SPEND_CAP_USD", "0")
    assert router.route("social.x_api", query="synthetic")["status"] == "disabled_by_policy"
    assert not calls


@pytest.mark.parametrize("kind", ["top", "best", "new"])
def test_hn_firebase(crawl, kind):
    router, handlers, _, _ = crawl
    handlers[f"https://hacker-news.firebaseio.com/v0/{kind}stories.json"] = [1, 2, 3]
    handlers["https://hacker-news.firebaseio.com/v0/item/1.json"] = {"id": 1, "title": "Synthetic story", "url": "https://source.example/story", "time": 1700000000}
    handlers["https://hacker-news.firebaseio.com/v0/item/2.json"] = {"id": 2, "title": "Synthetic discussion"}
    handlers["https://hacker-news.firebaseio.com/v0/item/3.json"] = {"id": 3, "deleted": True}
    out = router.route("hn", kind=kind)
    assert out["status"] == "ok" and len(out["items"]) == 2
    assert out["items"][1]["url"] == "https://news.ycombinator.com/item?id=2"


def test_hn_search_and_rejected_kind(crawl):
    router, handlers, calls, _ = crawl
    handlers["https://hn.algolia.com/api/v1/search"] = {"hits": [{"objectID": "1", "title": "Synthetic AI discussion"}]}
    assert router.route("hn", query="AI")["items"][0]["url"].endswith("id=1")
    before = len(calls)
    out = router.route("hn", kind="hiring")
    assert out["status"] == "error" and out["items"] == []
    assert len(calls) == before  # a rejected kind performs no network I/O


def test_hn_cache_no_second_network(crawl):
    router, handlers, calls, _ = crawl
    handlers["https://hacker-news.firebaseio.com/v0/topstories.json"] = [1]
    handlers["https://hacker-news.firebaseio.com/v0/item/1.json"] = {"id": 1, "title": "Synthetic story", "url": "https://source.example/story"}
    out = router.route("hn", kind="top")
    assert out["status"] == "ok" and out["items"]
    before = len(calls)
    assert router.route("hn", kind="top")["cached"]
    assert len(calls) == before


def test_rss_and_reviewed_rsshub_only(crawl, monkeypatch):
    router, handlers, calls, _ = crawl
    parser = Mock(return_value=SimpleNamespace(entries=[{"link": "https://source.example/article", "title": "Synthetic RSS entry"}, {"title": "No URL"}]))
    monkeypatch.setitem(sys.modules, "feedparser", SimpleNamespace(parse=parser))
    handlers["https://feeds.example/rss"] = http.Response(200, b"<rss>synthetic fixture</rss>", {})
    out = router.route("feed", feed="https://feeds.example/rss")
    assert out["status"] == "ok" and len(out["items"]) == 1
    assert parser.call_args.args == (b"<rss>synthetic fixture</rss>",)
    assert router.route("feed.rsshub", feed="/synthetic/public")["status"] == "not_configured"
    monkeypatch.setenv("RSSHUB_BASE_URL", "https://rsshub.example")
    before = len(calls)
    assert router.route("feed.rsshub", feed="/login/cookie")["status"] == "disabled_by_policy"
    assert len(calls) == before
    router.config["rsshub_routes"] = ["/synthetic/public"]
    handlers["https://rsshub.example/synthetic/public"] = http.Response(200, b"<rss/>", {})
    assert router.route("feed", feed="/synthetic/public")["items"]


def test_search_searxng_and_jina_fallback(crawl, monkeypatch):
    router, handlers, calls, _ = crawl
    assert router.route("search.searxng", query="synthetic")["status"] == "not_configured"
    handlers["https://s.jina.ai/synthetic"] = {"data": [{"url": "https://source.example/search", "title": "Synthetic result", "content": "source"}]}
    assert router.route("search", query="synthetic")["items"][0]["adapter"] == "search.jina"
    monkeypatch.setenv("SEARXNG_URL", "https://search.example")
    handlers["https://search.example/search"] = {"results": [{"url": "https://source.example/search2", "content": "Synthetic"}]}
    out = router.route("search", query="synthetic", recency_days=7)
    assert out["items"][0]["adapter"] == "search.searxng"
    assert "time_range=week" in calls[-1][0]


def test_reddit_uses_search_only(crawl, monkeypatch):
    router, handlers, calls, _ = crawl
    monkeypatch.setenv("SEARXNG_URL", "https://search.example")
    handlers["https://search.example/search"] = {"results": [
        {"url": "https://www.reddit.com/r/synthetic/comments/123", "title": "Synthetic discussion"},
        {"url": "https://other.example/not-reddit", "title": "Unrelated"}]}
    out = router.route("social.reddit_search", query="synthetic")
    assert len(out["items"]) == 1
    assert "site%3Areddit.com" in calls[-1][0]
    assert all(http.public_url(c[0]).hostname == "search.example" for c in calls)


def test_trafilatura_and_jina_web_fallback(crawl, monkeypatch):
    router, handlers, _, _ = crawl
    extract = Mock(return_value="Synthetic extracted text")
    monkeypatch.setitem(sys.modules, "trafilatura", SimpleNamespace(extract=extract))
    handlers["https://source.example/article"] = http.Response(200, b"<p>Synthetic text</p>", {})
    out = router.route("web", url="https://source.example/article")
    assert out["items"][0]["markdown"] == "Synthetic extracted text"
    assert extract.call_args.args == ("<p>Synthetic text</p>",)
    monkeypatch.setitem(sys.modules, "trafilatura", None)
    monkeypatch.setitem(sys.modules, "crawl4ai", None)
    handlers["https://source.example/second"] = http.Response(200, b"<p>Synthetic second page</p>", {})
    handlers["https://r.jina.ai/https://source.example/second"] = {"data": {"url": "https://source.example/second", "title": "Synthetic", "content": "Reader text"}}
    out = router.route("web", url="https://source.example/second")
    assert out["items"][0]["adapter"] == "web.jina"


@pytest.mark.parametrize("adapter,module,params", [
    ("web.trafilatura", "trafilatura", {"url": "https://source.example"}),
    ("web.crawl4ai", "crawl4ai", {"url": "https://source.example"}),
    ("feed.rss", "feedparser", {"feed": "https://source.example/feed"}),
])
def test_optional_dependency_absent(crawl, monkeypatch, adapter, module, params):
    router, _, calls, _ = crawl
    monkeypatch.setitem(sys.modules, module, None)
    out = router.route(adapter, **params)
    assert out["status"] == "not_configured" and "未配置" in out["errors"][0]
    assert not out["items"] and not calls


def test_crawl4ai_browser_requests_use_guard(crawl, monkeypatch):
    import asyncio
    router, handlers, calls, _ = crawl
    hooks = {}
    observed = []

    async def immediate_io(fn, *args):
        # The fake browser runs deterministic in-memory I/O. Avoid the sandbox's
        # socket-based cross-thread event-loop wakeup while sockets are denied.
        return fn(*args)

    monkeypatch.setattr(asyncio, "to_thread", immediate_io)

    class Context:
        async def route(self, pattern, handler):
            for url in ["https://source.example/article", "http://127.0.0.1/private"]:
                class Route:
                    request = SimpleNamespace(url=url, method="GET")

                    async def fulfill(self, **kwargs):
                        observed.append(("fulfilled", kwargs))

                    async def abort(self):
                        observed.append(("aborted", {}))
                await handler(Route())

        async def route_web_socket(self, pattern, handler):
            observed.append(("websocket_block", {}))

    class Crawler:
        def __init__(self, **kwargs):
            self.crawler_strategy = SimpleNamespace(set_hook=lambda name, hook: hooks.update({name: hook}))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def arun(self, **kwargs):
            await hooks["on_page_context_created"](None, Context())
            return SimpleNamespace(success=True, markdown=SimpleNamespace(raw_markdown="Synthetic JS result"))

    monkeypatch.setitem(sys.modules, "crawl4ai", SimpleNamespace(AsyncWebCrawler=Crawler, BrowserConfig=lambda **kw: kw,
        CrawlerRunConfig=lambda **kw: kw, CacheMode=SimpleNamespace(BYPASS="bypass")))
    handlers["https://source.example/article"] = http.Response(200, b"<p>Synthetic</p>", {})
    out = router.route("web.crawl4ai", url="https://source.example/article")
    assert out["status"] == "ok" and out["items"][0]["markdown"] == "Synthetic JS result"
    assert [x[0] for x in observed] == ["fulfilled", "aborted", "websocket_block"]
    assert not any("127.0.0.1" in c[0] for c in calls)
