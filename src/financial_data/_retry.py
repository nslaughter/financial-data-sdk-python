"""Retry decisions, backoff, `Retry-After`, and the bounds on retrying.

Every function here is pure. The clock and the random source are passed
in, so the rules can be tested directly and deterministically.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

import httpx

from ._config import RetryPolicy

# What is retried

_RETRIED_STATUSES: Final[dict[int, str]] = {
    429: "the request was throttled",
    500: "the server failed",
    502: "the server failed",
    503: "the server failed",
    504: "the server failed",
}


def status_retry_reason(status: int) -> str | None:
    """Say why a response's status is retried, or return `None` if it is not.

    `429`, `500`, `502`, `503`, and `504` are retried. Every other status
    would repeat.
    """
    return _RETRIED_STATUSES.get(status)


def error_retry_reason(error: BaseException, *, deadline: bool) -> str | None:
    """Say why an exception from httpx is retried, or return `None` if it is not.

    `deadline` says whether the call has a deadline. While it has one, an
    httpx timeout means the deadline has passed, which is not retried;
    without one, the timeout is a caller-supplied client's own, which is.
    httpx raises `RemoteProtocolError` both for a connection closed without
    a response and for a response it could not parse, so both are retried.
    A request httpx or a proxy refused to send would fail the same way
    again, and is not.
    """
    if isinstance(error, httpx.TimeoutException):
        return None if deadline else "the HTTP client's own timeout expired"
    if isinstance(error, httpx.ConnectError):
        return "connecting failed"
    if isinstance(error, httpx.ReadError | httpx.WriteError):
        return "the connection broke"
    if isinstance(error, httpx.CloseError):
        return "closing the connection failed"
    if isinstance(error, httpx.NetworkError):
        return "the connection failed"
    if isinstance(error, httpx.RemoteProtocolError):
        return "the server sent no complete response, or one that does not parse"
    return None


# Waiting


def backoff(policy: RetryPolicy, retry: int, random: Callable[[], float]) -> float:
    """Return the backoff before retry `retry`, which is 1 before the second attempt.

    It is `min(max_delay, base_delay * 2**(retry - 1))` seconds, and with
    jitter, `random()`, a number from 0 up to 1, times that.
    """
    delay = min(policy.max_delay, _doubled(policy.base_delay, retry - 1))
    return random() * delay if policy.jitter else delay


def _doubled(seconds: float, times: int) -> float:
    """Return `seconds * 2**times`, or infinity if no float holds it."""
    try:
        return math.ldexp(seconds, times)
    except OverflowError:  # a retry far past where the cap applies
        return math.inf


_DAY: Final = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
_LONG_DAY: Final = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
_MONTH: Final = r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_TIME: Final = r"(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
_MONTHS: Final = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)  # fmt: skip

# The three forms of an HTTP date (RFC 9110, section 5.6.7), and
# delay-seconds. `[0-9]` rather than `\d`, which matches every Unicode digit.
_SECONDS: Final = re.compile(r"[0-9]+")
_IMF_FIXDATE: Final = re.compile(
    rf"{_DAY}, (?P<day>[0-9]{{2}}) {_MONTH} (?P<year>[0-9]{{4}}) {_TIME} GMT"
)
_RFC_850: Final = re.compile(
    rf"{_LONG_DAY}, (?P<day>[0-9]{{2}})-{_MONTH}-(?P<year>[0-9]{{2}}) {_TIME} GMT"
)
_ASCTIME: Final = re.compile(
    rf"{_DAY} {_MONTH} (?P<day>[0-9]{{2}}| [0-9]) {_TIME} (?P<year>[0-9]{{4}})"
)


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """Return the seconds a `Retry-After` value asks for, or `None` if invalid.

    The value is a non-negative integer of seconds, or an HTTP date in any
    of its three forms, which is compared with `now`, an aware `datetime`.
    A date in the past asks for no wait. Seconds too many for a float ask
    for infinity, which no bound allows.
    """
    if value is None:
        return None
    text = value.strip(" \t")
    if _SECONDS.fullmatch(text):
        return float(text)
    moment = _http_date(text, now)
    if moment is None:
        return None
    return max(0.0, (moment - now).total_seconds())


def _http_date(text: str, now: datetime) -> datetime | None:
    """Return an HTTP date as an aware `datetime` in UTC, or `None` if invalid.

    The day name is checked for its form only, as most HTTP libraries do.
    """
    match = (
        _IMF_FIXDATE.fullmatch(text)
        or _ASCTIME.fullmatch(text)
        or _RFC_850.fullmatch(text)
    )
    if match is None:
        return None
    year = int(match["year"])
    if len(match["year"]) == 2:
        # RFC 850's two-digit year: a date that appears to be more than 50
        # years in the future is in the most recent past year with the same
        # last two digits.
        year += now.year - now.year % 100
        if year > now.year + 50:
            year -= 100
    try:
        return datetime(
            year,
            _MONTHS.index(match["month"]) + 1,
            int(match["day"]),
            int(match["hour"]),
            int(match["minute"]),
            int(match["second"]),
            tzinfo=UTC,
        )
    except ValueError:  # an impossible date or time, such as February 30
        return None


# Bounds


def wait_before_retry(
    policy: RetryPolicy,
    *,
    attempts: int,
    retry_after: float | None,
    waited: float,
    now: float,
    deadline: float | None,
    random: Callable[[], float],
) -> float | None:
    """Return the seconds to wait before the next attempt, or `None` to stop.

    `attempts` is how many attempts the call has made, `retry_after` the
    wait the last response's `Retry-After` asked for, or `None`, and
    `waited` the seconds the call has waited so far. `now` and `deadline`
    are on the same monotonic clock, and `deadline` is `None` when the call
    has none. A `Retry-After` is used as given, without jitter and without
    the backoff cap.

    The call stops once it has made `max_attempts` attempts, when the next
    wait would bring its total waiting above `max_retry_wait`, or when the
    next wait would end at or after its deadline.
    """
    if attempts >= policy.max_attempts:
        return None
    wait = backoff(policy, attempts, random) if retry_after is None else retry_after
    if waited + wait > policy.max_retry_wait:
        return None
    if deadline is not None and now + wait >= deadline:
        return None
    return wait
