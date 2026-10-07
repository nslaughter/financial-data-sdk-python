"""One call over httpx: attempts, waits, the deadline, redaction, and logging.

Every test here uses `httpx.MockTransport` through a supplied client, with
a fake monotonic clock that only the SDK's sleeps and the fake API advance.
"""

from __future__ import annotations

import json
import logging
import platform
import re
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import h11
import httpcore
import httpx
import pytest

from financial_data import (
    APIError,
    AuthenticationError,
    ClientClosedError,
    DeadlineExceededError,
    FinancialDataError,
    InvalidRequestError,
    Meta,
    NotFoundError,
    RateLimitError,
    RetryPolicy,
    ServerError,
    TransportError,
    UnexpectedResponseError,
    __version__,
    _params,
)
from financial_data._decode import decode_meta
from financial_data._params import Target
from financial_data._transport import Pool, Transport

KEY = "demo-research-key"
BASE_URL = "http://api.test"
# The scenarios' profile: waits of 0.1 and 0.2 s before the second and third
# attempts.
PROFILE = RetryPolicy(
    max_attempts=3, max_retry_wait=3.0, base_delay=0.1, max_delay=0.4, jitter=False
)
START = 1000.0
WALL = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
META = {
    "api_version": "v1",
    "supported_api_versions": ["v1"],
    "contract_version": "0.3.0",
    "server_time": "2026-10-01T00:00:00Z",
}
DECODED_META = Meta(
    api_version="v1",
    supported_api_versions=("v1",),
    contract_version="0.3.0",
    server_time=datetime(2026, 10, 1, tzinfo=UTC),
)
ATTRIBUTES = (
    "status",
    "code",
    "title",
    "detail",
    "parameter",
    "problem",
    "retry_after",
    "request_id",
    "method",
    "path",
    "attempts",
)


class Clock:
    """A monotonic clock that only sleeps and the fake API advance."""

    def __init__(self) -> None:
        self.now = START
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def utc(self) -> datetime:
        return WALL + timedelta(seconds=self.now - START)


@dataclass
class Sent:
    """A request as the fake API received it, copied before any redaction."""

    method: str
    url: str
    headers: list[tuple[str, str]]
    timeout: Any
    content: bytes

    def header(self, name: str) -> list[str]:
        return [value for key, value in self.headers if key.lower() == name.lower()]


Step = Callable[[httpx.Request], httpx.Response]


class API:
    """A fake API: it answers request n with step n, and repeats the last step."""

    def __init__(self, *steps: Step) -> None:
        self.steps = steps
        self.sent: list[Sent] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(
            Sent(
                request.method,
                str(request.url),
                list(request.headers.multi_items()),
                request.extensions.get("timeout"),
                request.read(),
            )
        )
        return self.steps[min(len(self.sent), len(self.steps)) - 1](request)


def respond(
    status: int, content: bytes = b"", headers: dict[str, str] | None = None
) -> Step:
    def step(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, content=content)

    return step


def ok(document: Any = META, headers: dict[str, str] | None = None) -> Step:
    return respond(
        200,
        json.dumps(document).encode(),
        {"Content-Type": "application/json", **(headers or {})},
    )


def problem(
    status: int, code: str, headers: dict[str, str] | None = None, **members: Any
) -> Step:
    body = {
        "status": status,
        "code": code,
        "title": "Injected fault",
        "detail": "Injected by the fault proxy.",
        "parameter": None,
        **members,
    }
    return respond(
        status,
        json.dumps(body).encode(),
        {"Content-Type": "application/problem+json", **(headers or {})},
    )


def fail(error: type[httpx.RequestError], message: str = "failed") -> Step:
    def step(request: httpx.Request) -> httpx.Response:
        raise error(message, request=request)

    return step


def after(clock: Clock, seconds: float, step: Step) -> Step:
    """Advance the clock by `seconds` while the request is in flight."""

    def delayed(request: httpx.Request) -> httpx.Response:
        clock.now += seconds
        return step(request)

    return delayed


def connect(
    api: API,
    clock: Clock,
    *,
    timeout: float | None = 5.0,
    retry: RetryPolicy = PROFILE,
    random: Callable[[], float] | None = None,
    key: str = KEY,
    base_url: str = BASE_URL,
    client_timeout: Any = 5.0,
    **options: Any,
) -> tuple[Transport, httpx.Client]:
    """Return a transport that sends through a supplied client to the fake API.

    `client_timeout` is the supplied client's own timeout, and `options` its
    other settings.
    """
    http = httpx.Client(
        transport=httpx.MockTransport(api), timeout=client_timeout, **options
    )

    def no_random() -> float:
        raise AssertionError("the random source was used")

    transport = Transport(
        api_key=key,
        base_url=base_url,
        timeout=timeout,
        retry=retry,
        pool=Pool(http),
        clock=clock.monotonic,
        sleep=clock.sleep,
        random=random or no_random,
        now=clock.utc,
    )
    return transport, http


