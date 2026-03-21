"""HTTP retry helper with exponential backoff."""

import asyncio
import logging
from functools import wraps

import httpx

logger = logging.getLogger(__name__)

# Retryable HTTP status codes
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


async def http_request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    **kwargs,
) -> httpx.Response:
    """
    Make an HTTP request with exponential backoff retry.
    Retries on connection errors and 429/5xx status codes.
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code not in _RETRYABLE_STATUSES or attempt == max_retries:
                return resp
            logger.warning(
                f"HTTP {resp.status_code} from {url}, retry {attempt + 1}/{max_retries}"
            )
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
            last_exc = e
            if attempt == max_retries:
                raise
            logger.warning(
                f"HTTP error {type(e).__name__} for {url}, retry {attempt + 1}/{max_retries}"
            )

        await asyncio.sleep(base_delay * (2 ** attempt))

    raise last_exc  # unreachable, but satisfies type checker
