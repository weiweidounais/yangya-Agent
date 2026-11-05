"""Synthetic-only router/security contracts; socket I/O is forbidden."""
import copy
import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from app.crawl import adapters, http
from app.crawl.cache import CrawlStore
from app.crawl.errors import CrawlError
from app.crawl.policy import get_policy, load_config
from app.crawl.ratelimit import RateLimiter
from app.crawl.router import CrawlRouter


class Clock:
    def __init__(self):
        self.now = 1700000000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def crawl(monkeypatch, tmp_path):
    for key in (*http.SECRET_ENV, "RSSHUB_BASE_URL", "SEARXNG_URL", "X_MONTHLY_SPEND_CAP_USD",
                "CRAWL_POLICY_OVERRIDES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("Real network forbidden")))
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("Real network forbidden")))
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]))
    clock = Clock()
    handlers = {}
    calls = []

    def sender(url, ip, **kwargs):
        calls.append((url, ip, kwargs))
        if url in handlers:
            value = handlers[url]
        elif url.split("?")[0] in handlers:
            value = handlers[url.split("?")[0]]
        elif url.endswith("/robots.txt"):
            value = http.Response(200, b"User-agent: *\nAllow: /", {})
        else:
            raise AssertionError("Unexpected mock URL: " + url)
        if callable(value):
            value = value(url, kwargs)
        if isinstance(value, Exception):
            raise value
        if not isinstance(value, http.Response):
            value = http.Response(200, json.dumps(value).encode(), {})
        value.url = url
        return value

    router = CrawlRouter(config=copy.deepcopy(load_config()), store=CrawlStore(tmp_path / "crawl.db"),
                         clock=clock, limiter=RateLimiter(clock=clock, sleep=clock.sleep), sender=sender)
    return router, handlers, calls, clock


# A query-driven JSON adapter exercises redirects, robots, proxy and quota paths.
HN_SEARCH = "https://hn.algolia.com/api/v1/search"
HN_ROBOTS = "https://hn.algolia.com/robots.txt"


def hits(*rows):
    return {"hits": [{"objectID": str(i + 1), "url": "https://source.example/" + str(i + 1), **row}
                     for i, row in enumerate(rows)]}


def test_schema_drops_missing_and_non_http_urls(crawl, monkeypatch):
    router, _, calls, _ = crawl
    monkeypatch.setattr(adapters, "fetch", lambda *args: [
        {}, {"url": ""}, {"url": None}, {"url": "file:///etc/passwd"},
        {"url": "https://source.example/story?utm_source=test#part", "title": "Synthetic item"}])
    out = router.route("web.trafilatura", url="https://source.example/story")
    assert set(out) == {"items", "cached", "policy", "errors", "status"}
    assert out["status"] == "ok" and len(out["items"]) == 1
    assert out["items"][0]["canonical_url"] == "https://source.example/story"
    assert set(out["items"][0]) == {"url", "canonical_url", "title", "snippet", "published_at", "source", "adapter", "fetched_at"}
    assert not calls


def test_disabled_cannot_be_enabled_by_arguments_or_cache(crawl, monkeypatch):
    router, _, calls, _ = crawl
    dispatch = Mock(return_value=[{"url": "https://source.example/item"}])
    monkeypatch.setattr(adapters, "fetch", dispatch)
    router.route("web.trafilatura", url="https://source.example")
    router.config["adapters"]["web.trafilatura"]["enabled"] = False
    for params in ({}, {"enabled": True}, {"policy": {"enabled": True}}):
        out = router.route("web.trafilatura", url="https://source.example", **params)
        assert out["status"] == "disabled_by_policy" and out["items"] == []
    assert dispatch.call_count == 1 and not calls


def test_cache_ttl_and_canonical_identity(crawl, monkeypatch):
    router, _, _, clock = crawl
    fetch = Mock(return_value=[{"url": "https://source.example/item"}])
    monkeypatch.setattr(adapters, "fetch", fetch)
    assert not router.route("web.trafilatura", url="https://source.example/item?utm_source=a")["cached"]
    assert router.route("web.trafilatura", url="https://source.example/item")["cached"]
    assert fetch.call_count == 1
    clock.now += 7201
    assert not router.route("web.trafilatura", url="https://source.example/item")["cached"]
    assert fetch.call_count == 2