def get_meta(transport: Transport, target: Target | None = None) -> Meta:
    return transport.call(target or _params.meta(), decode_meta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Capture every record at DEBUG and above, from every logger."""
    caplog.set_level(logging.DEBUG)
    return caplog


def sdk_records(logs: pytest.LogCaptureFixture | None) -> list[logging.LogRecord]:
    if logs is None:
        return []
    return [record for record in logs.records if record.name == "financial_data"]


def chain(error: BaseException) -> list[BaseException]:
    """Every exception reachable through `__cause__`, `__context__`, and `args`."""
    found: list[BaseException] = []
    pending = [error]
    while pending:
        current = pending.pop()
        if any(current is seen for seen in found):
            continue
        found.append(current)
        pending.extend(
            linked
            for linked in (current.__cause__, current.__context__, *current.args)
            if isinstance(linked, BaseException)
        )
    return found


def requests_in(error: BaseException) -> list[httpx.Request]:
    return [
        linked._request
        for linked in chain(error)
        if isinstance(linked, httpx.HTTPError) and linked._request is not None
    ]


def assert_key_absent(
    error: BaseException,
    logs: pytest.LogCaptureFixture | None = None,
    key: str = KEY,
) -> None:
    """Search every rendering of an exception, its chain, and the SDK's records."""
    forms = {key, quote(key, safe="")}
    texts = [str(error), repr(error), "".join(traceback.format_exception(error))]
    texts += [repr(getattr(error, name)) for name in ATTRIBUTES if hasattr(error, name)]
    for linked in chain(error):
        texts += [repr(linked.args), str(linked)]
    for request in requests_in(error):
        assert request.headers.get("Authorization") == "Bearer [redacted]"
        texts += [str(request.url), repr(request.headers.multi_items())]
    for record in sdk_records(logs):
        texts += [record.getMessage(), repr(record.args), str(record.msg)]
    for text in texts:
        for form in forms:
            assert form not in text


# Success and the request


def test_a_successful_response_is_decoded(clock: Clock) -> None:
    api = API(ok())
    transport, _ = connect(api, clock)
    assert get_meta(transport) == DECODED_META
    assert len(api.sent) == 1
    assert clock.sleeps == []


def test_every_request_carries_the_three_headers(clock: Clock) -> None:
    api = API(ok())
    transport, _ = connect(api, clock)
    get_meta(transport)
    [sent] = api.sent
    assert sent.method == "GET"
    assert sent.content == b""
    assert sent.url == "http://api.test/v1/meta"
    assert sent.header("Authorization") == [f"Bearer {KEY}"]
    assert sent.header("Accept") == ["application/json"]
    user_agent = (
        f"financial-data-sdk-python/{__version__} python/{platform.python_version()}"
    )
    assert sent.header("User-Agent") == [user_agent]


def test_the_url_is_the_base_url_and_the_path_with_its_query(clock: Clock) -> None:
    api = API(ok())
    transport, _ = connect(api, clock, base_url="http://gateway.test/prefix")
    target = Target(("v1", "series", "a/b"), (("page_token", "x+y=z&w"),))
    get_meta(transport, target)
    assert api.sent[0].url == (
        "http://gateway.test/prefix/v1/series/a%2Fb?page_token=x%2By%3Dz%26w"
    )


# What is retried: statuses

RETRIED_STATUSES: list[tuple[int, str, type[APIError]]] = [
    (429, "rate_limited", RateLimitError),
    (500, "internal", ServerError),
    (502, "bad_gateway", ServerError),
    (503, "unavailable", ServerError),
    (504, "gateway_timeout", ServerError),
]


@pytest.mark.parametrize(("status", "code", "exception"), RETRIED_STATUSES)
def test_a_transient_status_is_retried_until_max_attempts(
    clock: Clock, status: int, code: str, exception: type[APIError]
) -> None:
    api = API(problem(status, code))
    transport, _ = connect(api, clock)
    with pytest.raises(exception) as caught:
        get_meta(transport)
    assert type(caught.value) is exception
    assert caught.value.status == status
    assert caught.value.code == code
    assert caught.value.attempts == 3
    assert caught.value.retry_after is None
    assert len(api.sent) == 3
    assert clock.sleeps == [0.1, 0.2]
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_gateway_errors_without_a_problem_body_are_retried_until_success(
    clock: Clock,
) -> None:
    api = API(
        respond(503, b"Service Unavailable", {"Content-Type": "text/plain"}),
        respond(
            502, b"<html><body>Bad Gateway</body></html>", {"Content-Type": "text/html"}
        ),
        ok(),
    )
    transport, _ = connect(api, clock)
    assert get_meta(transport) == DECODED_META
    assert len(api.sent) == 3
    assert clock.sleeps == [0.1, 0.2]


NOT_RETRIED_STATUSES: list[tuple[Step, type[APIError]]] = [
    (problem(400, "invalid_parameter", parameter="page_size"), InvalidRequestError),
    (problem(401, "unauthenticated"), AuthenticationError),
    (problem(404, "not_found"), NotFoundError),
    (problem(403, "not_entitled"), APIError),
    (respond(410), APIError),
    (respond(418), APIError),
    (respond(501), ServerError),
    (respond(505), ServerError),
    (respond(599), ServerError),
]


@pytest.mark.parametrize(("step", "exception"), NOT_RETRIED_STATUSES)
def test_any_other_error_status_is_raised_after_one_request(
    clock: Clock, step: Step, exception: type[APIError]
) -> None:
    api = API(step)
    transport, _ = connect(api, clock)
    with pytest.raises(exception) as caught:
        get_meta(transport)
    assert isinstance(caught.value, exception)
    assert caught.value.attempts == 1
    assert len(api.sent) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("status", [101, 199, 300, 301, 302, 304, 307, 399])
def test_an_unexpected_status_is_not_retried(clock: Clock, status: int) -> None:
    api = API(respond(status, b"<html></html>", {"Content-Type": "text/html"}))
    transport, _ = connect(api, clock)
    with pytest.raises(UnexpectedResponseError) as caught:
        get_meta(transport)
    assert caught.value.status == status
    assert caught.value.attempts == 1
    assert len(api.sent) == 1
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("status", [600, 999])
def test_a_status_http_does_not_define_is_not_retried(
    clock: Clock, status: int
) -> None:
    api = API(problem(status, "internal"))
    transport, _ = connect(api, clock)
    with pytest.raises(UnexpectedResponseError) as caught:
        get_meta(transport)
    assert caught.value.status == status
    assert len(api.sent) == 1


@pytest.mark.parametrize(
    "step",
    [
        respond(200, b"<html></html>", {"Content-Type": "text/html"}),
        ok({"api_version": "v1"}),
        ok([META]),
        respond(200, b'{"api_version": ', {"Content-Type": "application/json"}),
    ],
)
def test_a_successful_response_that_fails_validation_is_not_retried(
    clock: Clock, step: Step
) -> None:
    api = API(step)
    transport, _ = connect(api, clock)
    with pytest.raises(UnexpectedResponseError) as caught:
        get_meta(transport)
    assert caught.value.status == 200
    assert caught.value.attempts == 1
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(api.sent) == 1


def test_validation_names_the_member(clock: Clock) -> None:
    api = API(ok({**META, "server_time": "2026-10-01T00:00:00+00:00"}))
    transport, _ = connect(api, clock)
    with pytest.raises(UnexpectedResponseError, match="server_time"):
        get_meta(transport)


# What is retried: exceptions from httpx

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
]
REFUSED_ERRORS: list[type[httpx.TransportError]] = [
    httpx.LocalProtocolError,
    httpx.UnsupportedProtocol,
    httpx.ProxyError,
]


