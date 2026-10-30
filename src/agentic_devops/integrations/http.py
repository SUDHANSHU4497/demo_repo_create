"""Small HTTP wrapper: timeouts, retries with backoff on 429/5xx, and readable errors."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any

import requests

log = logging.getLogger(__name__)

RETRYABLE = {429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str):
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:500]}")
        self.method = method
        self.url = url
        self.status = status
        self.body = body


class HttpClient:
    def __init__(
        self,
        auth_header: Callable[[], str],
        *,
        timeout: float = 60.0,
        max_attempts: int = 5,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        user_agent: str = "agentic-devops/0.1",
    ):
        self.auth_header = auth_header
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.session = session or requests.Session()
        self.sleep = sleep
        self.user_agent = user_agent

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        allow_404: bool = False,
        raw: bool = False,
    ) -> Any:
        hdrs = {
            "Authorization": self.auth_header(),
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if json is not None:
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.session.request(
                    method, url, params=params, json=json, data=data, headers=hdrs, timeout=self.timeout
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= self.max_attempts:
                    raise ApiError(method, url, 0, repr(exc)) from exc
                self._backoff(attempt, None)
                continue
            if resp.status_code in RETRYABLE and attempt < self.max_attempts:
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            if allow_404 and resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                raise ApiError(method, url, resp.status_code, resp.text)
            if raw:
                return resp.content
            if not resp.content:
                return None
            ctype = resp.headers.get("Content-Type", "")
            return resp.json() if "json" in ctype else resp.content

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = None
        if delay is None:
            delay = min(2**attempt, 30) + random.uniform(0, 1)
        log.warning("Retrying HTTP request in %.1fs (attempt %d)", delay, attempt)
        self.sleep(delay)
