"""Healthchecks.io style pings. Failures never propagate."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


async def ping(base_url: str, kind: str = "success", message: str | None = None) -> None:
    """kind: 'start', 'success' or 'fail'. An empty URL is a no-op."""
    if not base_url:
        return
    url = base_url.rstrip("/")
    if kind in ("start", "fail"):
        url = f"{url}/{kind}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if message:
                await client.post(url, content=message[:5000].encode("utf-8"))
            else:
                await client.get(url)
    except Exception as exc:  # noqa: BLE001
        log.warning("healthcheck ping failed (%s): %s", kind, exc)
