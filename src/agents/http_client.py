"""The outbound HTTP policy the clients of ``agents`` share: one place builds their client."""

from __future__ import annotations

import http.cookiejar
import ssl

import httpx2


def policy_client(
    *,
    timeout: httpx2.Timeout | float | None,
    base_url: str = "",
    transport: httpx2.AsyncBaseTransport | None = None,
    verify: ssl.SSLContext | bool = True,
    auth: httpx2.Auth | None = None,
) -> httpx2.AsyncClient:
    """A client that reads no proxy variable, follows no redirect and keeps no cookie.

    The peer is an address the host chose, so a proxy variable of the environment, a ``Location``
    that moves a request or a cookie a peer sets must not change where a request goes or what it
    carries. The jar allows no domain, so a pooled client stores and sends no cookie across
    requests.
    """
    return httpx2.AsyncClient(
        base_url=base_url,
        transport=transport,
        verify=verify,
        auth=auth,
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
        cookies=http.cookiejar.CookieJar(
            policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
        ),
    )
