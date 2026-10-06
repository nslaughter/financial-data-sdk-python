"""The retry policy, and checking and resolving the client's settings.

Every check here raises `ConfigError` outside any `except` block, so the
error has neither `__cause__` nor `__context__`, and no message repeats the
value it refuses.
"""

from __future__ import annotations

import math
import os
import re
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from ._errors import ConfigError

API_KEY_ENV = "FINANCIAL_DATA_API_KEY"
BASE_URL_ENV = "FINANCIAL_DATA_BASE_URL"
DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_TIMEOUT = 60.0

# A base URL's authority: a host, bracketed if it is an IPv6 address, and an
# optional port. User information is refused before this applies.
_AUTHORITY = re.compile(r"(?:\[[0-9A-Fa-f:.]+\]|[^\[\]:@]+)(?::(?P<port>[0-9]*))?")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How a call retries a transient failure.

    The constructor raises `ConfigError` for an invalid value.
    """

    max_attempts: int = 4
    """Attempts per call, including the first. `1` turns retries off."""
    max_retry_wait: float = 30.0
    """Total seconds a call may spend waiting between attempts."""
    base_delay: float = 0.5
    """Seconds of backoff before the first retry."""
    max_delay: float = 8.0
    """The backoff cap in seconds. It does not limit `Retry-After`."""
    jitter: bool = True
    """Draw each backoff uniformly between 0 and its value."""

    def __post_init__(self) -> None:
        if not _is_int(self.max_attempts) or self.max_attempts < 1:
            raise ConfigError(
                "RetryPolicy.max_attempts must be an integer of at least 1"
            )
        for name in ("max_retry_wait", "base_delay", "max_delay"):
            seconds = _finite_seconds(getattr(self, name))
            if seconds is None or seconds < 0:
                raise ConfigError(
                    f"RetryPolicy.{name} must be a finite number of seconds, "
                    "not negative"
                )
        if self.base_delay > self.max_delay:
            raise ConfigError("RetryPolicy.base_delay must be at most max_delay")
        if not isinstance(self.jitter, bool):
            raise ConfigError("RetryPolicy.jitter must be a bool")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finite_seconds(value: object) -> float | None:
    """Return seconds as a float, or `None` if they are not a finite number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    # Converting an int beyond the largest float raises OverflowError.
    if isinstance(value, int) and abs(value) > sys.float_info.max:
        return None
    seconds = float(value)
    return seconds if math.isfinite(seconds) else None


def _env(name: str) -> str | None:
    """Read an environment variable, counting an empty value as unset."""
    return os.environ.get(name) or None


def resolve_api_key(api_key: object) -> str:
    """Return the key from `api_key`, or from the environment when it is `None`.

    A key is a non-empty string of visible ASCII characters, `!` to `~`. The
    characters are compared, never encoded, because an encoding error holds
    the whole value and would become the `ConfigError`'s `__context__`.
    """
    source = "api_key"
    if api_key is None:
        api_key = _env(API_KEY_ENV)
        source = API_KEY_ENV
        if api_key is None:
            raise ConfigError(f"no API key: pass api_key or set {API_KEY_ENV}")
    if not isinstance(api_key, str):
        raise ConfigError(f"api_key must be a str, not {type(api_key).__name__}")
    if not api_key or not all("!" <= char <= "~" for char in api_key):
        raise ConfigError(
            f"{source} must be a non-empty key of visible ASCII characters, "
            "without spaces or control characters"
        )
    return api_key


def resolve_base_url(base_url: object) -> str:
    """Return the base URL without a trailing `/`.

    It comes from `base_url`, or when that is `None` from the environment, or
    else the default. It must be an `http` or `https` URL with a host and an
    optional path, without a query or fragment.
    """
    source = "base_url"
    if base_url is None:
        base_url = _env(BASE_URL_ENV)
        source = BASE_URL_ENV
        if base_url is None:
            return DEFAULT_BASE_URL
    if not isinstance(base_url, str):
        raise ConfigError(f"base_url must be a str, not {type(base_url).__name__}")
    problem = _base_url_problem(base_url)
    if problem is not None:
        raise ConfigError(f"{source} must be an http or https URL: {problem}")
    return base_url.rstrip("/")


def _base_url_problem(url: str) -> str | None:
    """Say what is wrong with a base URL, or return `None` if nothing is."""
    if any(char.isspace() or not char.isprintable() for char in url):
        return "it has a space or a control character"
    if "?" in url:
        return "it has a query"
    if "#" in url:
        return "it has a fragment"
    try:
        parts = urlsplit(url)
    except ValueError:  # an unbalanced bracket around an IPv6 host
        parts = None
    if parts is None:
        return "its host is malformed"
    if parts.scheme not in ("http", "https"):
        return "its scheme is not http or https"
    if "@" in parts.netloc:
        return "it has user information before its host"
    authority = _AUTHORITY.fullmatch(parts.netloc)
    if authority is None:
        return "it has no host, or its host or port is malformed"
    port = authority["port"]
    if port and (len(port) > 5 or int(port) > 65535):
        return "its port is out of range"
    parsed = True
    try:
        httpx.URL(url)
    except httpx.InvalidURL:
        parsed = False
    if not parsed:
        return "httpx cannot parse it"
    return None


def check_timeout(timeout: object) -> float | None:
    """Return the deadline in seconds, or `None` for none."""
    if timeout is None:
        return None
    seconds = _finite_seconds(timeout)
    if seconds is None or seconds <= 0:
        raise ConfigError(
            "timeout must be a positive, finite number of seconds, or None"
        )
    return seconds


def check_retry(retry: object) -> RetryPolicy:
    """Return the retry policy, or the default policy for `None`."""
    if retry is None:
        return RetryPolicy()
    if not isinstance(retry, RetryPolicy):
        raise ConfigError(f"retry must be a RetryPolicy, not {type(retry).__name__}")
    return retry


def check_http_client(http_client: object) -> httpx.Client | None:
    """Return the caller's HTTP client, or `None` when there is none."""
    if http_client is None or isinstance(http_client, httpx.Client):
        return http_client
    raise ConfigError(
        f"http_client must be an httpx.Client, not {type(http_client).__name__}"
    )
