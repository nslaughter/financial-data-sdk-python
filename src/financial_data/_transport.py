"""One call over httpx: attempts, waits, the deadline, redaction, and logging.

A call sends attempts until one succeeds or the retry policy stops it, and
raises the last attempt's exception. Each attempt reads its response as a
stream and checks the deadline when the headers arrive and after each chunk
of the body, because httpx's timeouts bound each socket operation, not the
whole response.

No exception the SDK raises holds the key. On catching one of httpx's, the
SDK removes the key from it and from every exception chained to it before
chaining it. It never chains an `httpx.HTTPStatusError` or an exception from
parsing or validating a body, which hold the response or its body: it
raises outside the `except` block instead. An exception the caller's own
code raises, such as one from an event hook, propagates unchanged.
"""

from __future__ import annotations

import logging
import platform
import random as _random
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, TypeVar

import httpx

from ._config import RetryPolicy
from ._decode import InvalidResponse, decode_object
from ._errors import (
    REDACTED,
    ClientClosedError,
    FinancialDataError,
    api_error,
    deadline_exceeded,
    redact,
    transport_error,
    unexpected_response,
)
from ._params import Target
from ._retry import (
    error_retry_reason,
    parse_retry_after,
    status_retry_reason,
    wait_before_retry,
)
from ._version import __version__

logger: Final = logging.getLogger("financial_data")

USER_AGENT: Final = (
    f"financial-data-sdk-python/{__version__} python/{platform.python_version()}"
)

_METHOD: Final = "GET"
_REDACTED_AUTHORIZATION: Final = f"Bearer {REDACTED}"

_T = TypeVar("_T")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Pool:
    """The HTTP client that a `Client` and the clients derived from it share.

    The SDK creates its own `httpx.Client` at the first request, so that a
    `Client` does no I/O until a call, and closes it in `close()`. It never
    closes a client the caller supplied, and a call through one the caller
    has closed raises `ClientClosedError`.

    A derived client uses a view of its source's pool, from `derive()`.
    Closing a view closes nothing but the view, and a call through one
    raises `ClientClosedError` once it, or any pool it was derived from, is
    closed.
    """

    def __init__(
        self, supplied: httpx.Client | None, *, source: Pool | None = None
    ) -> None:
        self._supplied = supplied
        self._source = source
        self._own: httpx.Client | None = None
        self._closed = False
        self._lock = threading.Lock()

    def derive(self) -> Pool:
        """Return a view of this pool, for a derived client."""
        return Pool(self._supplied, source=self)

    def get(self) -> httpx.Client:
        """Return the HTTP client for the next request."""
        # Each view up to the pool that is not one, in a loop rather than by
        # recursion, so that a client derived from a derived client, however
        # many times over, cannot exceed the recursion limit.
        pool = self
        while pool._source is not None:
            if pool._closed:
                raise ClientClosedError("the client is closed")
            pool = pool._source
        return pool._client()

    def _client(self) -> httpx.Client:
        """Return this pool's HTTP client, creating the SDK's own if needed."""
        with self._lock:
            if self._closed:
                raise ClientClosedError("the client is closed")
            if self._supplied is not None:
                if self._supplied.is_closed:
                    raise ClientClosedError(
                        "the HTTP client given as http_client is closed"
                    )
                return self._supplied
            if self._own is None:
                # No timeouts of its own, because the deadline sets them on
                # each attempt, and no redirects.
                self._own = httpx.Client(timeout=None, follow_redirects=False)
            return self._own

    def close(self) -> None:
        """Close the HTTP client the SDK created, if this pool is not a view.

        Closing twice does nothing.
        """
        with self._lock:
            self._closed = True
            own, self._own = self._own, None
        if own is not None:
            own.close()


class _Bearer(httpx.Auth):
    """Sends the SDK's key, replacing any `auth` a caller's HTTP client has."""

    def __init__(self, api_key: str) -> None:
        self._authorization = f"Bearer {api_key}"

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = self._authorization
        yield request


@dataclass(frozen=True, slots=True)
class _Deadline:
    """A call's deadline: its timeout in seconds, and when it passes."""

    timeout: float
    at: float
    """The instant on the transport's monotonic clock."""


@dataclass(slots=True)
class _Received:
    """An attempt that received a response."""

    status: int
    headers: httpx.Headers
    body: bytes | None = None
    """The whole body, or `None` if the SDK did not read it all."""
    late: bool = False
    """Whether the deadline passed while the response arrived."""


