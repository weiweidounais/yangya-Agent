"""Redirect security checks using synthetic responses and forbidden socket I/O."""
import json
from unittest.mock import Mock
from urllib.parse import urlencode, urlsplit

import pytest

from app.crawl import http
from app.crawl.errors import CrawlError
from app.crawl.policy import get_policy
from tests.test_crawl_router import crawl  # shared isolated network-denying fixture


def test_encoded_proxy_username_redacted_from_results_cache_audit_and_trace(crawl, monkeypatch, caplog, uid):
    from app.agent import crawl_tools, tool_registry
    from app.agent.harness import traces
    from app.tools import crawl as tools

    router, handlers, calls, _ = crawl
    encoded, decoded = "account%40tenant", "account@tenant"
    password = "synthetic-proxy-password"
    proxy = f"http://{encoded}:{password}@proxy.example:8080"
    monkeypatch.setenv("CRAWL_PROXY_URL", proxy)
    router.config["adapters"]["hn.algolia"]["proxy"] = True
    destination = f"https://hn.algolia.com/{encoded}/{decoded}"
    handlers["https://hn.algolia.com/api/v1/search"] = http.Response(302, b"", {"location": destination})
    handlers[destination] = {"hits": [{
        "objectID": "1", "url": "https://source.example/1", "title": f"{encoded} | {decoded} | {proxy}",
        "story_text": f"Synthetic result for {decoded}",
    }]}
    monkeypatch.setattr(tools.crawl_router, "route", router.route)
    recorded = Mock()
    monkeypatch.setattr(traces, "record_step", recorded)
    monkeypatch.setattr(tool_registry, "_sample_read", lambda: True)
    args = {"query": "synthetic"}
    outputs = []
    for cached in (False, True):
        out = tool_registry.invoke_tool("get_hn", args, {"user_id": uid})
        assert out["trust"] == "untrusted_source_data"
        assert out["data"]["status"] == "ok"
        assert out["data"]["cached"] is cached
        assert out["data"]["items"][0]["title"] == "[redacted] | [redacted] | [redacted]"
        outputs.append(out)

    monkeypatch.setattr(crawl_tools.llm_gateway, "has_llm", lambda *a: True)
    monkeypatch.setattr(crawl_tools.llm_gateway, "chat_json", lambda *a, **kw: {
        "calls": [{"tool": "get_hn", "args": args}]})
    context = crawl_tools.source_context("Find synthetic discussions", {"user_id": uid})
    assert "https://source.example/1" in context and "[redacted]" in context
    tool_traces = [call.kwargs for call in recorded.call_args_list if call.kwargs.get("tool") == "get_hn"]
    assert len(tool_traces) == 3 and all(row["ok"] for row in tool_traces)
    assert len(calls) == 3  # robots, initial request, redirect; cached calls do no I/O
    assert all(call[2]["proxy"] == proxy for call in calls)
    with router.store.connect() as db:
        audit = db.execute("SELECT * FROM crawl_audit").fetchall()
        cache = db.execute("SELECT payload FROM crawl_cache").fetchall()
    assert len(audit) == 3
    assert audit[-1][3] == "https://hn.algolia.com/[redacted]/[redacted]"
    assert cache
    rendered = json.dumps([outputs, audit, cache, [call.kwargs for call in recorded.call_args_list]]) + context + caplog.text
    for secret in (proxy, encoded, decoded, password):
        assert secret not in rendered


@pytest.mark.parametrize("adapter,start,params,payload,secret_env", [
    ("social.x_api", "https://api.x.com/2/tweets/search/recent", {"query": "synthetic"},
     {"data": [{"id": "1234", "text": "Synthetic post"}]}, "X_BEARER_TOKEN"),
    ("web.jina", "https://r.jina.ai/https://source.example/article",
     {"url": "https://source.example/article"},
     {"data": {"url": "https://source.example/article", "content": "Synthetic reader text"}}, "JINA_API_KEY"),
    ("search.jina", "https://s.jina.ai/synthetic", {"query": "synthetic"},
     {"data": [{"url": "https://source.example/article", "content": "Synthetic result"}]}, "JINA_API_KEY"),
])
@pytest.mark.parametrize("destination_kind", ["downgrade", "other_host", "other_port", "same_origin"])
def test_authorization_redirect_origin(crawl, monkeypatch, adapter, start, params, payload,
                                      secret_env, destination_kind):
    router, handlers, calls, _ = crawl
    monkeypatch.setenv(secret_env, "synthetic-redirect-token")
    host = urlsplit(start).netloc
    destination = {
        "downgrade": f"http://{host}/redirected",
        "other_host": "https://other.example/redirected",
        "other_port": f"https://{host}:8443/redirected",
        "same_origin": f"https://{host}/redirected",
    }[destination_kind]
    handlers["https://source.example/article"] = http.Response(200, b"Synthetic page", {})
    handlers[start] = http.Response(302, b"", {"location": destination})
    handlers[destination] = payload

    out = router.route(adapter, **params)

    assert out["status"] == "ok" and out["items"]
    initial = next(call for call in calls if call[0].split("?")[0] == start)
    assert initial[2]["headers"]["Authorization"] == "Bearer synthetic-redirect-token"
    redirected = [call for call in calls if call[0] == destination]
    assert len(redirected) == 1
    assert redirected[0][2]["headers"] == (initial[2]["headers"] if destination_kind == "same_origin" else {})
    assert all(not call[2]["headers"] for call in calls if call[0].endswith("/robots.txt"))
    if adapter == "web.jina":
        assert not next(call for call in calls if call[0] == params["url"])[2]["headers"]