@pytest.fixture
def search_fallback(crawl, monkeypatch):
    router, handlers, _, _ = crawl
    monkeypatch.setenv("SEARXNG_URL", "https://search.example")
    searxng = Mock(return_value=http.Response(500, b"Synthetic failure", {}))
    jina = Mock(return_value=http.Response(200, json.dumps({"data": [
        {"url": "https://source.example/result", "title": "Synthetic result"}
    ]}).encode(), {}))
    handlers["https://search.example/search"] = searxng
    handlers["https://s.jina.ai/synthetic"] = jina
    router.sender = Mock(wraps=router.sender)
    first = router.route("search", query="synthetic")
    assert first["status"] == "ok" and first["cached"] is False
    assert first["items"][0]["adapter"] == "search.jina"
    searxng.assert_called_once()
    jina.assert_called_once()
    router.sender.reset_mock()
    searxng.reset_mock()
    jina.reset_mock()
    return router, searxng, jina, first


def test_search_fallback_cache_first_and_ttl(search_fallback, crawl):
    router, searxng, jina, first = search_fallback
    _, _, _, clock = crawl
    with router.store.connect() as db:
        quota = db.execute("SELECT * FROM crawl_quota ORDER BY scope").fetchall()
    second = router.route("search", query="synthetic")
    assert second == {**first, "cached": True}
    router.sender.assert_not_called()
    searxng.assert_not_called()
    jina.assert_not_called()
    with router.store.connect() as db:
        assert db.execute("SELECT * FROM crawl_quota ORDER BY scope").fetchall() == quota

    clock.now += get_policy("search.jina", router.config).cache_ttl_s + 1
    expired = router.route("search", query="synthetic")
    assert expired["status"] == "ok" and expired["cached"] is False
    searxng.assert_called_once()
    jina.assert_called_once()


@pytest.mark.parametrize("adapter", ["search.searxng", "search.jina"])
@pytest.mark.parametrize("restriction", ["disabled", "budget"])
def test_search_fallback_cache_rechecks_policy(search_fallback, adapter, restriction):
    router, searxng, jina, _ = search_fallback
    if restriction == "disabled":
        router.config["adapters"][adapter]["enabled"] = False
        message = "Adapter disabled by operator policy"
    else:
        cap = get_policy(adapter, router.config).daily_cap
        day = router.date().strftime("%Y-%m-%d")
        with router.store.connect() as db:
            used = db.execute("SELECT used FROM crawl_quota WHERE scope=? AND period=?",
                              ("daily:" + adapter, day)).fetchone()[0]
        assert router.store.reserve("daily:" + adapter, day, cap - used, cap)
        message = "Adapter daily request cap reached"
    out = router.route("search", query="synthetic")
    assert out["status"] == "disabled_by_policy" and out["items"] == []
    assert out["errors"] == [message] and out["policy"]["adapter"] == adapter
    router.sender.assert_not_called()
    searxng.assert_not_called()
    jina.assert_not_called()


def test_token_bucket_is_per_domain(crawl):
    router, _, _, clock = crawl
    limiter = router.limiter
    limiter.acquire("one.example", 1, 30)
    limiter.acquire("two.example", 1, 30)
    assert not clock.sleeps
    limiter.acquire("one.example", 1, 30)
    assert clock.sleeps == [30]


def test_global_concurrency_bound():
    limiter = RateLimiter(concurrency=2)
    policy = get_policy("web.trafilatura", load_config())
    release, entered = threading.Event(), threading.Event()
    lock = threading.Lock()
    counts = [0, 0]

    def run(i):
        with limiter.slot(str(i), policy):
            with lock:
                counts[0] += 1
                counts[1] = max(counts)
                if counts[0] == 2:
                    entered.set()
            assert release.wait(2)
            with lock:
                counts[0] -= 1

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run, i) for i in range(4)]
        assert entered.wait(2)
        assert counts[1] == 2
        release.set()
        for future in futures:
            future.result()
    assert counts[1] == 2


@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://127.2.3.4", "http://169.254.169.254",
    "http://10.5.2.3", "http://172.16.0.1", "http://192.168.1.1", "http://localhost",
    "http://a.localhost", "http://[::1]", "http://[fe80::1]", "http://[::ffff:127.0.0.1]", "file:///etc/passwd"])
def test_ssrf_rejected_without_network(crawl, url):
    router, _, calls, _ = crawl
    assert router.route("web.jina", url=url)["status"] == "error"
    assert not calls


def test_private_dns_and_mixed_answers_rejected(crawl, monkeypatch):
    router, _, calls, _ = crawl
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 80))])
    assert router.route("web.jina", url="https://source.example")["status"] == "error"
    assert not calls