@pytest.mark.parametrize("error", RETRIED_ERRORS)
def test_a_connection_failure_is_retried_then_raised_as_transport_error(
    clock: Clock, error: type[httpx.TransportError]
) -> None:
    api = API(fail(error, "connection failed"))
    transport, _ = connect(api, clock)
    with pytest.raises(TransportError) as caught:
        get_meta(transport)
    assert caught.value.attempts == 3
    assert caught.value.method == "GET"
    assert caught.value.path == "/v1/meta"
    assert type(caught.value.__cause__) is error
    assert caught.value.__context__ is None
    assert str(caught.value) == f"{error.__name__}: connection failed (GET /v1/meta)"
    assert len(api.sent) == 3
    assert clock.sleeps == [0.1, 0.2]


@pytest.mark.parametrize("error", RETRIED_ERRORS)
def test_a_connection_failure_then_success(
    clock: Clock, error: type[httpx.TransportError]
) -> None:
    api = API(fail(error), ok())
    transport, _ = connect(api, clock)
    assert get_meta(transport) == DECODED_META
    assert len(api.sent) == 2


@pytest.mark.parametrize("error", TIMEOUTS)
def test_an_httpx_timeout_with_a_deadline_is_the_deadline_and_not_retried(
    clock: Clock, error: type[httpx.TimeoutException]
) -> None:
    api = API(fail(error, "timed out"))
    transport, _ = connect(api, clock, timeout=2.0)
    with pytest.raises(DeadlineExceededError) as caught:
        get_meta(transport)
    assert caught.value.attempts == 1
    assert type(caught.value.__cause__) is error
    assert caught.value.__context__ is None
    assert isinstance(caught.value, TimeoutError)
    assert str(caught.value) == (
        f"the deadline of 2 s passed: {error.__name__}: timed out (GET /v1/meta)"
    )
    assert len(api.sent) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("error", TIMEOUTS)
def test_a_supplied_clients_own_timeout_is_retried_without_a_deadline(
    clock: Clock, error: type[httpx.TimeoutException]
) -> None:
    api = API(fail(error, "timed out"))
    transport, _ = connect(api, clock, timeout=None, client_timeout=None)
    with pytest.raises(TransportError) as caught:
        get_meta(transport)
    assert caught.value.attempts == 3
    assert type(caught.value.__cause__) is error
    assert len(api.sent) == 3
    assert clock.sleeps == [0.1, 0.2]


@pytest.mark.parametrize("error", REFUSED_ERRORS)
def test_a_request_httpx_or_a_proxy_refuses_is_not_retried(
    clock: Clock, error: type[httpx.TransportError]
) -> None:
    api = API(fail(error, "refused"))
    transport, _ = connect(api, clock)
    with pytest.raises(TransportError) as caught:
        get_meta(transport)
    assert caught.value.attempts == 1
    assert type(caught.value.__cause__) is error
    assert len(api.sent) == 1


def test_a_body_that_does_not_decode_as_its_encoding_says_is_not_retried(
    clock: Clock,
) -> None:
    def gzip_that_is_not(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
            stream=httpx.ByteStream(b"not gzip"),
        )

    api = API(gzip_that_is_not)
    transport, _ = connect(api, clock)
    with pytest.raises(UnexpectedResponseError) as caught:
        get_meta(transport)
    assert caught.value.status == 200
    assert caught.value.attempts == 1
    assert type(caught.value.__cause__) is httpx.DecodingError
    assert caught.value.__context__ is None
    assert len(api.sent) == 1
    assert_key_absent(caught.value)


# Bounds


def test_max_attempts_of_one_turns_retries_off(clock: Clock) -> None:
    api = API(respond(503))
    transport, _ = connect(api, clock, retry=RetryPolicy(max_attempts=1))
    with pytest.raises(ServerError) as caught:
        get_meta(transport)
    assert caught.value.attempts == 1
    assert len(api.sent) == 1


def test_retrying_stops_before_the_budget_is_exceeded(clock: Clock) -> None:
    api = API(respond(503, headers={"Retry-After": "2"}))
    transport, _ = connect(api, clock)
    with pytest.raises(ServerError) as caught:
        get_meta(transport)
    # 2 s of waiting fits the 3 s budget; another 2 s would not.
    assert caught.value.attempts == 2
    assert caught.value.retry_after == 2.0
    assert clock.sleeps == [2.0]


