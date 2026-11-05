"""Bounded, pinned-IP HTTP transport. Every redirect re-enters the same guards."""
from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit, unquote, unquote_to_bytes

from .errors import CrawlError, NotConfigured, PolicyDenied

# Operators may append a contact URL, e.g. "PersonalAgentCrawler/1.0 (+https://example.com/bot)".
USER_AGENT = (os.getenv("CRAWL_USER_AGENT") or "").strip() or "PersonalAgentCrawler/1.0"
MAX_BYTES = 2 * 1024 * 1024
SECRET_ENV = ("CRAWL_PROXY_URL", "JINA_API_KEY", "X_BEARER_TOKEN")


def redact(value):
    """Sanitize even upstream content which happens to echo a credential."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    secrets = [os.getenv(k, "") for k in SECRET_ENV]
    proxy = os.getenv("CRAWL_PROXY_URL", "")
    if proxy:
        p = urlsplit(proxy)
        secrets.extend([p.username or "", unquote(p.username or ""), p.password or "", unquote(p.password or "")])
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            value = value.replace(secret, "[redacted]")
    return value


def public_url(url):
    if not isinstance(url, str) or not url or any(ord(c) < 33 for c in url) or "\\" in url:
        raise CrawlError("Invalid public HTTP URL")
    try:
        p = urlsplit(url)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            raise ValueError()
        if p.port is not None and not 1 <= p.port <= 65535:
            raise ValueError()
        host = p.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local")):
            raise ValueError()
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and (not ip.is_global or ip.is_multicast):
            raise ValueError()
        return p
    except ValueError:
        raise CrawlError("SSRF: non-public URL rejected") from None


def resolve_public(url):
    p = public_url(url)
    try:
        addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80),
                                       type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(row[4][0]) for row in addresses]
        if not ips or any(not ip.is_global or ip.is_multicast for ip in ips):
            raise CrawlError("SSRF: non-public DNS address rejected")
        return str(ips[0])
    except (OSError, ValueError):
        raise CrawlError("DNS resolution failed") from None


def audit_url(url):
    try:
        p = urlsplit(url)
        # Query values and userinfo never enter the audit trail.
        return redact(urlunsplit((p.scheme, p.hostname or "", p.path, "", "")))[:1000]
    except ValueError:
        return "invalid-url"


def _robots_path(value):
    """Normalize octets without turning encoded reserved literals into syntax."""
    def normalize(match):
        token = match.group()
        if token.startswith("%"):
            char = chr(int(token[1:], 16))
            if char in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~":
                return char
            return token.upper()
        return "".join(f"%{byte:02X}" for byte in token.encode("utf-8"))

    return re.sub(r"%[0-9a-fA-F]{2}|[^\x00-\x7f]", normalize, value)


def robots_allowed(text, url):
    """Match the most specific agent group and path, including * and final $."""
    groups, agents, rules = [], [], []
    for line in text.splitlines():
        key, sep, value = line.split("#", 1)[0].partition(":")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif key in {"allow", "disallow"} and agents:
            rules.append((value, key == "allow"))
    groups.append((agents, rules))
    product = USER_AGENT.split("/", 1)[0].lower()
    matching = []
    for agents, rules in groups:
        specificity = max((0 if agent == "*" else len(agent)
                           for agent in agents if agent == "*" or agent in product), default=-1)
        if specificity >= 0:
            matching.append((specificity, rules))
    best = max((specificity for specificity, _ in matching), default=-1)
    parsed = urlsplit(url)
    target = _robots_path((parsed.path or "/") + ("?" + parsed.query if parsed.query else ""))
    matches = []
    for specificity, rules in matching:
        if specificity != best:
            continue
        for path, allow in rules:
            if not path:
                continue
            path = _robots_path(path)
            anchored = path.endswith("$")
            pattern = path[:-1] if anchored else path
            expression = "^" + ".*".join(re.escape(part) for part in pattern.split("*"))
            if re.search(expression + ("$" if anchored else ""), target):
                matches.append((len(unquote_to_bytes(pattern.replace("*", ""))), allow))
    # Allow wins ties between equally specific rules.
    return max(matches, default=(0, True))[1]


@dataclass
class Response:
    status_code: int
    content: bytes
    headers: dict
    url: str = ""

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.content)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, server_hostname, **kwargs):
        super().__init__(host, port, **kwargs)
        self.server_hostname = server_hostname

    def connect(self):
        # HTTPConnection performs the optional proxy CONNECT first. TLS still
        # verifies the original hostname, while the TCP/CONNECT address is pinned.
        http.client.HTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.server_hostname)


def send(url, ip, *, proxy=None, headers=None, method="GET", body=None):
    """No library logging, ambient proxies, cookies, retries or auto redirects."""
    p = urlsplit(url)
    port = p.port or (443 if p.scheme == "https" else 80)
    host, connect_port = ip, port
    tunnel_headers = {}
    if proxy:
        proxy_parts = urlsplit(proxy)
        if proxy_parts.scheme != "http" or not proxy_parts.hostname:
            raise NotConfigured("CRAWL_PROXY_URL requires an HTTP CONNECT proxy")
        host, connect_port = proxy_parts.hostname, proxy_parts.port or 80
        if proxy_parts.username:
            auth = f"{unquote(proxy_parts.username)}:{unquote(proxy_parts.password or '')}"
            tunnel_headers["Proxy-Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
    connection = (_PinnedHTTPS(host, connect_port, p.hostname, timeout=15)
                  if p.scheme == "https" else http.client.HTTPConnection(host, connect_port, timeout=15))
    if proxy:
        connection.set_tunnel(ip, port, headers=tunnel_headers)
    request_headers = {**(headers or {}), "Host": p.netloc, "User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    try:
        connection.request(method, urlunsplit(("", "", p.path or "/", p.query, "")), body, request_headers)
        response = connection.getresponse()
        content = response.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise CrawlError("Response exceeds crawl byte limit")
        return Response(response.status, content, {k.lower(): v for k, v in response.getheaders()}, url)
    finally:
        connection.close()


class Fetcher:
    def __init__(self, router, adapter, policy, user_id):
        self.router, self.adapter, self.policy, self.user_id = router, adapter, policy, user_id

    def robots(self, url):
        if not self.policy.robots_respect:
            return
        p = public_url(url)
        root = urlunsplit((p.scheme, p.netloc, "/robots.txt", "", ""))
        key = "robots:" + root
        saved = self.router.store.get(self.adapter, key, self.router.clock())
        if saved is None:
            response = self.get(root, check_robots=False)
            if response.status_code in {404, 410}:
                text = "User-agent: *\nAllow: /"
            elif response.status_code == 200:
                text = response.text
            else:
                raise PolicyDenied("robots.txt unavailable or denied")
            saved = {"text": text}
            self.router.store.put(self.adapter, key, saved, self.router.clock() + 1800)
        if not robots_allowed(saved["text"], url):
            self.router.store.audit(self.user_id, self.adapter, audit_url(url), "robots_denied", 0, self.router.clock())
            raise PolicyDenied("robots.txt disallows this URL")

    def get(self, url, *, params=None, headers=None, check_robots=True, method="GET", body=None):
        if params:
            url += ("&" if "?" in url else "?") + urlencode({k: v for k, v in params.items() if v is not None})
        parsed = urlsplit(url)
        origin = (parsed.scheme, parsed.netloc)
        for _ in range(6):
            ip = resolve_public(url)
            if check_robots:
                self.robots(url)
            p = public_url(url)
            day = self.router.date().strftime("%Y-%m-%d")
            if not self.router.store.reserve("daily:" + self.adapter, day, 1, self.policy.daily_cap):
                raise PolicyDenied("Adapter daily request cap reached")
            status, size = "error", 0
            try:
                with self.router.limiter.slot(p.hostname, self.policy):
                    response = self.router.sender(url, ip, proxy=os.getenv("CRAWL_PROXY_URL") if self.policy.proxy else None,
                                                  headers=headers, method=method, body=body)
                size, status = len(response.content), str(response.status_code)
                if size > MAX_BYTES:
                    raise CrawlError("Response exceeds crawl byte limit")
            except CrawlError:
                raise
            except Exception:
                raise CrawlError("HTTP fetch failed") from None
            finally:
                self.router.store.audit(self.user_id, self.adapter, audit_url(url), status, size, self.router.clock())
            if response.status_code in {301, 302, 303, 307, 308}:
                url = urljoin(url, response.headers.get("location", ""))
                destination = urlsplit(url)
                if (destination.scheme, destination.netloc) != origin or destination.scheme == "http":
                    headers = {}  # Never forward API authorization across origins or onto HTTP.
                    if params and any(k in params for k in ("app_id", "app_key")):
                        raise CrawlError("Credential-bearing redirect rejected")
                if response.status_code == 303:
                    method, body = "GET", None
                continue
            return response
        raise CrawlError("Redirect limit exceeded")

    def json(self, url, **kwargs):
        response = self.get(url, **kwargs)
        if response.status_code != 200:
            raise CrawlError("Source returned an unsuccessful HTTP status")
        return response.json()
