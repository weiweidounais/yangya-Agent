"""Public errors contain only fixed messages, never transport exceptions."""


class CrawlError(Exception):
    status = "error"


class PolicyDenied(CrawlError):
    status = "disabled_by_policy"


class NotConfigured(CrawlError):
    status = "not_configured"