def test_the_deadline_bounds_backoff(clock: Clock) -> None:
    # deadline-bounds-backoff: a wait that would end after the deadline is
    # not started, and the last attempt's exception is raised.
    api = API(after(clock, 0.01, respond(503, b"Service Unavailable")))
    transport, _ = connect(api, clock, timeout=0.25)
    with pytest.raises(ServerError) as caught:
        get_meta(transport)
    assert caught.value.status == 503
    assert caught.value.code is None
    assert caught.value.attempts == 2
    assert clock.sleeps == [0.1]
    assert len(api.sent) == 2


@pytest.mark.parametrize(
    ("status", "code", "exception"),
    [(429, "rate_limited", RateLimitError), (503, "unavailable", ServerError)],
)
def test_a_retry_after_beyond_the_budget_raises_at_once(
    clock: Clock, status: int, code: str, exception: type[APIError]
) -> None:
    api = API(problem(status, code, {"Retry-After": "30"}))
    transport, _ = connect(api, clock)
    with pytest.raises(exception) as caught:
        get_meta(transport)
    assert type(caught.value) is exception
    assert caught.value.retry_after == 30.0
    assert caught.value.attempts == 1
    assert clock.sleeps == []
    assert len(api.sent) == 1


def test_a_retry_after_beyond_the_deadline_raises_at_once(clock: Clock) -> None:
    api = API(problem(429, "rate_limited", {"Retry-After": "2"}))
    transport, _ = connect(api, clock, timeout=1.0)
    with pytest.raises(RateLimitError) as caught:
        get_meta(transport)
    assert caught.value.retry_after == 2.0
    assert caught.value.attempts == 1
    assert clock.sleeps == []


def test_throttled_on_every_attempt(clock: Clock) -> None:
    api = API(problem(429, "rate_limited"))
    transport, _ = connect(api, clock)
    with pytest.raises(RateLimitError) as caught:
        get_meta(transport)
    assert caught.value.attempts == 3
    assert caught.value.retry_after is None
    assert clock.sleeps == [0.1, 0.2]


def test_a_sleep_that_ends_after_the_deadline_raises_the_last_exception(
    clock: Clock,
) -> None:
    api = API(respond(503))
    transport, _ = connect(api, clock, timeout=1.0)

    def oversleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += 5.0

    transport._sleep = oversleep
    with pytest.raises(ServerError) as caught:
        get_meta(transport)
    assert caught.value.attempts == 1
    assert len(api.sent) == 1


# Waiting


def test_jitter_draws_each_wait_from_the_random_source(clock: Clock) -> None:
    draws = iter([0.5, 0.25])
    used: list[float] = []

    def random() -> float:
        used.append(next(draws))
        return used[-1]

    api = API(respond(503), respond(503), ok())
    policy = RetryPolicy(
        max_attempts=3, max_retry_wait=3.0, base_delay=0.1, max_delay=0.4
    )
    transport, _ = connect(api, clock, retry=policy, random=random)
    get_meta(transport)
    assert used == [0.5, 0.25]
    assert clock.sleeps == [0.5 * 0.1, 0.25 * 0.2]


@pytest.mark.parametrize(
    ("value", "wait"),
    [
        ("1", 1.0),
        ("0", 0.0),
        ("Tue, 06 Oct 2026 12:00:02 GMT", 2.0),
        ("Tuesday, 06-Oct-26 12:00:02 GMT", 2.0),
        ("Tue Oct  6 12:00:02 2026", 2.0),
        ("Tue, 06 Oct 2026 11:00:00 GMT", 0.0),
        ("soon", 0.1),
        ("-1", 0.1),
    ],
)
def test_retry_after_in_each_form(clock: Clock, value: str, wait: float) -> None:
    api = API(problem(429, "rate_limited", {"Retry-After": value}), ok())
    transport, _ = connect(api, clock)
    assert get_meta(transport) == DECODED_META
    assert clock.sleeps == [wait]
    assert len(api.sent) == 2


def test_retry_after_on_an_error_that_is_not_retried_is_reported(
    clock: Clock,
) -> None:
    api = API(problem(403, "not_entitled", {"Retry-After": "120"}))
    transport, _ = connect(api, clock)
    with pytest.raises(APIError) as caught:
        get_meta(transport)
    assert caught.value.retry_after == 120.0


# Deadline


def test_each_attempt_has_the_time_remaining_as_its_timeout(clock: Clock) -> None:
    api = API(after(clock, 1.0, respond(503)), after(clock, 1.0, respond(503)), ok())
    transport, _ = connect(api, clock, timeout=5.0)
    get_meta(transport)
    timeouts = [sent.timeout for sent in api.sent]
    for timeout, remaining in zip(timeouts, [5.0, 3.9, 2.7], strict=True):
        assert set(timeout) == {"connect", "read", "write", "pool"}
        assert all(value == pytest.approx(remaining) for value in timeout.values())


def test_without_a_deadline_the_clients_own_timeouts_apply(clock: Clock) -> None:
    api = API(ok())
    transport, _ = connect(api, clock, timeout=None, client_timeout=7.0)
    get_meta(transport)
    assert api.sent[0].timeout == {
        "connect": 7.0,
        "read": 7.0,
        "write": 7.0,
        "pool": 7.0,
    }


