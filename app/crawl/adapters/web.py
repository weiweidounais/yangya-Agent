import asyncio
import os

from . import optional
from ..errors import CrawlError
from ..http import USER_AGENT, resolve_public


def fetch_web(name, ctx, params):
    url = params.get("url", "")
    resolve_public(url)
    if name == "web.trafilatura":
        extractor = optional("trafilatura")
        response = ctx.get(url)
        if response.status_code != 200:
            raise CrawlError("Web fetch failed")
        text = extractor.extract(response.text, include_comments=False, include_tables=True)
        return [{"url": url, "title": "", "markdown": text, "snippet": text}] if text else []
    if name == "web.jina":
        # Validate every origin redirect before the reader proxy can fetch it.
        response = ctx.get(url)
        if response.status_code != 200:
            raise CrawlError("Web fetch failed")
        url = response.url
        headers = {"Accept": "application/json", "X-No-Cache": "true"}
        if os.getenv("JINA_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["JINA_API_KEY"]
        data = ctx.json("https://r.jina.ai/" + url, headers=headers).get("data", {})
        return [{"url": data.get("url") or url, "title": data.get("title"),
                 "markdown": data.get("content"), "snippet": data.get("description") or data.get("content")}]
    crawler = optional("crawl4ai")
    return asyncio.run(_render(crawler, ctx, url))


async def _render(module, ctx, url):
    async def prepare(page, context, **kwargs):
        async def intercept(route):
            request = route.request
            # Subresources, redirects and XHR use the same SSRF/rate/robots gate.
            if request.method != "GET":
                await route.abort()
                return
            try:
                response = await asyncio.to_thread(ctx.get, request.url)
                headers = {k: v for k, v in response.headers.items()
                           if k in {"content-type", "location"}}
                await route.fulfill(status=response.status_code, headers=headers, body=response.content)
            except Exception:
                await route.abort()

        await context.route("**/*", intercept)
        # WebSockets never bypass the HTTP guard. Supported by the pinned browser.
        await context.route_web_socket("**/*", lambda ws: ws.close())
        return page

    config = module.BrowserConfig(headless=True, verbose=False, user_agent=USER_AGENT,
                                  extra_args=["--disable-background-networking", "--disable-features=ServiceWorker",
                                              "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
    async with module.AsyncWebCrawler(config=config) as crawler:
        crawler.crawler_strategy.set_hook("on_page_context_created", prepare)
        result = await crawler.arun(url=url, config=module.CrawlerRunConfig(
            cache_mode=module.CacheMode.BYPASS, verbose=False, page_timeout=15000))
        if not result.success:
            raise CrawlError("Browser extraction failed")
        markdown = getattr(result.markdown, "raw_markdown", result.markdown)
        return [{"url": url, "title": "", "markdown": str(markdown or ""), "snippet": str(markdown or "")}]
