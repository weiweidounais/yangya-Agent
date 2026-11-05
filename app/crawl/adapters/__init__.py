"""Adapters only parse sources; network I/O goes through the guarded Fetcher."""
from importlib import import_module

from ..errors import NotConfigured


def optional(module):
    try:
        return import_module(module)
    except ImportError:
        raise NotConfigured(f"未配置 optional dependency: {module}") from None


def fetch(name, ctx, params):
    if name.startswith("web."):
        from .web import fetch_web
        return fetch_web(name, ctx, params)
    if name.startswith("feed."):
        from .feeds import fetch_feed
        return fetch_feed(name, ctx, params)
    if name.startswith("hn."):
        from .hn import fetch_hn
        return fetch_hn(name, ctx, params)
    if name.startswith("search."):
        from .search import search
        return search(name, ctx, params)
    if name == "social.x_api":
        from .social import search_x
        return search_x(ctx, params)
    raise NotConfigured("Unknown crawl adapter")