def test_redirect_cannot_access_private_host(crawl):
    router, handlers, calls, _ = crawl
    handlers[HN_SEARCH] = http.Response(302, b"", {"location": "http://10.1.1.1/secret"})
    out = router.route("hn.algolia", query="synthetic")
    assert out["status"] == "error"
    assert not any("10.1.1.1" in call[0] for call in calls)


def test_proxy_opt_in_and_no_credentials_in_result_audit_or_logs(crawl, monkeypatch, caplog):
    router, handlers, calls, _ = crawl
    proxy = "http://synthetic-proxy-user:synthetic-proxy-secret@proxy.example:8080"
    monkeypatch.setenv("CRAWL_PROXY_URL", proxy)
    handlers[HN_SEARCH] = hits({"title": proxy})
    plain = router.route("hn.algolia", query="first")
    assert all(c[2]["proxy"] is None for c in calls)
    router.config["adapters"]["hn.algolia"]["proxy"] = True
    proxied = router.route("hn.algolia", query="second")
    assert calls[-1][2]["proxy"] == proxy
    handlers[HN_SEARCH] = RuntimeError(proxy)
    failed = router.route("hn.algolia", query="third")
    with router.store.connect() as db:
        audit = db.execute("SELECT * FROM crawl_audit").fetchall()
    rendered = json.dumps([plain, proxied, failed, audit]) + caplog.text
    assert proxy not in rendered and "synthetic-proxy-secret" not in rendered
    assert failed["errors"] == ["HTTP fetch failed"]


@pytest.mark.parametrize("path", ["/api/", "/*", "/api/*", "/*v1/search*", "/*hitsPerPage=20$"])
def test_robots_disallow(crawl, path):
    router, handlers, calls, _ = crawl
    handlers[HN_ROBOTS] = http.Response(200, ("User-agent: *\nDisallow: " + path).encode(), {})
    out = router.route("hn.algolia", query="synthetic")
    assert out["status"] == "disabled_by_policy"
    assert [c[0] for c in calls] == [HN_ROBOTS]


@pytest.mark.parametrize("encoded", ["%24", "%2A", "%2a"])
def test_robots_encoded_literals_cannot_allow_disallowed_page(crawl, encoded):
    router, handlers, calls, _ = crawl
    handlers["https://source.example/robots.txt"] = http.Response(
        200, f"User-agent: *\nDisallow: /private$\nAllow: /private{encoded}".encode(), {})
    out = router.route("web.jina", url="https://source.example/private")
    assert out["status"] == "disabled_by_policy"
    assert out["items"] == []
    assert out["errors"] == ["robots.txt disallows this URL"]
    assert [c[0] for c in calls] == ["https://source.example/robots.txt"]


@pytest.mark.parametrize("rule,path,allowed", [
    ("/private%24", "/private", True),
    ("/private%24", "/private%24", False),
    ("/private%24$", "/private%24/extra", True),
    ("/private%2A", "/private/extra", True),
    ("/private%2A$", "/private%2a", False),
    ("/private%2a$", "/private%2A", False),
    ("/private%2F", "/private/", True),
    ("/private/", "/private%2F", True),
    ("/private%3F", "/private?query", True),
    ("/private?value=%2A$", "/private?value=%2a", False),
    ("/pr%69vate$", "/private", False),
    ("/private$", "/pr%69vate", False),
    ("/caf%C3%A9$", "/café", False),
    ("/café$", "/caf%c3%a9", False),
])
def test_robots_percent_encoding_comparison(rule, path, allowed):
    assert http.robots_allowed(
        "User-agent: *\nDisallow: " + rule, "https://source.example" + path) is allowed


def test_robots_encoded_literals_do_not_inflate_rule_specificity():
    assert not http.robots_allowed(
        "User-agent: *\nDisallow: /a*bcd\nAllow: /a%2Ab", "https://source.example/a%2Abcd")


