"""A small HTTP helper that respects the two APIs' rate limits.

football-data.org tells you, on every response, how many calls you have left in
the current minute (`X-Requests-Available-Minute`) and when the counter resets
(`X-RequestCounter-Reset`, seconds). The Odds API reports credits left in
`x-requests-remaining`. Reading those headers and sleeping is what the
football-data.org welcome email asks LLM-written clients to do.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class QuotaState:
    """What the last response told us about the remaining quota."""

    remaining: int | None = None
    used: int | None = None
    reset_seconds: int | None = None
    last_cost: int | None = None


@dataclass
class Session:
    """Thin wrapper around `requests.Session` with throttling and retries.

    `sleep` is injectable so tests can run without waiting.
    """

    user_agent: str = "sports-analyzer/0.1 (+https://github.com)"
    timeout: float = 30.0
    max_retries: int = 4
    min_interval: float = 0.0  # seconds between calls to the same host
    sleep: Callable[[float], None] = time.sleep
    quota: QuotaState = field(default_factory=QuotaState)
    _session: requests.Session = field(default_factory=requests.Session, repr=False)
    _last_call: float = field(default=0.0, repr=False)

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """GET `url` and return parsed JSON, honouring rate-limit headers."""
        merged_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if headers:
            merged_headers.update(headers)

        for attempt in range(self.max_retries + 1):
            self._respect_min_interval()
            response = self._session.get(
                url, params=params, headers=merged_headers, timeout=self.timeout
            )
            self._last_call = time.monotonic()
            self._read_quota_headers(response.headers)

            if response.status_code < 400:
                return response.json()

            if response.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                wait = self._backoff_seconds(response.headers, attempt)
                log.warning(
                    "HTTP %s from %s; waiting %.0fs before retry %d/%d",
                    response.status_code,
                    _redact(url),
                    wait,
                    attempt + 1,
                    self.max_retries,
                )
                self.sleep(wait)
                continue

            # Do not echo the response body: it could contain the token in an error URL.
            raise requests.HTTPError(
                f"HTTP {response.status_code} from {_redact(url)}", response=response
            )

        raise RuntimeError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ internals
    def _respect_min_interval(self) -> None:
        if self.min_interval <= 0 or self._last_call == 0.0:
            return
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            self.sleep(self.min_interval - elapsed)

    def _read_quota_headers(self, headers: Any) -> None:
        """Pick up whichever quota headers this API uses. Both are optional."""
        remaining = _int_header(headers, "X-Requests-Available-Minute")
        if remaining is None:
            remaining = _int_header(headers, "x-requests-remaining")
        reset = _int_header(headers, "X-RequestCounter-Reset")
        cost = _int_header(headers, "x-requests-last")
        used = _int_header(headers, "x-requests-used")
        if used is not None:
            self.quota.used = used
        if remaining is not None:
            self.quota.remaining = remaining
        if reset is not None:
            self.quota.reset_seconds = reset
        if cost is not None:
            self.quota.last_cost = cost
        # Pre-emptive throttle: if the per-minute allowance is exhausted, wait for the reset.
        if remaining == 0 and reset:
            log.info("Per-minute quota exhausted; sleeping %ss for reset", reset)
            self.sleep(reset + 1)

    def _backoff_seconds(self, headers: Any, attempt: int) -> float:
        retry_after = _int_header(headers, "Retry-After")
        if retry_after:
            return float(retry_after) + 1
        reset = _int_header(headers, "X-RequestCounter-Reset")
        if reset:
            return float(reset) + 1
        return float(2 ** (attempt + 1))


def _int_header(headers: Any, name: str) -> int | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _redact(url: str) -> str:
    """Strip query strings so an apiKey never ends up in a log line."""
    return url.split("?", 1)[0]