@dataclass(frozen=True, slots=True)
class _Raised:
    """An attempt in which httpx raised, with the key removed from its exception."""

    error: httpx.TransportError | httpx.DecodingError
    headers: httpx.Headers | None = None
    """The response's headers, if it arrived before httpx raised."""
    status: int | None = None


_Outcome = _Received | _Raised


def _summary(outcome: _Outcome) -> str:
    """Say how an attempt ended: its status or the exception's class."""
    if isinstance(outcome, _Raised):
        return type(outcome.error).__name__
    if outcome.late:
        return f"status {outcome.status}, then the deadline passed"
    return f"status {outcome.status}"


@dataclass(frozen=True, slots=True)
class _Failure:
    """An attempt that failed: what to raise, and whether to retry."""

    error: FinancialDataError
    cause: httpx.HTTPError | None
    """httpx's exception, chained as the error's `__cause__`, or `None`."""
    retry: str | None
    """Why the attempt is retried, or `None` if it is not."""
    retry_after: float | None = None


class Transport:
    """Sends the calls of one `Client`, with its key, deadline, and retry policy.

    `clock` is a monotonic clock in seconds and `sleep` waits on it,
    `random` returns a number from 0 up to 1 for jitter, and `now` returns
    the aware time in UTC, which only a `Retry-After` date is compared with.
    Tests pass their own.
    """

    __slots__ = (
        "_auth",
        "_base_url",
        "_clock",
        "_headers",
        "_key",
        "_now",
        "_pool",
        "_random",
        "_retry",
        "_sleep",
        "_timeout",
    )

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout: float | None,
        retry: RetryPolicy,
        pool: Pool,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        random: Callable[[], float] = _random.random,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._key = api_key
        self._base_url = base_url
        self._timeout = timeout
        self._retry = retry
        self._pool = pool
        self._clock = clock
        self._sleep = sleep
        self._random = random
        self._now = now
        self._auth = _Bearer(api_key)
        self._headers = {"Accept": "application/json", "User-Agent": USER_AGENT}

    def derive(
        self, *, timeout: float | None, retry: RetryPolicy, pool: Pool
    ) -> Transport:
        """Return a transport with the same key, base URL, and clocks.

        The deadline and the retry policy are as `with_options` gives them,
        and `pool` is a view of this transport's pool.
        """
        return Transport(
            api_key=self._key,
            base_url=self._base_url,
            timeout=timeout,
            retry=retry,
            pool=pool,
            clock=self._clock,
            sleep=self._sleep,
            random=self._random,
            now=self._now,
        )

    def call(
        self,
        target: Target,
        decode: Callable[[dict[str, Any]], _T],
        check: Callable[[_T], str | None] | None = None,
    ) -> _T:
        """Send one call, retrying as the policy allows, and decode its response.

        `decode` turns the response's JSON object into what the call
        returns, and raises `InvalidResponse` for one without its documented
        form, which is not retried. `check`, if given, returns why a decoded
        page breaks a pagination guarantee, or `None` if it does not; a page
        that breaks one raises `UnexpectedResponseError` with `status`
        `None`, and is not retried either.
        """
        path = target.reported(self._key)
        url = self._base_url + target.path_with_query
        reported_url = _reported_url(self._base_url, path, self._key)
        began = self._clock()
        deadline = None
        if self._timeout is not None:
            deadline = _Deadline(self._timeout, began + self._timeout)
        attempts = 0
        waited = 0.0
        while True:
            http = self._pool.get()
            attempts += 1
            outcome = self._attempt(http, url, reported_url, deadline, began)
            request_id = (
                None if outcome.headers is None else outcome.headers.get("Request-Id")
            )
            fields = (
                _METHOD,
                path,
                attempts,
                _summary(outcome),
                self._clock() - began,
                None if request_id is None else redact(request_id, self._key),
            )
            logger.debug("%s %s: attempt %d, %s, %.3f s, request ID %s", *fields)
            if (
                isinstance(outcome, _Received)
                and outcome.body is not None
                and not outcome.late
                and 200 <= outcome.status <= 299
            ):
                return self._decode(
                    outcome, outcome.body, decode, check, request_id, path, attempts
                )
            failure = self._failure(outcome, deadline, request_id, path, attempts)
            wait = None
            if failure.retry is not None:
                wait = wait_before_retry(
                    self._retry,
                    attempts=attempts,
                    retry_after=failure.retry_after,
                    waited=waited,
                    now=self._clock(),
                    deadline=None if deadline is None else deadline.at,
                    random=self._random,
                )
            if wait is None:
                raise failure.error from failure.cause
            logger.info(
                "%s %s: attempt %d, %s, %.3f s, request ID %s; "
                "retrying in %.3f s because %s",
                *fields,
                wait,
                failure.retry,
            )
            self._sleep(wait)
            waited += wait
            began = self._clock()
            if deadline is not None and began >= deadline.at:
                # The wait was to end before the deadline, but a sleep can
                # overrun, and the call stops as if the wait had ended at it.
                raise failure.error from failure.cause

    def _attempt(
        self,
        http: httpx.Client,
        url: str,
        reported_url: httpx.URL,
        deadline: _Deadline | None,
        began: float,
    ) -> _Outcome:
        """Send one request and read its response, checking the deadline.

        The attempt's httpx timeout is the time remaining when it begins.
        Without a deadline, the HTTP client's own timeouts apply.
        """
        timeout = (
            httpx.USE_CLIENT_DEFAULT
            if deadline is None
            else httpx.Timeout(deadline.at - began)
        )
        request = http.build_request(
            _METHOD, url, headers=self._headers, timeout=timeout
        )
        response: httpx.Response | None = None
        try:
            response = http.send(
                request, auth=self._auth, stream=True, follow_redirects=False
            )
            received = _Received(response.status_code, response.headers)
            if self._passed(deadline):
                received.late = True
                return received
            chunks = []
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                if self._passed(deadline):
                    received.late = True
                    return received
            received.body = b"".join(chunks)
            return received
        except httpx.HTTPStatusError as refused:
            # Only a caller's event hook raises this, as one that calls
            # raise_for_status() does. httpx has closed the response, so its
            # body is not read, and the exception, which holds the response,
            # is left out of the chain.
            _scrub(refused, self._key, reported_url)
            return _Received(refused.response.status_code, refused.response.headers)
        except (httpx.TransportError, httpx.DecodingError) as error:
            _scrub(error, self._key, reported_url)
            if response is None:
                return _Raised(error)
            return _Raised(error, response.headers, response.status_code)
        finally:
            if response is not None:
                response.close()

    def _passed(self, deadline: _Deadline | None) -> bool:
        return deadline is not None and self._clock() >= deadline.at

    def _decode(
        self,
        received: _Received,
        body: bytes,
        decode: Callable[[dict[str, Any]], _T],
        check: Callable[[_T], str | None] | None,
        request_id: str | None,
        path: str,
        attempts: int,
    ) -> _T:
        """Validate and decode a successful response's body, and check the page.

        A body that does not validate raises `UnexpectedResponseError`
        outside the `except` block, so that no exception holds the body. So
        does a page that breaks a pagination guarantee, with `status` `None`.
        """
        content_type = received.headers.get("Content-Type")
        status: int | None = received.status
        try:
            value = decode(decode_object(content_type, body))
        except InvalidResponse as invalid:
            reason = str(invalid)
        else:
            broken = None if check is None else check(value)
            if broken is None:
                return value
            reason, status = broken, None
        raise unexpected_response(
            reason,
            status=status,
            request_id=request_id,
            method=_METHOD,
            path=path,
            attempts=attempts,
            secret=self._key,
        )

    def _failure(
        self,
        outcome: _Outcome,
        deadline: _Deadline | None,
        request_id: str | None,
        path: str,
        attempts: int,
    ) -> _Failure:
        """Choose the exception for an attempt that failed, and whether to retry."""
        key = self._key
        if isinstance(outcome, _Raised):
            error = outcome.error
            if isinstance(error, httpx.DecodingError):
                return _Failure(
                    unexpected_response(
                        "the body does not decode as its Content-Encoding says",
                        status=outcome.status,
                        request_id=request_id,
                        method=_METHOD,
                        path=path,
                        attempts=attempts,
                        secret=key,
                    ),
                    error,
                    None,
                )
            if deadline is not None and isinstance(error, httpx.TimeoutException):
                late = self._deadline_exceeded(deadline, error, path, attempts)
                return _Failure(late, error, None)
            return _Failure(
                transport_error(
                    type(error).__name__,
                    str(error),
                    method=_METHOD,
                    path=path,
                    attempts=attempts,
                    secret=key,
                ),
                error,
                error_retry_reason(error, deadline=deadline is not None),
            )
        if outcome.late and deadline is not None:
            late = self._deadline_exceeded(deadline, None, path, attempts)
            return _Failure(late, None, None)
        status = outcome.status
        if 400 <= status <= 599:
            retry_after = parse_retry_after(
                outcome.headers.get("Retry-After"), self._now()
            )
            return _Failure(
                api_error(
                    status=status,
                    content_type=outcome.headers.get("Content-Type"),
                    body=outcome.body,
                    retry_after=retry_after,
                    request_id=request_id,
                    method=_METHOD,
                    path=path,
                    attempts=attempts,
                    secret=key,
                ),
                None,
                status_retry_reason(status),
                retry_after,
            )
        return _Failure(
            unexpected_response(
                _unexpected_status(status),
                status=status,
                request_id=request_id,
                method=_METHOD,
                path=path,
                attempts=attempts,
                secret=key,
            ),
            None,
            None,
        )

    def _deadline_exceeded(
        self,
        deadline: _Deadline,
        cause: httpx.HTTPError | None,
        path: str,
        attempts: int,
    ) -> FinancialDataError:
        return deadline_exceeded(
            deadline.timeout,
            kind=None if cause is None else type(cause).__name__,
            detail="" if cause is None else str(cause),
            method=_METHOD,
            path=path,
            attempts=attempts,
            secret=self._key,
        )


