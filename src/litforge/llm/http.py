"""Keep local model requests off environment-configured proxies."""
from __future__ import annotations

import ipaddress
import urllib.request
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit


def open_url(request: str | urllib.request.Request, *, timeout: float) -> Any:
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    host = (urlsplit(url).hostname or "").lower()
    loopback = host == "localhost"
    with suppress(ValueError):
        loopback = loopback or ipaddress.ip_address(host).is_loopback
    if loopback:
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - configured provider
