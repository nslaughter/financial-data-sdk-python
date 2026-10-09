"""The SDK runner: the shared checks of `contract/expected/`, run through the SDK.

`spec/conformance.md` defines it under "The SDK runner". It follows the
API's conformance format, and adds only how a step reaches the API and how
an SDK result is compared:

- Each customer credential has one SDK client, with retries off, sending
  through one recording client whose request hook records each request.
- A request step goes through the SDK when `routing.route` finds a call
  for it, and over HTTP otherwise, exactly as the API runner sends it. Test
  actions, and the reset before every check, use the runner's own client.
- An SDK step passes when the SDK sent exactly the step's request, and its
  result, converted to JSON, or its exception meets the step's `expect`.
- The query, page, position, read, and apply checks call the SDK.

`Runner.run` returns the first difference of a failing check as a
`Failure`, which never contains a fixture key, and `Runner.counts` holds
how many steps of each file went through the SDK and over HTTP.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, NoReturn, TypeVar
from urllib.parse import quote

import httpx

from financial_data import (
    APIError,
    AuthenticationError,
    Client,
    InvalidRequestError,
    NotEntitledError,
    NotFoundError,
    PageTokenError,
    PageTokenExpiredError,
    PositionAheadError,
    PositionExpiredError,
    RateLimitError,
    RetryPolicy,
    Revision,
    ServerError,
    UnsupportedAPIVersionError,
)
from tests.support.api import (
    CONTROL_CREDENTIAL,
    CREDENTIALS,
    CUSTOMERS,
    DEFAULT_CREDENTIAL,
    without_keys,
)
from tests.support.convert import to_json
from tests.support.query import parse_query

from .matching import (
    Body,
    Difference,
    UnresolvedReference,
    equal_numbers,
    is_number,
    json_type,
    match,
    render,
    resolve,
    shorten,
)
from .routing import ABSENT, InvalidStep, Request, SDKCall, build, route
from .suite import Check, load_json

TIMEOUT: Final = 30.0
"""The deadline of each SDK call, and the timeout of each request over HTTP."""
STREAM_DATASET: Final = "core-indicators"
"""The dataset whose head position and change stream the change-stream checks read."""

_T = TypeVar("_T")

_BY_CODE: Final[Mapping[tuple[int, str], type[APIError]]] = {
    (400, "unknown_parameter"): InvalidRequestError,
    (400, "missing_parameter"): InvalidRequestError,
    (400, "invalid_parameter"): InvalidRequestError,
    (400, "conflicting_cutoffs"): InvalidRequestError,
    (400, "cutoff_in_future"): InvalidRequestError,
    (400, "invalid_page_token"): PageTokenError,
    (400, "page_token_mismatch"): PageTokenError,
    (410, "page_token_expired"): PageTokenExpiredError,
    (400, "position_ahead"): PositionAheadError,
    (410, "position_expired"): PositionExpiredError,
    (403, "not_entitled"): NotEntitledError,
    (404, "not_found"): NotFoundError,
    (404, "unsupported_api_version"): UnsupportedAPIVersionError,
}
"""The client contract's exceptions table, by status and code."""


def exception_class(status: int, code: str | None) -> type[APIError] | None:
    """Return the exception the client contract gives for a status and code.

    The code chooses first, then the status alone. `None` means no
    exception: the SDK raises none of these for a status outside 400-599.
    """
    if not 400 <= status <= 599:
        return None
    if code is not None and (status, code) in _BY_CODE:
        return _BY_CODE[(status, code)]
    if status == 401:
        return AuthenticationError
    if status == 429:
        return RateLimitError
    if status >= 500:
        return ServerError
    return APIError


@dataclass(frozen=True, slots=True)
class Failure:
    """The first difference in a failing check.

    `step` is the scenario step, from 1, or 0 for the reset that starts
    every check and for checks without steps. `request` is the SDK call made
    or the HTTP request sent, and `at` where the difference is, such as
    `status` or `body.data[0].value`.
    """

    check: Check
    step: int
    request: str
    at: str
    expected: str
    actual: str

    def __str__(self) -> str:
        step = f"step {self.step}: " if self.step else ""
        lines = (
            str(self.check),
            f"{step}{self.request}",
            f"at {self.at}",
            f"  expected: {self.expected}",
            f"  actual:   {self.actual}",
        )
        return without_keys("\n".join(lines))