def _unexpected_status(status: int) -> str:
    """Say why a response that is neither a success nor an error is refused."""
    if 100 <= status <= 199:
        return "an informational response, which the API does not send"
    if 200 <= status <= 299:
        return "a successful response that an event hook refused"
    if 300 <= status <= 399:
        return "a redirect, which the SDK does not follow"
    return "a status HTTP does not define"


def _reported_url(base_url: str, path: str, secret: str) -> httpx.URL:
    """Return the URL to leave on httpx's exceptions: the request's, without the key.

    `path` is the path as the SDK reports it, with `[redacted]` in place of
    each argument that holds the key. The base URL can hold the key too. In
    its path, as a gateway's prefix might hold it, the key is replaced where
    it is. Where `[redacted]` would change the scheme, host, or port, the
    whole URL is `[redacted]`, rather than one that names another host.
    """
    base = httpx.URL(base_url)
    try:
        url = httpx.URL(redact(base_url + path, secret))
    except httpx.InvalidURL:  # such as one with [redacted] where its host begins
        return httpx.URL(REDACTED)
    if _origin(url) != _origin(base):
        return httpx.URL(REDACTED)
    return url


def _origin(url: httpx.URL) -> tuple[str, bytes, bytes]:
    """Return a URL's scheme, user information, host, and port."""
    return url.scheme, url.userinfo, url.netloc


