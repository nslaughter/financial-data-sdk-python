"""Retry decisions, backoff, `Retry-After`, and the bounds on retrying."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import h11
import httpx
import pytest

from financial_data import RetryPolicy
from financial_data._retry import (
    backoff,
    error_retry_reason,
    parse_retry_after,
    status_retry_reason,
    wait_before_retry,
)

NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
MAX = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)
NO_JITTER = RetryPolicy(jitter=False)


def no_random() -> float:
    raise AssertionError("the random source was used")


# What is retried


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_statuses_are_retried(status: int) -> None:
    assert status_retry_reason(status)


@pytest.mark.parametrize(
    "status",
    [
        *(100, 101, 200, 204, 301, 302, 304, 400, 401, 403, 404, 410, 418, 499),
        *(501, 505, 507, 511, 599, 600, 999),
    ],
)
def test_other_statuses_are_not_retried(status: int) -> None:
    assert status_retry_reason(status) is None


def request() -> httpx.Request:
    return httpx.Request("GET", "http://localhost:8080/v1/meta")


RETRIED_ERRORS: list[type[httpx.TransportError]] = [
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.CloseError,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
]

TIMEOUTS: list[type[httpx.TimeoutException]] = [
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.TimeoutException,
]

REFUSED_ERRORS: list[type[httpx.HTTPError]] = [
    httpx.LocalProtocolError,
    httpx.UnsupportedProtocol,
    httpx.ProxyError,
    httpx.ProtocolError,
    httpx.TransportError,
    httpx.DecodingError,
    httpx.TooManyRedirects,
    httpx.RequestError,
]


@pytest.mark.parametrize("deadline", [True, False])
@pytest.mark.parametrize("error", RETRIED_ERRORS)
def test_connection_failures_are_retried(
    error: type[httpx.TransportError], deadline: bool
) -> None:
    assert error_retry_reason(error("failed", request=request()), deadline=deadline)


def test_a_malformed_response_is_retried_like_a_dropped_connection() -> None:
    # httpx raises the same class for a header line h11 could not parse.
    cause = h11.RemoteProtocolError("illegal header line")
    error = httpx.RemoteProtocolError(str(cause), request=request())
    error.__cause__ = cause
    assert error_retry_reason(error, deadline=True)


@pytest.mark.parametrize("error", TIMEOUTS)
def test_a_timeout_is_retried_only_without_a_deadline(
    error: type[httpx.TimeoutException],
) -> None:
    raised = error("timed out", request=request())
    assert error_retry_reason(raised, deadline=False)
    assert error_retry_reason(raised, deadline=True) is None


@pytest.mark.parametrize("deadline", [True, False])
@pytest.mark.parametrize("error", REFUSED_ERRORS)
def test_requests_httpx_refuses_and_other_errors_are_not_retried(
    error: type[httpx.HTTPError], deadline: bool
) -> None:
    assert error_retry_reason(error("refused"), deadline=deadline) is None


@pytest.mark.parametrize("error", [ValueError("x"), RuntimeError("closed")])
def test_exceptions_not_from_httpx_are_not_retried(error: Exception) -> None:
    assert error_retry_reason(error, deadline=False) is None


# Backoff


def test_backoff_doubles_up_to_the_cap() -> None:
    waits = [backoff(NO_JITTER, retry, no_random) for retry in range(1, 9)]
    assert waits == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0, 8.0, 8.0]


def test_backoff_of_the_scenario_profile() -> None:
    policy = RetryPolicy(base_delay=0.1, max_delay=0.4, jitter=False)
    assert [backoff(policy, retry, no_random) for retry in (1, 2, 3, 4)] == [
        0.1,
        0.2,
        0.4,
        0.4,
    ]


@pytest.mark.parametrize("retry", [2_100, 10_000, 10**30])
def test_backoff_far_past_the_cap_stays_at_it(retry: int) -> None:
    assert backoff(NO_JITTER, retry, no_random) == 8.0
    tiny = RetryPolicy(base_delay=5e-324, max_delay=1e300, jitter=False)
    assert backoff(tiny, retry, no_random) == 1e300


def test_backoff_without_delay_is_zero() -> None:
    policy = RetryPolicy(base_delay=0.0, max_delay=0.0, jitter=False)
    assert backoff(policy, 10**30, no_random) == 0.0


@pytest.mark.parametrize("fraction", [0.0, 0.25, 0.5, 0.999999])
@pytest.mark.parametrize(("retry", "ceiling"), [(1, 0.5), (3, 2.0), (6, 8.0)])
def test_jitter_draws_from_the_random_source_within_the_backoff(
    fraction: float, retry: int, ceiling: float
) -> None:
    draws: list[float] = []

    def random() -> float:
        draws.append(fraction)
        return fraction

    wait = backoff(RetryPolicy(), retry, random)
    assert draws == [fraction]
    assert wait == fraction * ceiling
    assert 0.0 <= wait < ceiling


# Retry-After


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("0", 0.0),
        ("1", 1.0),
        ("30", 30.0),
        ("007", 7.0),
        (" 2\t", 2.0),
        ("86400", 86400.0),
    ],
)
def test_retry_after_in_seconds(value: str, seconds: float) -> None:
    assert parse_retry_after(value, NOW) == seconds


def test_retry_after_too_long_for_a_float_asks_for_infinity() -> None:
    assert parse_retry_after("9" * 400, NOW) == math.inf
    assert parse_retry_after("9" * 5_000, NOW) == math.inf


@pytest.mark.parametrize(
    "value",
    [
        "Tue, 06 Oct 2026 12:00:05 GMT",  # IMF-fixdate
        "Tuesday, 06-Oct-26 12:00:05 GMT",  # RFC 850
        "Tue Oct  6 12:00:05 2026",  # asctime
    ],
)
def test_retry_after_as_an_http_date_in_each_form(value: str) -> None:
    assert parse_retry_after(value, NOW) == 5.0
    later = NOW + timedelta(seconds=1.5)
    assert parse_retry_after(value, later) == 3.5


def test_the_day_name_is_checked_only_for_its_form() -> None:
    assert parse_retry_after("Mon, 06 Oct 2026 12:00:05 GMT", NOW) == 5.0


def test_asctime_with_a_two_digit_day() -> None:
    value = "Sat Oct 10 12:00:00 2026"
    assert parse_retry_after(value, NOW) == 4 * 86400.0


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("Tue, 06 Oct 2026 23:59:60 GMT", 12 * 3600.0),
        ("Tuesday, 06-Oct-26 23:59:60 GMT", 12 * 3600.0),
        ("Tue Oct  6 23:59:60 2026", 12 * 3600.0),
        ("Tue, 06 Oct 2026 12:00:60 GMT", 60.0),
        ("Tue, 06 Oct 2026 11:59:60 GMT", 0.0),
        ("Fri, 31 Dec 9999 23:59:60 GMT", (MAX - NOW).total_seconds() + 1.0),
    ],
)
def test_a_leap_second_is_the_second_after_second_59(
    value: str, seconds: float
) -> None:
    assert parse_retry_after(value, NOW) == seconds


@pytest.mark.parametrize(
    "value",
    [
        "Tue, 06 Oct 2026 11:59:59 GMT",
        "Tue, 06 Oct 2026 12:00:00 GMT",
        "Thu, 01 Jan 1970 00:00:00 GMT",
        "Sunday, 06-Nov-94 08:49:37 GMT",
        "Sun Nov  6 08:49:37 1994",
    ],
)
def test_a_date_in_the_past_asks_for_no_wait(value: str) -> None:
    assert parse_retry_after(value, NOW) == 0.0


@pytest.mark.parametrize(
    ("value", "year"),
    [
        ("Tuesday, 06-Oct-26 12:00:05 GMT", 2026),
        ("Tuesday, 06-Oct-76 12:00:05 GMT", 2076),
        ("Tuesday, 06-Oct-77 12:00:05 GMT", 1977),
        ("Tuesday, 06-Oct-99 12:00:05 GMT", 1999),
        ("Tuesday, 06-Oct-00 12:00:05 GMT", 2000),
    ],
)
def test_an_rfc_850_year_more_than_50_years_ahead_is_in_the_past(
    value: str, year: int
) -> None:
    moment = datetime(year, 10, 6, 12, 0, 5, tzinfo=UTC)
    assert parse_retry_after(value, NOW) == max(0.0, (moment - NOW).total_seconds())


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "soon",
        "-1",
        "+1",
        "1.5",
        "1e3",
        "0x10",
        "1 2",
        "\u0661",  # an Arabic-Indic digit, which \d would match
        "2\n",
        "Tue, 06 Oct 2026 12:00:05 UTC",
        "Tue, 06 Oct 2026 12:00:05 gmt",
        "tue, 06 Oct 2026 12:00:05 GMT",
        "Tue, 06 oct 2026 12:00:05 GMT",
        "Tue, 6 Oct 2026 12:00:05 GMT",
        "Tue, 06 Oct 26 12:00:05 GMT",
        "Tue, 06 Oct 2026 12:00 GMT",
        "Tue 06 Oct 2026 12:00:05 GMT",
        "Tue, 30 Feb 2026 12:00:05 GMT",
        "Tue, 06 Oct 2026 24:00:00 GMT",
        "Tue, 06 Oct 2026 23:59:61 GMT",
        "Tue, 06 Oct 0000 12:00:05 GMT",
        "Tue, 06 Oct 2026 12:00:05 GMT trailing",
        "Tues, 06 Oct 2026 12:00:05 GMT",
        "Tue, 06-Oct-26 12:00:05 GMT",
        "Tuesday, 06-Oct-2026 12:00:05 GMT",
        "Tue Oct 6 12:00:05 2026",
        "Tue Oct  6 12:00:05 26",
        "2026-10-06T12:00:05Z",
    ],
)
def test_an_invalid_retry_after_is_ignored(value: str) -> None:
    assert parse_retry_after(value, NOW) is None


def test_no_retry_after() -> None:
    assert parse_retry_after(None, NOW) is None


# Bounds


def plan(
    policy: RetryPolicy = NO_JITTER,
    *,
    attempts: int = 1,
    retry_after: float | None = None,
    waited: float = 0.0,
    now: float = 100.0,
    deadline: float | None = None,
    random: Callable[[], float] = no_random,
) -> float | None:
    return wait_before_retry(
        policy,
        attempts=attempts,
        retry_after=retry_after,
        waited=waited,
        now=now,
        deadline=deadline,
        random=random,
    )


def test_backoff_is_waited_before_each_retry() -> None:
    assert [plan(attempts=attempts) for attempts in (1, 2, 3)] == [0.5, 1.0, 2.0]


def test_retrying_stops_after_max_attempts() -> None:
    assert plan(attempts=4) is None
    assert plan(RetryPolicy(max_attempts=1, jitter=False), attempts=1) is None
    assert plan(RetryPolicy(max_attempts=5, jitter=False), attempts=4) == 4.0


def test_retry_after_is_used_as_given_without_jitter_or_the_cap() -> None:
    assert plan(RetryPolicy(), retry_after=20.0) == 20.0
    assert plan(RetryPolicy(), retry_after=0.0) == 0.0


def test_retrying_stops_when_the_wait_would_exceed_the_budget() -> None:
    assert plan(retry_after=30.0) == 30.0
    assert plan(retry_after=30.5) is None
    assert plan(retry_after=10.0, waited=20.0) == 10.0
    assert plan(retry_after=10.0, waited=20.5) is None
    assert plan(attempts=3, waited=28.0) == 2.0
    assert plan(attempts=3, waited=28.5) is None
    assert plan(retry_after=math.inf) is None


def test_retrying_stops_when_the_wait_would_end_at_or_after_the_deadline() -> None:
    assert plan(now=100.0, deadline=100.6) == 0.5
    assert plan(now=100.0, deadline=100.5) is None
    assert plan(now=100.0, deadline=100.4) is None
    assert plan(retry_after=2.0, now=100.0, deadline=101.0) is None
    assert plan(retry_after=2.0, now=100.0, deadline=102.5) == 2.0


def test_the_random_source_is_used_only_for_backoff() -> None:
    draws: list[float] = []

    def random() -> float:
        draws.append(0.5)
        return 0.5

    assert plan(RetryPolicy(), attempts=2, random=random) == 0.5
    assert draws == [0.5]
    assert plan(RetryPolicy(), retry_after=3.0, random=random) == 3.0
    assert plan(RetryPolicy(), attempts=4, random=random) is None
    assert draws == [0.5]