@dataclass(slots=True)
class Counts:
    """What one file's checks sent: SDK checks, and request steps by route."""

    checks: int = 0
    """Query, page, position, read, and apply checks, which call the SDK."""
    sdk: int = 0
    """Scenario request steps sent through the SDK."""
    http: int = 0
    """Scenario request steps sent over HTTP."""


@dataclass(frozen=True, slots=True)
class Recorded:
    """A request the recording client sent: its method, path, and query."""

    method: str
    path: str
    """The path as sent, percent-encoded, without the base URL's path."""
    query: tuple[tuple[str, str], ...]
    """Each query parameter, decoded, in order."""


class Recorder:
    """The recording client's request hook."""

    def __init__(self, prefix: str = "") -> None:
        self._prefix = prefix
        self.requests: list[Recorded] = []

    def __call__(self, request: httpx.Request) -> None:
        target = request.url.raw_path.decode("ascii")
        path, _, query = target.partition("?")
        if self._prefix and path.startswith(self._prefix + "/"):
            path = path[len(self._prefix) :]
        self.requests.append(Recorded(request.method, path, parse_query(query)))


@dataclass(frozen=True, slots=True)
class _Exchange:
    """A request sent over HTTP and the response received."""

    request: Request
    status: int
    headers: httpx.Headers
    body: bytes

    def status_line(self) -> str:
        """The status and the start of the body, for a report."""
        text = self.body.decode("utf-8", "replace").strip()
        return f"{self.status} {shorten(text)}" if text else str(self.status)


class _Failed(Exception):
    def __init__(self, request: str, at: str, expected: str, actual: str) -> None:
        super().__init__(at)
        self.request = request
        self.at = at
        self.expected = expected
        self.actual = actual


def _fail(request: object, at: str, expected: str, actual: str) -> NoReturn:
    raise _Failed(str(request), at, expected, actual)


def _differs(request: object, difference: Difference | None) -> None:
    if difference is not None:
        _fail(request, difference.at, difference.expected, difference.actual)


def describe(error: BaseException) -> str:
    """Name an exception for a report: its class and the SDK's attributes."""
    fields = [
        f"{name} {getattr(error, name)!r}"
        for name in ("status", "code", "parameter", "attempts")
        if hasattr(error, name)
    ]
    text = type(error).__name__
    if fields:
        text += ": " + ", ".join(fields)
    if not isinstance(error, APIError):
        text += f" ({shorten(str(error))})"
    return text


def _plain_json(value: Any) -> Any:
    """Copy a JSON value from the contract's files so `json.dumps` writes it."""
    if is_number(value) and not isinstance(value, int):
        whole = int(value)
        return whole if whole == value else float(value)
    if isinstance(value, Mapping):
        return {name: _plain_json(member) for name, member in value.items()}
    if isinstance(value, list):
        return [_plain_json(element) for element in value]
    return value


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _parsed(body: bytes) -> Body:
    try:
        return Body(load_json(body))
    except (ValueError, RecursionError) as error:
        return Body(error=f"not JSON: {error}")