@pytest.mark.parametrize("step", [ok(), respond(503), problem(404, "not_found")])
def test_the_deadline_passing_during_a_request(clock: Clock, step: Step) -> None:
    # deadline-during-request: checked when the headers arrive, whatever the
    # status, and not retried.
    api = API(after(clock, 1.5, step))
    transport, _ = connect(api, clock, timeout=1.0)
    with pytest.raises(DeadlineExceededError) as caught:
        get_meta(transport)
    assert caught.value.attempts == 1
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert str(caught.value) == "the deadline of 1 s passed (GET /v1/meta)"
    assert len(api.sent) == 1
    assert clock.sleeps == []


class SlowBody(httpx.SyncByteStream):
    """A body whose chunks each take `interval` seconds on the fake clock."""

    def __init__(self, clock: Clock, chunks: list[bytes], interval: float) -> None:
        self.clock = clock
        self.chunks = chunks
        self.interval = interval
        self.returned = 0
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.clock.now += self.interval
            self.returned += 1
            yield chunk

    def close(self) -> None:
        self.closed = True


def split(content: bytes, parts: int) -> list[bytes]:
    size = -(-len(content) // parts)
    return [content[index : index + size] for index in range(0, len(content), size)]


@pytest.mark.parametrize(("parts", "returned"), [(8, 4), (4, 4)])
def test_a_body_that_moves_the_clock_past_the_deadline_is_refused(
    clock: Clock, parts: int, returned: int
) -> None:
    # With 8 parts the body is incomplete when the deadline passes; with 4,
    # the last part completes it, and it is refused all the same.
    body = SlowBody(clock, split(json.dumps(META).encode(), parts), 0.3)

    def slow(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": "application/json"}, stream=body
        )

    api = API(slow)
    transport, _ = connect(api, clock, timeout=1.0)
    with pytest.raises(DeadlineExceededError) as caught:
        get_meta(transport)
    assert body.returned == returned
    assert body.closed
    assert caught.value.attempts == 1
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(api.sent) == 1


def test_the_deadline_is_checked_when_the_headers_arrive(clock: Clock) -> None:
    body = SlowBody(clock, split(json.dumps(META).encode(), 2), 0.0)

    def late_headers(request: httpx.Request) -> httpx.Response:
        clock.now += 1.5
        return httpx.Response(
            200, headers={"Content-Type": "application/json"}, stream=body
        )

    transport, _ = connect(API(late_headers), clock, timeout=1.0)
    with pytest.raises(DeadlineExceededError) as caught:
        get_meta(transport)
    assert body.returned == 0
    assert body.closed
    assert caught.value.__cause__ is None


def test_a_body_within_the_deadline_is_read_in_full(clock: Clock) -> None:
    body = SlowBody(clock, split(json.dumps(META).encode(), 3), 0.3)

    def slow(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": "application/json"}, stream=body
        )

    transport, _ = connect(API(slow), clock, timeout=1.0)
    assert get_meta(transport) == DECODED_META
    assert body.returned == 3
    assert body.closed


def test_a_body_is_read_without_a_deadline_however_slow(clock: Clock) -> None:
    body = SlowBody(clock, split(json.dumps(META).encode(), 8), 60.0)

    def slow(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": "application/json"}, stream=body
        )

    transport, _ = connect(API(slow), clock, timeout=None)
    assert get_meta(transport) == DECODED_META


# A caller-supplied client


def test_a_supplied_clients_headers_are_sent_and_its_auth_replaced(
    clock: Clock,
) -> None:
    api = API(ok())
    hook_calls: list[str] = []

    def hook(request: httpx.Request) -> None:
        hook_calls.append(request.url.path)

    transport, http = connect(
        api,
        clock,
        headers={
            "X-Customer": "acme",
            "Accept": "text/html",
            "Authorization": "Basic Y3VzdG9tZXI6c2VjcmV0",
        },
        auth=("user", "pass"),
        event_hooks={"request": [hook]},
    )
    get_meta(transport)
    [sent] = api.sent
    assert sent.header("X-Customer") == ["acme"]
    assert sent.header("Authorization") == [f"Bearer {KEY}"]
    assert sent.header("Accept") == ["application/json"]
    assert hook_calls == ["/v1/meta"]
    assert not http.is_closed
    assert http.get("http://api.test/v1/meta").status_code == 200


def test_a_supplied_client_that_follows_redirects_does_not_follow_one(
    clock: Clock,
) -> None:
    api = API(respond(302, headers={"Location": "http://api.test/v1/meta"}), ok())
    transport, _ = connect(api, clock, follow_redirects=True)
    with pytest.raises(UnexpectedResponseError) as caught:
        get_meta(transport)
    assert caught.value.status == 302
    assert caught.value.attempts == 1
    assert len(api.sent) == 1


def test_a_closed_supplied_client_raises_client_closed_error(clock: Clock) -> None:
    api = API(ok())
    transport, http = connect(api, clock)
    http.close()
    with pytest.raises(ClientClosedError) as caught:
        get_meta(transport)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert api.sent == []


def test_a_supplied_client_closed_between_attempts(clock: Clock) -> None:
    http: httpx.Client | None = None

    def close_then_fail(request: httpx.Request) -> httpx.Response:
        assert http is not None
        http.close()
        return httpx.Response(503)

    api = API(close_then_fail)
    transport, http = connect(api, clock)
    with pytest.raises(ClientClosedError):
        get_meta(transport)
    assert len(api.sent) == 1


def raise_for_status(response: httpx.Response) -> None:
    response.raise_for_status()


def read_then_raise_for_status(response: httpx.Response) -> None:
    response.read()
    response.raise_for_status()


def test_a_hook_that_refuses_a_503_is_retried_as_its_status_says(
    clock: Clock,
) -> None:
    api = API(problem(503, "unavailable", {"Request-Id": "req_1"}))
    transport, _ = connect(api, clock, event_hooks={"response": [raise_for_status]})
    with pytest.raises(ServerError) as caught:
        get_meta(transport)
    assert type(caught.value) is ServerError
    assert caught.value.status == 503
    # The SDK reads no body from a response a hook refused.
    assert caught.value.code is None
    assert caught.value.problem is None
    assert caught.value.request_id == "req_1"
    assert caught.value.attempts == 3
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(api.sent) == 3


def test_a_hook_that_refuses_a_403_problem_raises_exactly_api_error(
    clock: Clock,
) -> None:
    api = API(problem(403, "not_entitled", {"Retry-After": "5"}))
    transport, _ = connect(
        api, clock, event_hooks={"response": [read_then_raise_for_status]}
    )
    with pytest.raises(APIError) as caught:
        get_meta(transport)
    assert type(caught.value) is APIError
    assert caught.value.status == 403
    assert caught.value.code is None
    assert caught.value.retry_after == 5.0
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(api.sent) == 1


def test_a_hook_that_refuses_a_redirect_echoing_the_key(
    clock: Clock, logs: pytest.LogCaptureFixture
) -> None:
    headers = {"Location": f"http://elsewhere.test/{KEY}", "Request-Id": KEY}
    api = API(respond(302, headers=headers))
    transport, _ = connect(
        api,
        clock,
        follow_redirects=True,
        event_hooks={"response": [raise_for_status]},
    )
    with pytest.raises(UnexpectedResponseError) as caught:
        get_meta(transport)
    assert caught.value.status == 302
    assert caught.value.request_id == "[redacted]"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(api.sent) == 1
    assert_key_absent(caught.value, logs)


def test_a_hook_that_reads_and_refuses_a_401_echoing_the_key(
    clock: Clock, logs: pytest.LogCaptureFixture
) -> None:
    def echo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            headers={"Content-Type": "application/problem+json"},
            content=json.dumps({"code": "unauthenticated", "detail": KEY}).encode(),
            extensions={"reason_phrase": f"Unauthorized {KEY}".encode()},
        )

    api = API(echo)
    transport, _ = connect(
        api, clock, event_hooks={"response": [read_then_raise_for_status]}
    )
    with pytest.raises(AuthenticationError) as caught:
        get_meta(transport)
    assert caught.value.status == 401
    assert caught.value.detail is None
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(api.sent) == 1
    assert_key_absent(caught.value, logs)


