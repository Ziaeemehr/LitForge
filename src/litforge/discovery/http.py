"""Polite HTTP client for scholarly APIs: per-host rate limits, retries, disk cache, cost tracking.

The transport is injectable so tests never touch the network.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from litforge import __version__

# (status, headers, body)
Response = tuple[int, Mapping[str, str], bytes]
Transport = Callable[[str, Mapping[str, str], float], Response]

DEFAULT_MIN_INTERVAL = {
    "export.arxiv.org": 3.0,  # arXiv API terms: one request every 3 seconds
    "arxiv.org": 3.0,  # PDF downloads are held to the same pace
    "api.semanticscholar.org": 1.1,
    "api.crossref.org": 0.2,
    "api.openalex.org": 0.12,
}
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RESPONSE_BYTES = 200 * 1024 * 1024  # stop reading endless or huge bodies (PDF downloads)


class HttpError(RuntimeError):
    def __init__(self, url: str, status: int, detail: str = "") -> None:
        super().__init__(f"HTTP {status} for {url} {detail}".strip())
        self.status = status


class BudgetExhausted(RuntimeError):
    """The API's remaining paid/free budget is too low to continue."""


def urllib_transport(url: str, headers: Mapping[str, str], timeout: float) -> Response:
    req = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https scholarly APIs
            body = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise HttpError(url, -1, f"response larger than {MAX_RESPONSE_BYTES // 2**20} MB")
            return resp.status, {**dict(resp.headers.items()), "X-LitForge-Final-URL": resp.geturl()}, body
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read() or b""


@dataclass
class HttpClient:
    cache_dir: Path | None = None
    cache_ttl_s: float = 7 * 24 * 3600
    refresh: bool = False
    timeout: float = 30.0
    max_retries: int = 4
    transport: Transport = urllib_transport
    sleep: Callable[[float], None] = time.sleep
    min_interval: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_MIN_INTERVAL))
    user_agent: str = f"LitForge/{__version__} (local research tool)"
    requests_made: int = 0
    cache_hits: int = 0
    cost_usd: float = 0.0
    remaining_usd: dict[str, float] = field(default_factory=dict)
    _last_request: dict[str, float] = field(default_factory=dict)

    def url(self, base: str, params: Mapping[str, Any] | None = None) -> str:
        if not params:
            return base
        clean = {k: v for k, v in params.items() if v is not None}
        return f"{base}?{urllib.parse.urlencode(clean, safe=':,|')}"

    def _cache_path(self, url: str) -> Path | None:
        if self.cache_dir is None:
            return None
        # Secrets in query strings must not end up in cache keys on disk.
        parsed = urllib.parse.urlsplit(url)
        query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query) if k not in ("api_key", "mailto", "email")]
        key = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(sorted(query))))
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.cache_dir / parsed.netloc / f"{digest}.json"

    def _throttle(self, host: str) -> None:
        interval = self.min_interval.get(host, 0.2)
        last = self._last_request.get(host)
        if last is not None:
            wait = interval - (time.monotonic() - last)
            if wait > 0:
                self.sleep(wait)
        self._last_request[host] = time.monotonic()

    def get_bytes(self, url: str, headers: Mapping[str, str] | None = None) -> bytes:
        return self.get_bytes_with_url(url, headers)[0]

    def get_bytes_with_url(self, url: str, headers: Mapping[str, str] | None = None) -> tuple[bytes, str]:
        """Return the response body and final URL after redirects."""
        cache = self._cache_path(url)
        if cache is not None and cache.exists() and not self.refresh:
            entry = json.loads(cache.read_text())
            if time.time() - entry["fetched_at"] < self.cache_ttl_s:
                self.cache_hits += 1
                return bytes(entry["body"], "utf-8"), entry.get("response_url", url)
        host = urllib.parse.urlsplit(url).netloc
        hdrs = {"User-Agent": self.user_agent, "Accept": "application/json", **(headers or {})}
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            self._throttle(host)
            status, resp_headers, body = self.transport(url, hdrs, self.timeout)
            self.requests_made += 1
            self._track_cost(host, resp_headers)
            if status == 200:
                response_url = _header(resp_headers, "X-LitForge-Final-URL") or url
                if cache is not None:
                    parsed = urllib.parse.urlsplit(response_url)
                    query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query)
                             if k not in ("api_key", "mailto", "email")]
                    cached_url = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps({"url": url.split("api_key=")[0], "fetched_at": time.time(),
                                                 "body": body.decode("utf-8", errors="replace"),
                                                 "response_url": cached_url}))
                return body, response_url
            if status in RETRY_STATUSES and attempt < self.max_retries:
                retry_after = _header(resp_headers, "Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                if wait > 120:  # e.g. daily budget reset hours away: give up instead of hanging
                    raise BudgetExhausted(f"{host} asks to retry after {wait:.0f}s")
                self.sleep(wait)
                delay *= 2
                continue
            raise HttpError(url.split("api_key=")[0], status, body[:200].decode("utf-8", errors="replace"))
        raise HttpError(url, -1, "retries exhausted")

    def get_json(self, url: str, headers: Mapping[str, str] | None = None) -> Any:
        return json.loads(self.get_bytes(url, headers))

    def _track_cost(self, host: str, headers: Mapping[str, str]) -> None:
        cost = _header(headers, "X-RateLimit-Cost-USD")
        remaining = _header(headers, "X-RateLimit-Remaining-USD")
        if cost:
            self.cost_usd += float(cost)
        if remaining:
            self.remaining_usd[host] = float(remaining)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None