class Runner:
    """Runs checks against the API at a base URL, one at a time.

    `transport`, when given, carries every request instead of the network,
    as `httpx.MockTransport` does in the runner's own tests.
    """

    def __init__(
        self, base_url: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.counts: dict[str, Counts] = {}
        self.http = httpx.Client(
            timeout=TIMEOUT,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        # Requests go as written, without asking for compression, as the API
        # runner sends them.
        del self.http.headers["Accept-Encoding"]
        prefix = httpx.URL(self.base_url).raw_path.decode("ascii").rstrip("/")
        self.recorder = Recorder(prefix)
        self.recording = httpx.Client(
            event_hooks={"request": [self.recorder]},
            trust_env=False,
            transport=transport,
        )
        self.clients = {
            credential_id: Client(
                api_key=CREDENTIALS[credential_id].api_key,
                base_url=self.base_url,
                timeout=TIMEOUT,
                retry=RetryPolicy(max_attempts=1),
                http_client=self.recording,
            )
            for credential_id in CUSTOMERS
        }
        self._step = 0

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
        self.recording.close()
        self.http.close()

    def run(self, check: Check) -> Failure | None:
        """Run one check, after a reset, and return its first difference."""
        self._step = 0
        counts = self.counts.setdefault(check.file, Counts())
        kinds: dict[str, Callable[[Check], None]] = {
            "query check": self._query_check,
            "page check": self._page_check,
            "position check": self._position_check,
            "read check": self._read_check,
            "apply check": self._apply_check,
            "scenario": self._scenario,
        }
        if check.kind != "scenario":
            counts.checks += 1
        try:
            kinds[check.kind](check)
        except _Failed as failed:
            return Failure(
                check,
                self._step,
                failed.request,
                failed.at,
                failed.expected,
                failed.actual,
            )
        return None

    # HTTP

    def _send(self, request: Request) -> _Exchange:
        headers = {}
        if request.authorization is not None:
            headers["Authorization"] = request.authorization
        elif request.credential is not None:
            headers["Authorization"] = (
                f"Bearer {CREDENTIALS[request.credential].api_key}"
            )
        content = None
        if request.body is not ABSENT:
            content = json.dumps(_plain_json(request.body)).encode()
            headers["Content-Type"] = "application/json"
        try:
            with self.http.stream(
                request.method,
                self.base_url + request.target(),
                headers=headers,
                content=content,
            ) as response:
                try:
                    body = b"".join(response.iter_raw())
                except httpx.StreamConsumed:
                    # A response made with its content, as httpx.MockTransport
                    # returns one, is read already, and was never encoded.
                    body = response.content
        except httpx.HTTPError as error:
            _fail(request, "response", "a response", f"{type(error).__name__}: {error}")
        return _Exchange(request, response.status_code, response.headers, body)

    def _test_action(self, method: str, path: str, body: object) -> None:
        request = Request(method, path, credential=CONTROL_CREDENTIAL, body=body)
        exchange = self._send(request)
        if exchange.status != 200:
            _fail(request, "status", "200", exchange.status_line())

    def _reset(self, clock: str | None) -> None:
        self._test_action(
            "POST", "/test/reset", {} if clock is None else {"clock": clock}
        )

    # SDK calls

    def _client(self) -> Client:
        return self.clients[DEFAULT_CREDENTIAL]

    def _sdk(self, call: str, function: Callable[[], _T]) -> _T:
        """Make an SDK call that must return, as every check's does."""
        try:
            return function()
        except Exception as error:  # any exception fails the check
            _fail(call, "result", "a result", describe(error))

    def _converted(self, call: object, result: object) -> Any:
        try:
            return to_json(result)
        except (TypeError, ValueError) as error:
            _fail(call, "result", "a result with a JSON form", str(error))

    # Checks

    @staticmethod
    def _query(check: Check) -> dict[str, Any]:
        return {
            name: value
            for name, value in check.body["query"].items()
            if value is not None
        }

    def _query_check(self, check: Check) -> None:
        if "contrast_available_as_of" in check.body:
            _fail(
                check,
                "contrast_available_as_of",
                "a check the SDK can make",
                "published_as_of, which this SDK does not have",
            )
        self._reset(check.body.get("clock"))
        query = self._query(check)
        arguments = ", ".join(f"{name}={value!r}" for name, value in query.items())
        call = f"client.observations.pages({arguments}) as {DEFAULT_CREDENTIAL}"
        pages = self._sdk(
            call, lambda: list(self._client().observations.pages(**query))
        )
        data = [self._converted(call, record) for page in pages for record in page.data]
        _differs(f"{call}, every page", match("data", check.body["expected"], data))
        call = f"client.observations.iterate({arguments}) as {DEFAULT_CREDENTIAL}"
        records = self._sdk(
            call, lambda: list(self._client().observations.iterate(**query))
        )
        iterated = [self._converted(call, record) for record in records]
        for index, (want, got) in enumerate(zip(data, iterated, strict=False)):
            if want != got:
                _fail(call, f"records[{index}]", render(want), render(got))
        if len(data) != len(iterated):
            _fail(call, "records", f"{len(data)} records", f"{len(iterated)} records")

    def _page_check(self, check: Check) -> None:
        self._reset(check.body.get("clock"))
        query = self._query(check)
        arguments = ", ".join(f"{name}={value!r}" for name, value in query.items())
        call = (
            f"client.observations.pages({arguments}, page_size=10) "
            f"as {DEFAULT_CREDENTIAL}"
        )
        pages = self._sdk(
            call,
            lambda: list(self._client().observations.pages(**query, page_size=10)),
        )
        for number, (page, want) in enumerate(zip(pages, check.pages, strict=False), 1):
            at = f"page {number}"
            if len(page.data) != want["count"]:
                _fail(call, at, f"{want['count']} records", f"{len(page.data)} records")
            if not page.data:
                continue
            first = self._converted(call, page.data[0])
            last = self._converted(call, page.data[-1])
            _differs(
                call,
                match(f"{at}, first record", {"observation_id": want["first"]}, first),
            )
            _differs(
                call,
                match(f"{at}, last record", {"observation_id": want["last"]}, last),
            )
        if len(pages) != len(check.pages):
            _fail(call, "pages", f"{len(check.pages)} pages", f"{len(pages)} pages")

    def _position_check(self, check: Check) -> None:
        self._reset(check.body["at"])
        call = f"client.datasets.get({STREAM_DATASET!r}) as {DEFAULT_CREDENTIAL}"
        dataset = self._sdk(call, lambda: self._client().datasets.get(STREAM_DATASET))
        expected = {"head_position": check.body["expected_position"]}
        _differs(call, match("result", expected, self._converted(call, dataset)))

    def _read_check(self, check: Check) -> None:
        self._reset(check.body.get("clock"))
        after = check.body["after_position"]
        limit = check.body["limit"]
        call = (
            f"client.changes.read({STREAM_DATASET!r}, after={after!r}, "
            f"limit={limit!r}) as {DEFAULT_CREDENTIAL}"
        )
        page = self._sdk(
            call,
            lambda: self._client().changes.read(
                STREAM_DATASET, after=after, limit=limit
            ),
        )
        expected = {
            "data": check.body["expected"],
            "next_position": check.body["expected_next_position"],
            "head_position": check.body["expected_head_position"],
        }
        _differs(call, match("result", expected, self._converted(call, page)))

    def _apply_check(self, check: Check) -> None:
        """Apply the stream to a local copy by precedence, as D2's copy does.

        The copy maps each `observation_id` to its current revision. A
        revision replaces the current one only when its `revision_number`
        is higher. The runner compares sequences to choose the revisions
        through each position, because it is checking the rule, not
        resuming a stream.
        """
        self._reset(check.body.get("clock"))
        call = (
            f"client.changes.pages({STREAM_DATASET!r}, after=0) as {DEFAULT_CREDENTIAL}"
        )
        pages = self._sdk(
            call, lambda: list(self._client().changes.pages(STREAM_DATASET, after=0))
        )
        revisions = [revision for page in pages for revision in page.data]
        copy: dict[str, Revision] = {}

        def apply(revision: Revision) -> None:
            current = copy.get(revision.observation_id)
            if current is None or revision.revision_number > current.revision_number:
                copy[revision.observation_id] = revision

        start = check.body["from_position"]
        through = check.body["through_position"]
        for revision in revisions:
            if revision.sequence <= start:
                apply(revision)
        for revision in revisions:
            if start < revision.sequence <= through:
                apply(revision)
        observation = check.body["observation_id"]
        current = copy.get(observation)
        _differs(
            f"{call}, applied through position {through}",
            match(
                f"current revision of {observation}",
                check.body["expected_current_revision_id"],
                None if current is None else current.revision_id,
            ),
        )

    def _scenario(self, check: Check) -> None:
        self._reset(check.body.get("clock"))
        bodies: dict[str, Body] = {}
        for number, step in enumerate(check.body["steps"], 1):
            self._step = number
            if "set_clock" in step:
                self._test_action("PUT", "/test/clock", {"now": step["set_clock"]})
            elif "set_credential" in step:
                changes = dict(step["set_credential"])
                credential_id = changes.pop("credential_id")
                path = f"/test/credentials/{quote(credential_id, safe='')}"
                self._test_action("PUT", path, changes)
            elif "reset" in step:
                self._test_action("POST", "/test/reset", step["reset"])
            else:
                self._request_step(check, step, bodies)

    # Request steps

    def _request_step(
        self, check: Check, step: Mapping[str, Any], bodies: dict[str, Body]
    ) -> None:
        written = step["request"]
        try:
            request = build(written, bodies)
        except (UnresolvedReference, InvalidStep) as error:
            method = written.get("method", "GET")
            _fail(
                f"{render(method)} {render(written['path'])}",
                "request",
                "every reference resolved, and members the format allows",
                str(error),
            )
        call = route(request, step["expect"])
        counts = self.counts[check.file]
        if call is None:
            counts.http += 1
            self._http_step(step, request, bodies)
        else:
            counts.sdk += 1
            self._sdk_step(step, call, request, bodies)

    def _status(
        self, request: object, expect: Mapping[str, Any], bodies: dict[str, Body]
    ) -> int:
        status = self._resolved(request, expect["status"], bodies)
        whole = None
        if is_number(status):
            try:
                whole = int(status)
            except (ValueError, OverflowError):  # NaN or infinity
                whole = None
        if whole is None or not equal_numbers(status, whole):
            _fail(
                request,
                "references",
                "a status that is an integer",
                f"{render(expect['status'])} is {render(status)}",
            )
        return whole

    def _resolved(self, request: object, value: Any, bodies: dict[str, Body]) -> Any:
        try:
            return resolve(value, bodies)
        except UnresolvedReference as error:
            _fail(request, "references", "every reference resolved", str(error))

    def _resolved_text(
        self, request: object, member: str, value: Any, bodies: dict[str, Body]
    ) -> str:
        text = self._resolved(request, value, bodies)
        if not isinstance(text, str):
            _fail(
                request,
                "references",
                f"{member} that is a string",
                f"{render(value)} is {json_type(text)}",
            )
        return text

    def _http_step(
        self, step: Mapping[str, Any], request: Request, bodies: dict[str, Body]
    ) -> None:
        exchange = self._send(request)
        if "id" in step:
            bodies[step["id"]] = _parsed(exchange.body)
        self._meets(exchange, step["expect"], bodies)

    def _meets(
        self, exchange: _Exchange, expect: Mapping[str, Any], bodies: dict[str, Body]
    ) -> None:
        """Check a response against `expect`, in the order of the expect table.

        Each member's references are resolved just before it is checked,
        so a reference to the step's own body cannot hide an earlier
        difference.
        """
        request = exchange.request
        status = self._status(request, expect, bodies)
        if status != exchange.status:
            _fail(request, "status", str(status), exchange.status_line())
        if "code" in expect:
            code = self._resolved_text(request, "code", expect["code"], bodies)
            content_type = exchange.headers.get("Content-Type", "")
            if _media_type(content_type) != "application/problem+json":
                _fail(
                    request,
                    "header Content-Type",
                    "application/problem+json",
                    render(content_type),
                )
            body = _parsed(exchange.body)
            if body.error is not None:
                _fail(request, "body", "a problem", body.error)
            _differs(
                request,
                match("body", {"code": code, "status": exchange.status}, body.value),
            )
        if "body" in expect:
            want = self._resolved(request, expect["body"], bodies)
            body = _parsed(exchange.body)
            if body.error is not None:
                _fail(request, "body", render(want), body.error)
            _differs(request, match("body", want, body.value))
        for name in sorted(expect.get("headers", {})):
            want_header = self._resolved_text(
                request, f"header {name}", expect["headers"][name], bodies
            )
            values = exchange.headers.get_list(name)
            if not values:
                _fail(request, f"header {name}", render(want_header), "no header")
            got = ", ".join(values)
            if name.lower() == "content-type":
                same = _media_type(got) == _media_type(want_header)
            else:
                same = got == want_header
            if not same:
                _fail(request, f"header {name}", render(want_header), render(got))
        if "body_sha256" in expect:
            digest = self._resolved_text(
                request, "body_sha256", expect["body_sha256"], bodies
            )
            got = hashlib.sha256(exchange.body).hexdigest()
            if got != digest:
                _fail(request, "body_sha256", digest, got)
        if "body_lines" in expect:
            lines_wanted = self._resolved(request, expect["body_lines"], bodies)
            if not isinstance(lines_wanted, list):
                _fail(
                    request,
                    "references",
                    "body_lines that is an array",
                    f"{render(expect['body_lines'])} is {json_type(lines_wanted)}",
                )
            _differs(request, match("body_lines", lines_wanted, self._lines(exchange)))

    def _lines(self, exchange: _Exchange) -> list[Any]:
        text = exchange.body.decode("utf-8", "replace")
        if not text.endswith("\n"):
            _fail(
                exchange.request,
                "body_lines",
                "lines of JSON, each ending with \\n",
                f"the body does not end with \\n: {render(shorten(text))}",
            )
        lines = []
        for number, line in enumerate(text[:-1].split("\n"), 1):
            parsed = _parsed(line.encode())
            if parsed.error is not None:
                _fail(
                    exchange.request,
                    "body_lines",
                    "lines of JSON, each ending with \\n",
                    f"line {number} is {parsed.error}",
                )
            lines.append(parsed.value)
        return lines

    def _sdk_step(
        self,
        step: Mapping[str, Any],
        call: SDKCall,
        request: Request,
        bodies: dict[str, Body],
    ) -> None:
        """Make an SDK step's call, and check what it sent, returned, or raised."""
        self.recorder.requests.clear()
        result: object = None
        error: Exception | None = None
        try:
            result = call(self.clients[call.credential])
        except Exception as raised:  # any exception is checked below
            error = raised
        self._check_recorded(call, request, error)
        expect = step["expect"]
        if error is None:
            converted = self._converted(call, result)
            if "id" in step:
                bodies[step["id"]] = Body(converted)
            status = self._status(call, expect, bodies)
            if status != 200:
                _fail(call, "status", str(status), f"a result: {render(converted)}")
            if "body" in expect:
                want = self._resolved(call, expect["body"], bodies)
                _differs(call, match("body", want, converted))
            return
        problem = getattr(error, "problem", None)
        if "id" in step:
            bodies[step["id"]] = (
                Body(dict(problem))
                if isinstance(error, APIError) and problem is not None
                else Body(error=f"the SDK raised {describe(error)}, with no problem")
            )
        status = self._status(call, expect, bodies)
        code = None
        if "code" in expect:
            code = self._resolved_text(call, "code", expect["code"], bodies)
        wanted = exception_class(status, code)
        if wanted is None or type(error) is not wanted:
            name = "a result" if wanted is None else wanted.__name__
            _fail(call, "exception", f"{name} for status {status}", describe(error))
        assert isinstance(error, APIError)
        if error.status != status:
            _fail(call, "status", str(status), str(error.status))
        if code is not None:
            if error.code != code:
                _fail(call, "code", render(code), render(error.code))
            _differs(
                call,
                match(
                    "problem",
                    {"code": code, "status": status},
                    None if problem is None else dict(problem),
                ),
            )
        if "body" in expect:
            want = self._resolved(call, expect["body"], bodies)
            _differs(
                call, match("problem", want, None if problem is None else dict(problem))
            )

    def _check_recorded(
        self, call: SDKCall, request: Request, error: Exception | None
    ) -> None:
        """The SDK must have sent one request: the step's, with exactly its query."""
        recorded = list(self.recorder.requests)
        if len(recorded) != 1:
            actual = f"{len(recorded)} requests"
            if error is not None:
                actual += f", and the SDK raised {describe(error)}"
            _fail(call, "requests sent", f"1 request: {request}", actual)
        (sent,) = recorded
        if (sent.method, sent.path) != (request.method, request.path):
            _fail(
                call,
                "request sent",
                f"{request.method} {request.path}",
                f"{sent.method} {sent.path}",
            )
        if sorted(sent.query) != sorted(request.pairs()):
            _fail(
                call,
                "query parameters sent",
                render(sorted(request.pairs())),
                render(sorted(sent.query)),
            )
