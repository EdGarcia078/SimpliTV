"""HTTP request hardening helpers shared by middleware and authentication."""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Request

from app.core.config import settings

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _trusted_proxy_networks():
    networks = []
    for raw in settings.TRUSTED_PROXIES.split(","):
        value = raw.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            # Invalid operator configuration is ignored rather than weakening the
            # trust boundary by treating it as a wildcard.
            continue
    return tuple(networks)


def is_trusted_proxy(peer_host: str | None) -> bool:
    if not peer_host:
        return False
    try:
        peer = ipaddress.ip_address(peer_host)
    except ValueError:
        return False
    return any(peer in network for network in _trusted_proxy_networks())


def _valid_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def get_client_ip(request: Request) -> str:
    """Return the client IP using a right-to-left trusted-proxy chain.

    A client can prepend arbitrary values to ``X-Forwarded-For``. Therefore the
    leftmost value is never trusted blindly. Starting at the TCP peer, configured
    trusted proxy hops are removed from the right and the first untrusted address
    becomes the client IP.
    """
    peer = _valid_ip(request.client.host if request.client else None)
    if peer is None:
        return request.client.host if request.client else "unknown"
    if not is_trusted_proxy(peer):
        return peer

    forwarded = request.headers.get("x-forwarded-for") or ""
    chain = [ip for part in forwarded.split(",") if (ip := _valid_ip(part))]
    chain.append(peer)
    for candidate in reversed(chain):
        if is_trusted_proxy(candidate):
            continue
        return candidate
    return peer


def request_origin_is_same_host(request: Request, origin: str) -> bool:
    """Compare a browser Origin/Referer against the actual Host header."""
    if not origin or origin == "null":
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False

    request_host = (request.headers.get("host") or "").strip().lower()
    return bool(request_host) and parsed.netloc.lower() == request_host


def is_cross_site_browser_request(request: Request) -> bool:
    """Detect browser-originated unsafe requests that do not target this host."""
    if request.method.upper() not in UNSAFE_METHODS:
        return False

    fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if fetch_site == "cross-site":
        return True

    origin = request.headers.get("origin")
    if origin is not None:
        return not request_origin_is_same_host(request, origin)

    referer = request.headers.get("referer")
    if referer:
        return not request_origin_is_same_host(request, referer)

    # Native clients/curl may legitimately omit browser origin headers. Cookie
    # SameSite plus the checks above still block normal cross-site browser CSRF.
    return False


@dataclass
class _FailureBucket:
    failures: deque[float]
    locked_until: float = 0.0


class LoginRateLimiter:
    """Small in-memory brute-force limiter for the login endpoint.

    It deliberately stores no passwords or session tokens. Pair buckets prevent
    repeated guessing of one account, while a higher IP threshold prevents
    cycling through many usernames from the same source.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pair: dict[str, _FailureBucket] = {}
        self._ip: dict[str, _FailureBucket] = {}

    @staticmethod
    def _bucket(mapping: dict[str, _FailureBucket], key: str) -> _FailureBucket:
        bucket = mapping.get(key)
        if bucket is None:
            bucket = _FailureBucket(deque())
            mapping[key] = bucket
        return bucket

    @staticmethod
    def _prune(bucket: _FailureBucket, now: float) -> None:
        cutoff = now - settings.LOGIN_WINDOW_SECONDS
        while bucket.failures and bucket.failures[0] < cutoff:
            bucket.failures.popleft()
        if bucket.locked_until <= now:
            bucket.locked_until = 0.0

    def _state(self, mapping, key: str, max_failures: int, now: float) -> tuple[bool, int]:
        bucket = self._bucket(mapping, key)
        self._prune(bucket, now)
        if bucket.locked_until > now:
            return True, max(1, int(bucket.locked_until - now))
        if len(bucket.failures) >= max_failures:
            bucket.locked_until = now + settings.LOGIN_LOCKOUT_SECONDS
            return True, settings.LOGIN_LOCKOUT_SECONDS
        return False, 0

    def check(self, client_ip: str, username: str) -> tuple[bool, int]:
        now = time.monotonic()
        user_key = username.strip().casefold() or "<empty>"
        pair_key = f"{client_ip}\0{user_key}"
        with self._lock:
            locked, retry = self._state(
                self._pair, pair_key, settings.LOGIN_MAX_FAILURES, now
            )
            if locked:
                return False, retry
            locked, retry = self._state(
                self._ip, client_ip, settings.LOGIN_IP_MAX_FAILURES, now
            )
            if locked:
                return False, retry
        return True, 0

    def record_failure(self, client_ip: str, username: str) -> None:
        now = time.monotonic()
        user_key = username.strip().casefold() or "<empty>"
        pair_key = f"{client_ip}\0{user_key}"
        with self._lock:
            for mapping, key, limit in (
                (self._pair, pair_key, settings.LOGIN_MAX_FAILURES),
                (self._ip, client_ip, settings.LOGIN_IP_MAX_FAILURES),
            ):
                bucket = self._bucket(mapping, key)
                self._prune(bucket, now)
                bucket.failures.append(now)
                if len(bucket.failures) >= limit:
                    bucket.locked_until = now + settings.LOGIN_LOCKOUT_SECONDS

    def record_success(self, client_ip: str, username: str) -> None:
        user_key = username.strip().casefold() or "<empty>"
        pair_key = f"{client_ip}\0{user_key}"
        with self._lock:
            self._pair.pop(pair_key, None)

    def reset(self) -> None:
        """Test/support helper; no persistent security state is discarded."""
        with self._lock:
            self._pair.clear()
            self._ip.clear()


login_rate_limiter = LoginRateLimiter()