def test_an_exception_from_a_request_hook_propagates_unchanged(clock: Clock) -> None:
    raised = ValueError("refused by the hook", KEY)
    calls: list[httpx.Request] = []

    def hook(request: httpx.Request) -> None:
        calls.append(request)
        raise raised

    api = API(ok())
    transport, _ = connect(api, clock, event_hooks={"request": [hook]})
    with pytest.raises(ValueError) as caught:
        get_meta(transport)
    assert caught.value is raised
    assert caught.value.args == ("refused by the hook", KEY)
    assert len(calls) == 1
    assert api.sent == []
    assert clock.sleeps == []


def test_an_exception_from_a_response_hook_propagates_unchanged(
    clock: Clock,
) -> None:
    raised = RuntimeError("refused by the hook")

    def hook(response: httpx.Response) -> None:
        raise raised

    api = API(respond(503))
    transport, _ = connect(api, clock, event_hooks={"response": [hook]})
    with pytest.raises(RuntimeError) as caught:
        get_meta(transport)
    assert caught.value is raised
    assert len(api.sent) == 1


# The key


def raise_malformed_header_line(request: httpx.Request) -> httpx.Response:
    """Raise as httpx, httpcore, and h11 do for a header line that does not parse."""
    line = "illegal header line: bytearray(b'X-Echo: Bearer " + KEY + "')"
    try:
        try:
            raise h11.RemoteProtocolError(line)
        except h11.RemoteProtocolError as error:
            raise httpcore.RemoteProtocolError(error) from error
    except httpcore.RemoteProtocolError as error:
        raise httpx.RemoteProtocolError(str(error), request=request) from error


def test_a_malformed_response_quoting_the_key_is_retried_and_redacted(
    clock: Clock, logs: pytest.LogCaptureFixture
) -> None:
    api = API(raise_malformed_header_line)
    transport, _ = connect(api, clock)
    with pytest.raises(TransportError) as caught:
        get_meta(transport)
    assert caught.value.attempts == 3
    assert len(api.sent) == 3
    cause = caught.value.__cause__
    assert isinstance(cause, httpx.RemoteProtocolError)
    assert cause.args == (
        "illegal header line: bytearray(b'X-Echo: Bearer [redacted]')",
    )
    inner = cause.__cause__
    assert isinstance(inner, httpcore.RemoteProtocolError)
    [h11_error] = inner.args
    assert isinstance(h11_error, h11.RemoteProtocolError)
    assert h11_error.args == cause.args
    assert inner.__cause__ is h11_error
    assert "[redacted]" in str(caught.value)
    assert_key_absent(caught.value, logs)
    # Every request still carried the key.
    for sent in api.sent:
        assert sent.header("Authorization") == [f"Bearer {KEY}"]


