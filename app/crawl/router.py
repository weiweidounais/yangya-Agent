"""One policy boundary for every source and every public agent crawl tool."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import adapters
from .cache import CrawlStore, cache_key, canonical_url
from .errors import CrawlError, NotConfigured
from .http import Fetcher, public_url, redact, send
from .policy import get_policy, load_config
from .ratelimit import RateLimiter

_LIMITER = RateLimiter()
_DEFAULT = None
_LOCK = threading.Lock()
_PARAMS = {"url", "query", "feed", "kind", "location", "remote", "sources", "sites", "recency_days", "user_id"}


def result(status="ok", *, items=None, cached=False, policy=None, errors=None):
    return {"items": items or [], "cached": cached, "policy": policy or {}, "errors": errors or [], "status": status}


class CrawlRouter:
    def __init__(self, *, config=None, store=None, limiter=None, sender=None, clock=time.time):
        self.config = config if config is not None else load_config()
        self.store = store or CrawlStore(Path(os.getenv("DATA_DIR", "./data")) / "crawl.db")
        self.limiter, self.sender, self.clock = limiter or _LIMITER, sender or send, clock

    def date(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc)

    def route(self, adapter_or_kind, **params):
        try:
            out = self._route(adapter_or_kind, params)
            # Status and policy enums are trusted structure, not upstream content.
            out["errors"] = redact(out["errors"])
            out["items"] = self.sanitize_items(out["items"])
            return out
        except CrawlError as exc:
            return result(exc.status, errors=[str(exc)])
        except Exception:
            # Never expose library errors: URLs, API keys and proxy credentials
            # routinely appear in their text. Do not log exception repr/traceback.
            return result("error", errors=["Source retrieval failed"])

    @staticmethod
    def sanitize_items(items):
        clean = []
        for item in items:
            safe = redact(item)
            try:
                public_url(safe["url"])
                public_url(safe["canonical_url"])
            except CrawlError:
                continue
            safe["adapter"] = item["adapter"]
            safe["fetched_at"] = item["fetched_at"]
            clean.append(safe)
        return clean

    def _route(self, name, params):
        if name in {"web", "search"}:
            choices = ["web.trafilatura", "web.crawl4ai", "web.jina"] if name == "web" else ["search.searxng", "search.jina"]
            if name == "search":
                key = cache_key(params, {"searxng": os.getenv("SEARXNG_URL", "")})
                cached = self.store.get(name, key, self.clock())
                if cached is not None:
                    policies = {}
                    for choice in choices:
                        policy = get_policy(choice, self.config)
                        flags = {"adapter": choice, **policy.public()}
                        if not policy.enabled:
                            return result("disabled_by_policy", policy=flags, errors=["Adapter disabled by operator policy"])
                        # Check remaining budget without charging a cache hit.
                        if not self.store.reserve("daily:" + choice, self.date().strftime("%Y-%m-%d"),
                                                  0, policy.daily_cap - 1):
                            return result("disabled_by_policy", policy=flags, errors=["Adapter daily request cap reached"])
                        policies[choice] = policy.public()
                        if choice == cached["result"]["policy"]["adapter"]:
                            break
                    if policies == cached["policies"]:
                        return {**cached["result"], "cached": True}
            tried = []
            for choice in choices:
                out = self.route(choice, **params)
                tried.append(out)
                if out["status"] == "disabled_by_policy" or out["items"]:
                    if name == "search" and out["status"] == "ok" and out["items"]:
                        policy = get_policy(choice, self.config)
                        # Preserve the source result's TTL even if it was already cached.
                        fetched = min(datetime.fromisoformat(item["fetched_at"]).timestamp() for item in out["items"])
                        policies = {entry["policy"]["adapter"]: get_policy(entry["policy"]["adapter"], self.config).public()
                                    for entry in tried}
                        self.store.put(name, key, {"result": out, "policies": policies}, fetched + policy.cache_ttl_s)
                    return out
            return self.combine(tried)
        if name == "feed":
            return self.route("feed.rsshub" if str(params.get("feed", "")).startswith("/") else "feed.rss", **params)
        if name == "hn":
            adapter = "hn.algolia" if params.get("query") else "hn.firebase"
            return self.route(adapter, **params)
        policy = get_policy(name, self.config)
        flags = {"adapter": name if name in self.config.get("adapters", {}) else redact(name), **policy.public()}
        if not policy.enabled:
            return result("disabled_by_policy", policy=flags, errors=["Adapter disabled by operator policy"])
        if set(params) - _PARAMS:
            return result("error", policy=flags, errors=["Unsupported crawl parameters; policy is operator-controlled"])
        if name == "social.reddit_search":
            args = {**params, "query": "site:reddit.com " + str(params.get("query", ""))}
            out = self.route("search", **args)
            out["items"] = [x for x in out["items"] if (public_url(x["url"]).hostname or "").lower() in {"reddit.com", "www.reddit.com", "old.reddit.com"}]
            return out
        if name == "social.x_api" and not os.getenv("X_BEARER_TOKEN"):
            return result("not_configured", policy=flags, errors=["未配置 X API（需要 X_BEARER_TOKEN）"])
        days = params.get("recency_days")
        if days is not None and (type(days) is not int or not 1 <= days <= 365):
            raise CrawlError("recency_days must be between 1 and 365")
        key = cache_key(params, {**flags, "rsshub_routes": self.config.get("rsshub_routes"),
                                 "rsshub": os.getenv("RSSHUB_BASE_URL", ""), "searxng": os.getenv("SEARXNG_URL", "")})
        cached = self.store.get(name, key, self.clock())
        if cached is not None:
            return result(items=cached, cached=True, policy=flags)
        try:
            rows = adapters.fetch(name, Fetcher(self, name, policy, str(params.get("user_id", ""))), params)
            items = self.sanitize_items(self.normalize(rows, name))
            self.store.put(name, key, items, self.clock() + policy.cache_ttl_s)
            return result(items=items, policy=flags)
        except CrawlError as exc:
            return result(exc.status, policy=flags, errors=[str(exc)])
        except Exception:
            return result("error", policy=flags, errors=["Source retrieval failed"])

    def normalize(self, rows, adapter):
        items = []
        for row in rows[:100]:
            try:
                url = row.get("url")
                public_url(url)
            except (CrawlError, AttributeError):
                continue
            item = {"url": url, "canonical_url": canonical_url(url), "title": str(row.get("title") or "")[:500],
                    "snippet": str(row.get("snippet") or "")[:2000], "published_at": str(row["published_at"]) if row.get("published_at") else None,
                    "source": public_url(url).hostname, "adapter": adapter, "fetched_at": self.date().isoformat()}
            if row.get("markdown"):
                item["markdown"] = str(row["markdown"])[:20000]
            items.append(item)
        return items

    @staticmethod
    def combine(outputs):
        items = [item for out in outputs for item in out["items"]]
        statuses = [out["status"] for out in outputs]
        status = "ok" if "ok" in statuses else next((s for s in ("disabled_by_policy", "error", "not_configured") if s in statuses), "ok")
        return result(status, items=items, cached=bool(outputs) and all(out["cached"] for out in outputs),
                      policy={"sources": [out["policy"] for out in outputs]}, errors=[e for out in outputs for e in out["errors"]])


def route(adapter_or_kind, **params):
    global _DEFAULT
    try:
        with _LOCK:
            # Reload operator policy on each request, including environment overrides.
            config = load_config()
            if _DEFAULT is None or _DEFAULT.config != config:
                _DEFAULT = CrawlRouter(config=config)
            router = _DEFAULT
        return router.route(adapter_or_kind, **params)
    except CrawlError as exc:
        return result(exc.status, errors=[str(exc)])