def _scrub(error: BaseException, secret: str, url: httpx.URL) -> None:
    """Remove the key from an httpx exception and every exception chained to it.

    The request of each httpx exception reachable through `__cause__` and
    `__context__`, the exception itself included, gets `Bearer [redacted]`
    as its `Authorization`, the URL without the key, and `[redacted]` in
    place of the key in its other headers, such as a `Host` that holds the
    key because the base URL's host does. In the arguments of
    each exception reachable, every occurrence of the key is replaced with
    `[redacted]`: httpx, httpcore, and h11 quote a response line they could
    not parse there, and httpcore passes h11's exception as an argument.
    """
    seen: set[int] = set()
    pending = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, httpx.HTTPError):
            request = current._request
            if request is not None:
                for name, value in request.headers.items():
                    if secret in value:
                        request.headers[name] = redact(value, secret)
                if "Authorization" in request.headers:
                    request.headers["Authorization"] = _REDACTED_AUTHORIZATION
                request.url = url
        current.args = tuple(
            _scrub_argument(argument, secret) for argument in current.args
        )
        pending.extend(
            linked
            for linked in (current.__cause__, current.__context__, *current.args)
            if isinstance(linked, BaseException)
        )


def _scrub_argument(argument: object, secret: str) -> object:
    if isinstance(argument, str):
        return redact(argument, secret)
    if isinstance(argument, bytes):
        return argument.replace(secret.encode("ascii"), REDACTED.encode("ascii"))
    return argument