@pytest.mark.parametrize(
    ("status", "content_type", "exception"),
    [
        (200, "application/json", UnexpectedResponseError),
        (401, "application/problem+json", AuthenticationError),
    ],
)
def test_a_body_that_holds_the_key_and_does_not_parse_is_not_chained(
    clock: Clock,
    logs: pytest.LogCaptureFixture,
    status: int,
    content_type: str,
    exception: type[FinancialDataError],
) -> None:
    content = f'{{"detail": "{KEY}", '.encode()
    api = API(respond(status, content, {"Content-Type": content_type}))
    transport, _ = connect(api, clock)
    with pytest.raises(exception) as caught:
        get_meta(transport)
    assert type(caught.value) is exception
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert_key_absent(caught.value, logs)


def test_a_problem_that_echoes_the_key_leaves_only_redacted(
    clock: Clock, logs: pytest.LogCaptureFixture
) -> None:
    # As key-absent-from-errors-and-logs, step 6.
    body = {
        "status": 401,
        "code": "unauthenticated",
        "title": "Unauthenticated",
        "detail": f"Bearer {KEY} is not valid.",
        "parameter": None,
        "echo": {"headers": [f"Authorization: Bearer {KEY}"]},
    }
    headers = {"Content-Type": "application/problem+json", "Request-Id": KEY}
    api = API(respond(401, json.dumps(body).encode(), headers))
    transport, _ = connect(api, clock)
    with pytest.raises(AuthenticationError) as caught:
        get_meta(transport)
    assert caught.value.detail == "Bearer [redacted] is not valid."
    assert caught.value.request_id == "[redacted]"
    assert caught.value.problem is not None
    assert caught.value.problem["echo"] == {
        "headers": ["Authorization: Bearer [redacted]"]
    }
    assert "request_id [redacted]" in str(caught.value)
    assert_key_absent(caught.value, logs)
    assert any("[redacted]" in record.getMessage() for record in sdk_records(logs))


@pytest.mark.parametrize("key", [KEY, "key/with=slash", "k+y&z?#"])
@pytest.mark.parametrize(
    ("step", "exception"),
    [
        (problem(404, "not_found"), NotFoundError),
        (fail(httpx.RemoteProtocolError, "Server disconnected"), TransportError),
    ],
)
def test_an_argument_holding_the_key_is_reported_as_redacted(
    clock: Clock,
    logs: pytest.LogCaptureFixture,
    key: str,
    step: Step,
    exception: type[FinancialDataError],
) -> None:
    api = API(step)
    transport, _ = connect(api, clock, key=key)
    target = Target(("v1", "series", key), (("page_token", f"t-{key}"), ("n", "1")))
    with pytest.raises(exception) as caught:
        get_meta(transport, target)
    reported = "/v1/series/[redacted]?page_token=[redacted]&n=1"
    assert getattr(caught.value, "path") == reported  # noqa: B009
    assert f"(GET {reported})" in str(caught.value)
    # The request was sent as given.
    assert api.sent[0].url == BASE_URL + target.path_with_query
    assert_key_absent(caught.value, logs, key)
    for record in sdk_records(logs):
        assert reported in record.getMessage()
    requests = requests_in(caught.value)
    if exception is TransportError:
        assert requests, "the chain holds the request"
    for request in requests:
        assert request.url == httpx.URL(BASE_URL + reported)


def test_a_key_in_the_base_url_is_kept_out_of_the_chain(
    clock: Clock, logs: pytest.LogCaptureFixture
) -> None:
    api = API(fail(httpx.ConnectError))
    transport, _ = connect(api, clock, base_url=f"http://gateway.test/{KEY}")
    with pytest.raises(TransportError) as caught:
        get_meta(transport)
    [request] = requests_in(caught.value)
    assert request.url == httpx.URL("http://gateway.test/[redacted]/v1/meta")
    assert_key_absent(caught.value, logs)


@pytest.mark.parametrize(
    ("key", "base_url"),
    [
        ("gateway", "http://gateway.test"),
        ("example", "http://api.example.test"),
        ("gateway", "http://gateway.test/gateway"),
        ("8443", "https://api.test:8443"),
        ("https", "https://api.test"),
        ("test/v", "http://api.test"),
        ("/v1", "http://api.test"),
        ("/prefix", "http://api.test/prefix"),
    ],
)
def test_a_key_that_would_change_the_host_redacts_the_whole_url(
    clock: Clock, logs: pytest.LogCaptureFixture, key: str, base_url: str
) -> None:
    # Before the path, [redacted] would make a host httpx cannot parse, or
    # another host, as `http://api.test[redacted]/meta` does.
    api = API(fail(httpx.ConnectError))
    transport, _ = connect(api, clock, key=key, base_url=base_url)
    with pytest.raises(TransportError) as caught:
        get_meta(transport)
    [request] = requests_in(caught.value)
    assert request.url == httpx.URL("[redacted]")
    assert_key_absent(caught.value, logs, key=key)


def test_a_key_in_the_host_is_kept_out_of_the_host_header(clock: Clock) -> None:
    api = API(fail(httpx.ConnectError))
    transport, _ = connect(api, clock, key="gateway", base_url="http://gateway.test")
    with pytest.raises(TransportError) as caught:
        get_meta(transport)
    [request] = requests_in(caught.value)
    assert api.sent[0].header("Host") == ["gateway.test"]
    assert request.headers["Host"] == "[redacted].test"


