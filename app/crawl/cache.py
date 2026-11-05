"""Canonical request identities; persistence stays behind the data access layer."""
import hashlib
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.data_access.crawl import CrawlStore


def canonical_url(url):
    p = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", urlencode(sorted(query)), ""))


def cache_key(params, policy):
    values = {k: canonical_url(v) if k in {"url", "feed"} and isinstance(v, str)
              and v.startswith(("http://", "https://")) else v
              for k, v in params.items() if k != "user_id"}
    return hashlib.sha256(json.dumps([values, policy], sort_keys=True, default=str).encode()).hexdigest()