@pytest.mark.parametrize("destination_kind", ["downgrade", "other_host", "other_port", "same_origin"])
def test_credential_query_redirect_origin(crawl, destination_kind):
    """Query credentials (app_id/app_key) never follow a redirect off the origin."""
    router, handlers, calls, _ = crawl
    start = "https://api.example/v1/search"
    credentials = {"app_id": "synthetic-app-id", "app_key": "synthetic-app-key"}
    destination = {
        "downgrade": "http://api.example/redirected",
        "other_host": "https://other.example/redirected",
        "other_port": "https://api.example:8443/redirected",
        "same_origin": "https://api.example/redirected",
    }[destination_kind] + "?" + urlencode(credentials)
    handlers[start] = http.Response(302, b"", {"location": destination})
    handlers[destination] = {"results": [{"url": "https://source.example/1", "title": "Synthetic result"}]}
    fetcher = http.Fetcher(router, "hn.algolia", get_policy("hn.algolia", router.config), "synthetic-user")

    if destination_kind == "same_origin":
        assert fetcher.json(start, params=credentials)["results"]
        assert calls[-1][0] == destination
    else:
        with pytest.raises(CrawlError, match="Credential-bearing redirect rejected"):
            fetcher.get(start, params=credentials)
        assert [call[0].split("?")[0] for call in calls] == ["https://api.example/robots.txt", start]
    initial = next(call for call in calls if call[0].split("?")[0] == start)
    assert "app_id=synthetic-app-id" in initial[0] and "app_key=synthetic-app-key" in initial[0]
    with router.store.connect() as db:
        audit = db.execute("SELECT url FROM crawl_audit").fetchall()
    assert all("app_key" not in row[0] for row in audit)


def test_jina_redirect_to_robots_denied_path_never_fetches_content(crawl):
    router, handlers, calls, _ = crawl
    handlers["https://source.example/robots.txt"] = http.Response(
        200, b"User-agent: *\nDisallow: /private", {})
    handlers["https://source.example/go"] = http.Response(
        302, b"", {"location": "/private"})

    out = router.route("web.jina", url="https://source.example/go")

    assert out["status"] == "disabled_by_policy" and out["items"] == []
    assert out["errors"] == ["robots.txt disallows this URL"]
    assert [call[0] for call in calls] == ["https://source.example/robots.txt", "https://source.example/go"]
    with router.store.connect() as db:
        denied = db.execute("SELECT url, status, bytes FROM crawl_audit WHERE status='robots_denied'").fetchall()
    assert [tuple(row) for row in denied] == [("https://source.example/private", "robots_denied", 0)]


def test_jina_extracts_final_allowed_redirect_url(crawl):
    router, handlers, calls, _ = crawl
    handlers["https://source.example/go"] = http.Response(302, b"", {"location": "/article"})
    handlers["https://source.example/article"] = http.Response(200, b"Synthetic final page", {})
    handlers["https://r.jina.ai/https://source.example/article"] = {
        "data": {"url": "https://source.example/article", "content": "Synthetic final extraction"}}

    out = router.route("web.jina", url="https://source.example/go")

    assert out["status"] == "ok"
    assert out["items"][0]["url"] == "https://source.example/article"
    assert out["items"][0]["markdown"] == "Synthetic final extraction"
    assert [call[0] for call in calls] == [
        "https://source.example/robots.txt", "https://source.example/go", "https://source.example/article",
        "https://r.jina.ai/robots.txt", "https://r.jina.ai/https://source.example/article",
    ]