@pytest.mark.parametrize(
    "step",
    [
        fail(httpx.ConnectError),
        fail(httpx.ReadTimeout),
        fail(httpx.LocalProtocolError),
        problem(429, "rate_limited", {"Retry-After": "30"}),
        respond(200, b"<html></html>", {"Content-Type": "text/html"}),
    ],
)
def test_the_key_is_absent_from_every_failure(
    clock: Clock, logs: pytest.LogCaptureFixture, step: Step
) -> None:
    def echo(request: httpx.Request) -> httpx.Response:
        try:
            return step(request)
        except httpx.HTTPError as error:
            error.args = (f"{error.args[0]}: {KEY}",)
            raise

    api = API(echo)
    transport, _ = connect(api, clock, timeout=1.0)
    with pytest.raises(FinancialDataError) as caught:
        get_meta(transport)
    assert_key_absent(caught.value, logs)
    for sent in api.sent:
        assert sent.header("Authorization") == [f"Bearer {KEY}"]


# Request IDs


def test_request_id_comes_from_whatever_answered(
    clock: Clock, logs: pytest.LogCaptureFixture
) -> None:
    # request-id-from-any-response
    api = API(
        respond(
            503, b"busy", {"Content-Type": "text/plain", "Request-Id": "req_first"}
        ),
        problem(429, "rate_limited", {"Retry-After": "30", "Request-Id": "req_second"}),
    )
    transport, _ = connect(api, clock)
    with pytest.raises(RateLimitError) as caught:
        get_meta(transport)
    assert caught.value.request_id == "req_second"
    assert caught.value.attempts == 2
    assert "req_second" in str(caught.value)
    [retry] = [record for record in sdk_records(logs) if record.levelno == logging.INFO]
    assert "req_first" in retry.getMessage()


def test_an_unexpected_response_has_its_request_id(clock: Clock) -> None:
    api = API(respond(200, b"<html></html>", {"Request-Id": "req_9"}))
    transport, _ = connect(api, clock)
    with pytest.raises(UnexpectedResponseError) as caught:
        get_meta(transport)
    assert caught.value.request_id == "req_9"


# Logging


def test_each_attempt_is_logged_at_debug_and_each_retry_at_info(
    clock: Clock, logs: pytest.LogCaptureFixture
) -> None:
    api = API(
        after(clock, 0.25, respond(503, headers={"Request-Id": "req_1"})),
        fail(httpx.ConnectError),
        ok(headers={"Request-Id": "req_3"}),
    )
    transport, _ = connect(api, clock)
    get_meta(transport)
    logged = [(record.levelname, record.args) for record in sdk_records(logs)]
    assert logged == [
        ("DEBUG", ("GET", "/v1/meta", 1, "status 503", 0.25, "req_1")),
        (
            "INFO",
            (
                "GET",
                "/v1/meta",
                1,
                "status 503",
                0.25,
                "req_1",
                0.1,
                "the server failed",
            ),
        ),
        ("DEBUG", ("GET", "/v1/meta", 2, "ConnectError", 0.0, None)),
        (
            "INFO",
            ("GET", "/v1/meta", 2, "ConnectError", 0.0, None, 0.2, "connecting failed"),
        ),
        ("DEBUG", ("GET", "/v1/meta", 3, "status 200", 0.0, "req_3")),
    ]
    messages = [record.getMessage() for record in sdk_records(logs)]
    assert (
        messages[0] == "GET /v1/meta: attempt 1, status 503, 0.250 s, request ID req_1"
    )
    assert messages[1] == (
        "GET /v1/meta: attempt 1, status 503, 0.250 s, request ID req_1; "
        "retrying in 0.100 s because the server failed"
    )


def test_a_deadline_passed_while_reading_is_logged(
    clock: Clock, logs: pytest.LogCaptureFixture
) -> None:
    api = API(after(clock, 2.0, ok()))
    transport, _ = connect(api, clock, timeout=1.0)
    with pytest.raises(DeadlineExceededError):
        get_meta(transport)
    [record] = sdk_records(logs)
    assert record.levelno == logging.DEBUG
    assert record.args == (
        "GET",
        "/v1/meta",
        1,
        "status 200, then the deadline passed",
        2.0,
        None,
    )


def test_the_logger_has_no_handlers_and_logs_nothing_at_warning(
    clock: Clock, logs: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("financial_data")
    assert logger.handlers == []
    assert logger.propagate
    api = API(respond(503), fail(httpx.ReadError), problem(429, "rate_limited"))
    transport, _ = connect(api, clock)
    with pytest.raises(RateLimitError):
        get_meta(transport)
    assert sdk_records(logs)
    assert all(record.levelno < logging.WARNING for record in sdk_records(logs))
    assert logger.handlers == []


def test_no_request_header_is_logged(
    clock: Clock, logs: pytest.LogCaptureFixture
) -> None:
    api = API(respond(503), ok())
    transport, _ = connect(api, clock)
    get_meta(transport)
    for record in sdk_records(logs):
        message = record.getMessage()
        assert "Bearer" not in message
        assert "Authorization" not in message
        assert not re.search("user-agent|accept", message, re.IGNORECASE)


# The pool


def test_the_pool_creates_its_own_client_once_without_timeouts_or_redirects() -> None:
    pool = Pool(None)
    http = pool.get()
    try:
        assert pool.get() is http
        assert http.timeout == httpx.Timeout(None)
        assert not http.follow_redirects
    finally:
        pool.close()
    assert http.is_closed
    with pytest.raises(ClientClosedError):
        pool.get()
    pool.close()


def test_closing_the_pool_leaves_a_supplied_client_open() -> None:
    http = httpx.Client(transport=httpx.MockTransport(ok()))
    pool = Pool(http)
    assert pool.get() is http
    pool.close()
    assert not http.is_closed
    with pytest.raises(ClientClosedError):
        pool.get()
    http.close()


def test_a_pool_closed_before_any_request_creates_no_client() -> None:
    pool = Pool(None)
    pool.close()
    with pytest.raises(ClientClosedError):
        pool.get()
    assert pool._own is None