@pytest.mark.parametrize("rules,allowed", [
    ("User-agent: *\nDisallow: /*\nAllow: /api/", True),
    ("User-agent: *\nDisallow: /api/*\nAllow: /api/*", True),
    ("User-agent: *\nDisallow: /api/v1/search$", True),
    ("User-agent: *\nDisallow: /*\nUser-agent: PersonalAgentCrawler\nAllow: /", True),
    ("User-agent: *\nAllow: /\nUser-agent: PersonalAgentCrawler\nDisallow: /*", False),
    ("User-agent: PersonalAgentCrawler\nAllow: /\nUser-agent: PersonalAgentCrawler\nDisallow: /api/*", False),
])
def test_robots_wildcard_precedence_and_anchors(crawl, rules, allowed):
    router, handlers, calls, _ = crawl
    handlers[HN_ROBOTS] = http.Response(200, rules.encode(), {})
    handlers[HN_SEARCH] = hits()
    out = router.route("hn.algolia", query="synthetic")
    assert out["status"] == ("ok" if allowed else "disabled_by_policy")
    assert len(calls) == (2 if allowed else 1)


@pytest.mark.parametrize("username,expected_items", [("ok", 1), ("http", 0), ("low", 1)])
def test_redaction_preserves_schema_on_fetch_and_cache(crawl, monkeypatch, username, expected_items):
    router, handlers, calls, _ = crawl
    proxy = f"http://{username}:synthetic-password@proxy.example:8080"
    monkeypatch.setenv("CRAWL_PROXY_URL", proxy)
    handlers[HN_SEARCH] = hits({"title": proxy, "story_text": "synthetic-password"})
    for cached in (False, True):
        out = router.route("hn.algolia", query="synthetic")
        assert out["status"] == "ok"
        assert out["policy"]["tos_risk"] == "low"
        assert out["cached"] is cached
        assert len(out["items"]) == expected_items
        for item in out["items"]:
            assert http.public_url(item["url"])
            assert http.public_url(item["canonical_url"])
        assert proxy not in json.dumps(out) and "synthetic-password" not in json.dumps(out)
    assert len(calls) == 2


def test_cached_urls_revalidated_after_credentials_change(crawl, monkeypatch):
    router, handlers, calls, _ = crawl
    handlers[HN_SEARCH] = hits({})
    assert router.route("hn.algolia", query="synthetic")["items"]
    monkeypatch.setenv("CRAWL_PROXY_URL", "http://http:synthetic-password@proxy.example:8080")
    out = router.route("hn.algolia", query="synthetic")
    assert out["cached"] is True and out["items"] == [] and out["status"] == "ok"
    assert len(calls) == 2


def test_robots_failure_closed_and_policy_opt_out(crawl):
    router, handlers, calls, _ = crawl
    handlers[HN_ROBOTS] = http.Response(503, b"", {})
    assert router.route("hn.algolia", query="synthetic")["status"] == "disabled_by_policy"
    router.config["adapters"]["hn.algolia"]["robots_respect"] = False
    handlers[HN_SEARCH] = hits()
    assert router.route("hn.algolia", query="synthetic")["status"] == "ok"
    assert len(calls) == 2


def test_daily_cap_persisted_and_audit_identity(crawl):
    router, handlers, calls, _ = crawl
    router.config["adapters"]["hn.algolia"].update(daily_cap=2)
    handlers[HN_SEARCH] = hits()
    assert router.route("hn.algolia", query="one", user_id="synthetic-user")["status"] == "ok"
    assert router.route("hn.algolia", query="two", user_id="synthetic-user")["status"] == "disabled_by_policy"
    assert len(calls) == 2  # robots and the first API call
    with router.store.connect() as db:
        rows = db.execute("SELECT user_id, adapter, url, status, bytes FROM crawl_audit").fetchall()
    assert all(row[0] == "synthetic-user" and row[1] == "hn.algolia" for row in rows)
    assert all("?" not in row[2] and row[4] > 0 for row in rows)


def test_operator_override_env_only_and_unknown_adapters_disabled(monkeypatch):
    assert get_policy("hn.algolia", load_config()).enabled
    monkeypatch.setenv("CRAWL_POLICY_OVERRIDES", json.dumps({"hn.algolia": {"enabled": False, "daily_cap": 5}}))
    policy = get_policy("hn.algolia", load_config())
    assert not policy.enabled and policy.daily_cap == 5 and policy.tos_risk == "low"
    for name in ("unknown.adapter", "another.unknown", "web.unlisted"):
        assert name not in load_config()["adapters"]
        assert not get_policy(name, load_config()).enabled
    monkeypatch.setenv("CRAWL_POLICY_OVERRIDES", json.dumps({"hn.algolia": {"tos_risk": "unrated"}}))
    with pytest.raises(CrawlError):
        get_policy("hn.algolia", load_config())


def test_budget_reservation_atomic(crawl):
    router, _, _, _ = crawl
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: router.store.reserve("test", "month", 1, 3), range(20)))
    assert sum(results) == 3
