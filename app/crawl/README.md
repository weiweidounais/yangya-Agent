# Crawl sources (PR1)

`from app.crawl import crawl_router; crawl_router.route("web", url=...)`
is the common entry point for chat and scheduled read-only tasks. The six read-only
tools are registered in `app.agent.tool_registry`. Chat uses model-planned calls
through the existing task loop; no crawl keywords are used for routing. Plans
are fixed before retrieval, and escaped source data cannot authorize more tools.

## Operator configuration

`config/crawl_policy.yaml` controls each adapter. `CRAWL_POLICY_OVERRIDES` is a JSON
object mapping adapter names to policy patches, read from the process environment
on each top-level call. Request/tool arguments cannot change these values.
Unknown adapters are disabled.
Robots requests count toward request quotas; disallowed paths never get fetched.

List exact reviewed, non-login RSSHub routes in the YAML file (`rsshub_routes`).
An empty list denies every RSSHub route. Configuration names:

- `CRAWL_USER_AGENT`: optional User-Agent override (default `PersonalAgentCrawler/1.0`);
  append a contact URL for your deployment.
- `RSSHUB_BASE_URL`, `SEARXNG_URL`: public HTTP(S) service endpoints. Private
  service addresses are deliberately rejected by the same SSRF guard.
- `X_BEARER_TOKEN`: official X recent-search access. No token means no items.
- `X_MONTHLY_SPEND_CAP_USD`: default 50, estimated at $0.005 per returned post.
  Reserve a full 10-post page before calling; refund unread posts after a valid
  response. Ambiguous failures retain the reservation to avoid overspending.
  Cached reads incur no cost and remain available when the budget is exhausted.
- `JINA_API_KEY`: optional bearer authorization for Reader and Search.
- `CRAWL_PROXY_URL`: optional HTTP CONNECT proxy, used only when policy.proxy is
  true. It must support CONNECT to destination ports. Proxy credentials never go
  to parser/browser libraries, results, audit or transport logging.

Cache, daily quota, X monthly spend, and fetch audits live in `DATA_DIR/crawl.db`,
separate from profile transactions. Cache identities include all request options
and policy. Public source data is shared; audit user identity is injected by the
registry. URLs in audit omit query strings and userinfo. SQLite transactions
serialize quotas across local worker processes; token buckets and concurrency
are process-wide. Multi-host deployments need a shared quota/rate service.

## Optional dependencies and Python 3.10

Core import does not import any heavy parser. Missing extras return
`not_configured`; web/search kinds try their ordered fallbacks. Install optional
parsers/browser from `requirements-crawl.txt`; Crawl4AI additionally needs its
Playwright Chromium installation. No browsers or dependencies are installed by
application import or tool execution.

The pinned [Crawl4AI 0.6.3](https://pypi.org/project/Crawl4AI/0.6.3/) supports Python
3.9 and later. Install the optional extras in a worker environment that matches
the core pins; **do not downgrade core dependencies** to satisfy an extra.

HTTP destinations are resolved, all DNS answers checked, and the connection is
pinned to a public IP with original-host TLS verification. Redirects re-enter
the guard. Browser subrequests use guarded HTTP fulfillment; WebSockets are
blocked. Third-party Reader/Search services control their own downstream network
and DNS. Response bytes and extracted text have fixed bounds; no source content
is interpreted as an instruction.

Tests use synthetic payloads, mocked optional packages, a fake clock, and blocked
socket connections. Live API/browser compatibility, upstream availability and
the combined optional dependency install are not asserted by these tests.
