"""The SDK runner against a fake API built on `httpx.MockTransport`.

The fake answers the stage 1 endpoints from the vendored fixtures, as
`tests/support/fake_api.py` does, and test control with `200`. It refuses a
request without a customer key under `/v1`, except `/v1/meta`, and one
without the test-control key under `/test`. A test can replace the answer
to a method and path. The checks are the vendored files' own where the fake
can answer them, and otherwise small scenarios written here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from financial_data import (
    APIError,
    AuthenticationError,
    InvalidRequestError,
    PageTokenExpiredError,
    RateLimitError,
    ServerError,
)
from tests.conformance.routing import Request
from tests.conformance.runner import (
    Counts,
    Failure,
    Runner,
    exception_class,
)
from tests.conformance.suite import Check, select
from tests.support.api import CONTROL_KEY, CREDENTIALS, KEYS
from tests.support.fake_api import META, FakeAPI, json_response, problem
from tests.support.query import parse_query

BASE_URL = "http://api.test"
CUSTOMER_KEYS = {
    f"Bearer {c.api_key}" for c in CREDENTIALS.values() if c.kind == "customer"
}


class FakeServer:
    """The fake API and fake test control, behind one `httpx.MockTransport`."""

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix
        self.api = FakeAPI()
        self.sent: list[httpx.Request] = []
        self.control: list[tuple[str, str, Any]] = []
        self.answers: dict[
            tuple[str, str], Callable[[httpx.Request], httpx.Response]
        ] = {}

    def answer(self, method: str, path: str, response: httpx.Response) -> None:
        """Answer every request for a method and path with a copy of `response`."""
        self.answers[(method, path)] = lambda request: httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        path = request.url.raw_path.decode().partition("?")[0]
        assert path.startswith(self.prefix)
        path = path[len(self.prefix) :]
        if (request.method, path) in self.answers:
            return self.answers[(request.method, path)](request)
        authorization = request.headers.get("Authorization")
        if path.startswith("/test/"):
            if authorization != f"Bearer {CONTROL_KEY}":
                return json_response(problem(401, "unauthenticated"), 401)
            self.control.append((request.method, path, json.loads(request.content)))
            return json_response({"ok": True})
        if path != "/v1/meta" and authorization not in CUSTOMER_KEYS:
            return json_response(problem(401, "unauthenticated"), 401)
        if request.method != "GET":
            return json_response(problem(405, "method_not_allowed"), 405)
        if self.prefix:
            request = httpx.Request(
                request.method,
                request.url.copy_with(
                    raw_path=request.url.raw_path[len(self.prefix) :]
                ),
                headers=request.headers,
            )
        return self.api(request)

    @property
    def customer_requests(self) -> list[httpx.Request]:
        return [r for r in self.sent if not r.url.path.startswith("/test/")]


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
def runner(server: FakeServer) -> Iterator[Runner]:
    runner = Runner(BASE_URL, transport=httpx.MockTransport(server))
    yield runner
    runner.close()


def vendored(file: str, kind: str, name: str) -> Check:
    (check,) = [
        c for c in select(1).checks if (c.file, c.kind, c.name) == (file, kind, name)
    ]
    return check


def changed(check: Check, change: Callable[[dict[str, Any]], None]) -> Check:
    """A copy of a check, its body changed."""
    body = json.loads(json.dumps(check.body))
    change(body)
    return Check(check.file, check.kind, check.name, body, check.pages)


def scenario(*steps: dict[str, Any], clock: str | None = None) -> Check:
    body: dict[str, Any] = {"name": "written here", "steps": list(steps)}
    if clock is not None:
        body["clock"] = clock
    return Check("pagination", "scenario", "written here", body)


def get(
    path: str, *, expect: dict[str, Any], step_id: str | None = None, **request: Any
) -> dict[str, Any]:
    step: dict[str, Any] = {"request": {"path": path, **request}, "expect": expect}
    if step_id is not None:
        step["id"] = step_id
    return step


def failed(runner: Runner, check: Check) -> Failure:
    failure = runner.run(check)
    assert failure is not None, f"{check} passed"
    return failure


# The checks of the vendored files


def test_a_query_check_passes_and_resets_first(
    runner: Runner, server: FakeServer
) -> None:
    check = vendored(
        "august-2026-at-cutoffs", "query check", "research date used in the examples"
    )
    assert runner.run(check) is None
    assert server.control == [("POST", "/test/reset", {})]
    # pages() and iterate() each sent the query once.
    assert [dict(r.url.params) for r in server.customer_requests] == [
        {
            "series_id": "activity-index",
            "period_start": "2026-08-01",
            "period_end": "2026-09-01",
            "available_as_of": "2026-09-04T00:00:00Z",
        }
    ] * 2


def test_a_query_check_compares_values_exactly(runner: Runner) -> None:
    check = changed(
        vendored(
            "august-2026-at-cutoffs",
            "query check",
            "research date used in the examples",
        ),
        lambda body: body["expected"][0].update(value="102.40"),
    )
    failure = failed(runner, check)
    assert (failure.step, failure.at, failure.expected, failure.actual) == (
        0,
        "data[0].value",
        '"102.40"',
        '"102.4"',
    )
    assert failure.request == (
        "client.observations.pages(series_id='activity-index', "
        "period_start='2026-08-01', period_end='2026-09-01', "
        "available_as_of='2026-09-04T00:00:00Z') as cred_research, every page"
    )


def test_a_query_check_leaves_null_members_out(
    runner: Runner, server: FakeServer
) -> None:
    check = vendored("august-2026-at-cutoffs", "query check", "latest")
    assert check.body["query"]["available_as_of"] is None
    assert runner.run(check) is None
    assert "available_as_of" not in server.customer_requests[0].url.params


def test_iterate_must_return_the_same_records_as_pages(
    runner: Runner, server: FakeServer
) -> None:
    def change(document: dict[str, Any]) -> dict[str, Any]:
        # A copy: the fake's records are the fixture's own objects.
        first = {**document["data"][0], "value": "102.40"}
        return {**document, "data": [first, *document["data"][1:]]}

    server.api.rewrite(2, change)  # iterate()'s request
    check = vendored(
        "august-2026-at-cutoffs", "query check", "research date used in the examples"
    )
    failure = failed(runner, check)
    assert failure.at == "records[0]"
    assert failure.request.startswith("client.observations.iterate(")


def test_a_page_check_compares_counts_and_first_and_last_records(
    runner: Runner, server: FakeServer
) -> None:
    check = vendored("full-history", "page check", "latest")
    assert runner.run(check) is None
    assert [r.url.params.get("page_size") for r in server.customer_requests] == [
        "10"
    ] * 4
    pages = [dict(page) for page in check.pages]
    pages[3]["count"] = 3
    failure = failed(
        runner, Check(check.file, check.kind, check.name, check.body, tuple(pages))
    )
    assert (failure.at, failure.expected, failure.actual) == (
        "page 4",
        "3 records",
        "2 records",
    )
    pages = [dict(page) for page in check.pages]
    pages[1]["last"] = "obs_jul25"
    failure = failed(
        runner, Check(check.file, check.kind, check.name, check.body, tuple(pages))
    )
    assert failure.at == "page 2, last record.observation_id"
    failure = failed(
        runner, Check(check.file, check.kind, check.name, check.body, check.pages[:3])
    )
    assert (failure.at, failure.expected, failure.actual) == (
        "pages",
        "3 pages",
        "4 pages",
    )


def test_a_position_check_resets_to_its_instant(
    runner: Runner, server: FakeServer
) -> None:
    check = vendored("change-stream", "position check", "after every fixture revision")
    assert runner.run(check) is None
    assert server.control == [
        ("POST", "/test/reset", {"clock": "2026-10-01T00:00:00Z"})
    ]
    check = vendored(
        "change-stream", "position check", "two revisions with the same available_at"
    )
    failure = failed(runner, check)  # the fake is always at the default clock
    assert (failure.at, failure.expected, failure.actual) == (
        "result.head_position",
        "20",
        "37",
    )


@pytest.mark.parametrize(
    "name",
    [
        "withdrawal, then a tie in available_at",
        "revision delivered before the release it revises",
        "provider correction",
        "end of the stream",
    ],
)
def test_read_checks_pass(runner: Runner, server: FakeServer, name: str) -> None:
    check = vendored("change-stream", "read check", name)
    assert runner.run(check) is None
    (request,) = server.customer_requests
    expected = {"after": str(check.body["after_position"])}
    if check.body["limit"] is not None:
        expected["limit"] = str(check.body["limit"])
    assert dict(request.url.params) == expected


def test_a_read_check_compares_the_positions(runner: Runner) -> None:
    check = changed(
        vendored("change-stream", "read check", "provider correction"),
        lambda body: body.update(expected_next_position=31),
    )
    failure = failed(runner, check)
    assert (failure.at, failure.expected, failure.actual) == (
        "result.next_position",
        "31",
        "30",
    )


def test_an_apply_check_applies_by_precedence(runner: Runner) -> None:
    check = vendored("change-stream", "apply check", "apply by precedence, not arrival")
    assert runner.run(check) is None
    wrong = changed(
        check,
        lambda body: body.update(
            expected_current_revision_id=body["incorrect_if_applied_by_arrival"]
        ),
    )
    failure = failed(runner, wrong)
    assert (failure.at, failure.expected, failure.actual) == (
        "current revision of obs_nov25",
        '"rev_nov25_1"',
        '"rev_nov25_2"',
    )


def test_an_apply_check_stops_at_its_through_position(runner: Runner) -> None:
    # Through position 25, only rev_nov25_2 (sequence 25) has arrived.
    check = changed(
        vendored("change-stream", "apply check", "apply by precedence, not arrival"),
        lambda body: body.update(from_position=0, through_position=24),
    )
    failure = failed(runner, check)
    assert (failure.expected, failure.actual) == ('"rev_nov25_2"', "null")


def test_a_vendored_scenario_with_a_reference_passes(
    runner: Runner, server: FakeServer
) -> None:
    check = vendored(
        "pagination", "scenario", "the page holding the last result has no token"
    )
    assert runner.run(check) is None
    tokens = [r.url.params.get("page_token") for r in server.customer_requests]
    assert tokens == [None, "token-1", None]
    assert runner.counts == {"pagination": Counts(checks=0, sdk=3, http=0)}


def test_a_comparison_the_sdk_cannot_make_fails(runner: Runner) -> None:
    check = changed(
        vendored("august-2026-at-cutoffs", "query check", "latest"),
        lambda body: body.update(contrast_available_as_of=[]),
    )
    failure = failed(runner, check)
    assert failure.at == "contrast_available_as_of"


# SDK steps


OBSERVATIONS = {"series_id": "activity-index", "page_size": "10"}


def test_references_resolve_against_the_converted_result(
    runner: Runner, server: FakeServer
) -> None:
    check = scenario(
        get(
            "/v1/observations",
            query=OBSERVATIONS,
            step_id="p1",
            # A reference in expect may name the step itself.
            expect={
                "status": 200,
                "body": {"position": "${p1.position}", "data": [{}] * 10},
            },
        ),
        get(
            "/v1/observations",
            query={**OBSERVATIONS, "page_token": "${p1.next_page_token}"},
            expect={
                "status": 200,
                "body": {
                    "data": [
                        {"observation_id": "obs_nov24", "value": "${p1.data.0.value}"}
                    ]
                    + [{}] * 9,
                },
            },
        ),
    )
    failure = runner.run(check)
    assert failure is not None
    # obs_jan24's value, 97.1, differs from obs_nov24's.
    assert failure.step == 2
    assert failure.at == "body.data[0].value"
    assert failure.expected == '"97.1"'
    assert server.customer_requests[1].url.params["page_token"] == "token-1"


def test_a_reference_to_a_step_that_has_not_run_fails_the_step(runner: Runner) -> None:
    check = scenario(
        get("/v1/series", expect={"status": 200}),
        get(
            "/v1/observations",
            query={**OBSERVATIONS, "page_token": "${p9.next_page_token}"},
            expect={"status": 200},
        ),
    )
    failure = failed(runner, check)
    assert (failure.step, failure.at) == (2, "request")
    assert failure.actual == "${p9.next_page_token} names no step that has run"
    check = scenario(
        get("/v1/series", expect={"status": 200, "body": {"x": "${me.x}"}})
    )
    failure = failed(runner, check)
    assert (failure.step, failure.at) == (1, "references")


def test_an_sdk_step_passes_on_the_exception_the_contract_gives(
    runner: Runner, server: FakeServer
) -> None:
    check = scenario(
        get(
            "/v1/series/no-such-series",
            step_id="e",
            expect={
                "status": 404,
                "code": "not_found",
                "body": {"code": "not_found", "parameter": None},
            },
        ),
        get("/v1/series/${e.code}", expect={"status": 404, "code": "${e.code}"}),
    )
    assert runner.run(check) is None
    assert server.customer_requests[1].url.path == "/v1/series/not_found"


@pytest.mark.parametrize(
    ("expect", "at", "expected"),
    [
        (
            {"status": 404, "code": "unsupported_api_version"},
            "exception",
            "UnsupportedAPIVersionError for status 404",
        ),
        # Without a code, the class is the one the status alone gives.
        ({"status": 404}, "exception", "APIError for status 404"),
        ({"status": 200}, "exception", "a result for status 200"),
        ({"status": 400, "code": "not_found"}, "exception", "APIError for status 400"),
        (
            {"status": 404, "body": {"detail": "x"}},
            "exception",
            "APIError for status 404",
        ),
        (
            {"status": 404, "code": "not_found", "body": {"title": "Missing"}},
            "problem.title",
            '"Missing"',
        ),
    ],
)
def test_an_sdk_step_fails_on_another_exception(
    runner: Runner, expect: dict[str, Any], at: str, expected: str
) -> None:
    failure = failed(runner, scenario(get("/v1/series/no-such-series", expect=expect)))
    assert (failure.at, failure.expected) == (at, expected)
    if at == "exception":
        assert failure.actual == (
            "NotFoundError: status 404, code 'not_found', parameter None, attempts 1"
        )


def test_the_problems_status_must_equal_the_status(
    runner: Runner, server: FakeServer
) -> None:
    body = {**problem(404, "not_found"), "status": 400}
    server.answer("GET", "/v1/series/x", json_response(body, 404))
    failure = failed(
        runner,
        scenario(get("/v1/series/x", expect={"status": 404, "code": "not_found"})),
    )
    assert (failure.at, failure.expected, failure.actual) == (
        "problem.status",
        "404",
        "400",
    )


def test_an_sdk_step_that_returns_must_expect_200(runner: Runner) -> None:
    failure = failed(runner, scenario(get("/v1/series", expect={"status": 403})))
    assert (failure.at, failure.expected) == ("status", "403")
    assert failure.actual.startswith(
        'a result: {"data": [{"series_id": "activity-index"'
    )


def test_a_transport_error_from_the_sdk_fails_the_step(
    runner: Runner, server: FakeServer
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    server.answers[("GET", "/v1/series")] = refuse
    failure = failed(runner, scenario(get("/v1/series", expect={"status": 200})))
    assert failure.at == "exception"
    assert failure.actual.startswith(
        "TransportError: attempts 1 (ConnectError: refused"
    )


def test_the_sdk_must_send_exactly_the_steps_query(runner: Runner) -> None:
    def add(request: httpx.Request) -> None:
        request.url = request.url.copy_add_param("extra", "1")

    runner.recording.event_hooks["request"].insert(0, add)
    failure = failed(runner, scenario(get("/v1/series", expect={"status": 200})))
    assert (failure.at, failure.expected, failure.actual) == (
        "query parameters sent",
        "[]",
        '[["extra", "1"]]',
    )


def test_the_sdk_must_send_the_steps_path(runner: Runner) -> None:
    def move(request: httpx.Request) -> None:
        request.url = request.url.copy_with(path="/v1/datasets")

    runner.recording.event_hooks["request"].insert(0, move)
    failure = failed(runner, scenario(get("/v1/series", expect={"status": 200})))
    assert (failure.at, failure.expected, failure.actual) == (
        "request sent",
        "GET /v1/series",
        "GET /v1/datasets",
    )


def test_the_sdk_must_send_one_request(runner: Runner) -> None:
    def refuse(request: httpx.Request) -> None:
        raise RuntimeError("a hook refused it")

    runner.recording.event_hooks["request"].insert(0, refuse)
    failure = failed(runner, scenario(get("/v1/series", expect={"status": 200})))
    assert failure.at == "requests sent"
    assert failure.actual == (
        "0 requests, and the SDK raised RuntimeError (a hook refused it)"
    )


def test_each_customer_credential_has_its_own_client(
    runner: Runner, server: FakeServer
) -> None:
    check = scenario(
        get("/v1/series", credential="cred_unentitled", expect={"status": 200}),
        get("/v1/series", credential="cred_research", expect={"status": 200}),
        get("/v1/series", expect={"status": 200}),
    )
    assert runner.run(check) is None
    assert [r.headers["Authorization"] for r in server.customer_requests] == [
        "Bearer demo-unentitled-key",
        "Bearer demo-research-key",
        "Bearer demo-research-key",
    ]
    assert runner.counts["pagination"].sdk == 3


def test_a_base_url_with_a_path_prefix_is_left_out_of_the_recorded_path() -> None:
    server = FakeServer("/gateway")
    runner = Runner(f"{BASE_URL}/gateway/", transport=httpx.MockTransport(server))
    try:
        check = scenario(get("/v1/series/activity-index", expect={"status": 200}))
        assert runner.run(check) is None
        assert server.sent[-1].url.path == "/gateway/v1/series/activity-index"
    finally:
        runner.close()


# HTTP steps


def test_a_step_without_a_key_goes_over_http(
    runner: Runner, server: FakeServer
) -> None:
    check = scenario(
        get("/v1/meta", credential=None, expect={"status": 200, "body": META})
    )
    assert runner.run(check) is None
    (request,) = server.customer_requests
    assert "Authorization" not in request.headers
    assert "Accept-Encoding" not in request.headers
    assert runner.counts["pagination"] == Counts(checks=0, sdk=0, http=1)


def test_http_steps_send_the_request_as_written(
    runner: Runner, server: FakeServer
) -> None:
    check = scenario(
        get(
            "/v1/observations",
            query={"series_id": ["a b", "a+b"], "page_size": "010", "x": None},
            expect={"status": 200},
        ),
        get("/v1/series", authorization="Basic ZGVtbzpkZW1v", expect={"status": 401}),
        get(
            "/test/clock",
            method="PUT",
            credential="cred_test_control",
            body={"now": "2026-10-01T00:00:00Z"},
            expect={"status": 200},
        ),
    )
    assert runner.run(check) is None
    first, second, third = server.sent[1:]
    assert (
        first.url.raw_path
        == b"/v1/observations?page_size=010&series_id=a%20b&series_id=a%2Bb"
    )
    assert second.headers["Authorization"] == "Basic ZGVtbzpkZW1v"
    assert third.method == "PUT"
    assert third.headers["Content-Type"] == "application/json"
    assert json.loads(third.content) == {"now": "2026-10-01T00:00:00Z"}


def test_http_expectations_of_code_headers_digest_and_lines(
    runner: Runner, server: FakeServer
) -> None:
    lines = b'{"a": 1}\n{"b": [2]}\n'
    server.answer(
        "GET",
        "/v1/export",
        httpx.Response(
            200, headers={"Content-Type": "application/x-ndjson"}, content=lines
        ),
    )
    meta = json.dumps(META).encode()
    check = scenario(
        get(
            "/v1/series",
            credential=None,
            expect={
                "status": 401,
                "code": "unauthenticated",
                "headers": {"content-type": "application/problem+json; charset=utf-8"},
            },
        ),
        get(
            "/v1/meta",
            expect={
                "status": 200,
                "body_sha256": hashlib.sha256(meta).hexdigest(),
                "headers": {"Content-Type": "application/json"},
            },
        ),
        get(
            "/v1/export",
            expect={"status": 200, "body_lines": [{"a": 1}, {"b": [2]}]},
        ),
    )
    assert runner.run(check) is None


@pytest.mark.parametrize(
    ("expect", "at", "expected", "actual"),
    [
        ({"status": 404}, "status", "404", "200 " + json.dumps(META)),
        (
            {"status": 200, "code": "x"},
            "header Content-Type",
            "application/problem+json",
            '"application/json"',
        ),
        (
            {"status": 200, "body": {"api_version": "v2"}},
            "body.api_version",
            '"v2"',
            '"v1"',
        ),
        (
            {"status": 200, "headers": {"Allow": "GET"}},
            "header Allow",
            '"GET"',
            "no header",
        ),
        (
            {"status": 200, "headers": {"Content-Type": "text/plain"}},
            "header Content-Type",
            '"text/plain"',
            '"application/json"',
        ),
        ({"status": 200, "body_sha256": "00"}, "body_sha256", "00", None),
        ({"status": 200, "body_lines": [META]}, "body_lines", None, None),
        ({"status": "${m.x}"}, "references", "every reference resolved", None),
        ({"status": "200"}, "references", "a status that is an integer", None),
    ],
)
def test_http_expectations_that_fail(
    runner: Runner,
    expect: dict[str, Any],
    at: str,
    expected: str | None,
    actual: str | None,
) -> None:
    failure = failed(runner, scenario(get("/v1/meta", credential=None, expect=expect)))
    assert failure.at == at
    assert failure.request == "GET /v1/meta"
    if expected is not None:
        assert failure.expected == expected
    if actual is not None:
        assert failure.actual == actual


def test_a_code_must_match_the_problems_code_and_status(
    runner: Runner, server: FakeServer
) -> None:
    check = scenario(
        get("/v1/series", credential=None, expect={"status": 401, "code": "x"})
    )
    failure = failed(runner, check)
    assert (failure.at, failure.expected, failure.actual) == (
        "body.code",
        '"x"',
        '"unauthenticated"',
    )
    server.answer(
        "GET",
        "/v1/nothing",
        json_response({**problem(404, "not_found"), "status": 400}, 404),
    )
    check = scenario(get("/v1/nothing", expect={"status": 404, "code": "not_found"}))
    failure = failed(runner, check)
    assert (failure.at, failure.expected, failure.actual) == (
        "body.status",
        "404",
        "400",
    )


def test_a_reference_to_a_body_that_is_not_json_fails(
    runner: Runner, server: FakeServer
) -> None:
    server.answer("GET", "/v1/text", httpx.Response(200, text="plain"))
    check = scenario(
        get("/v1/text", step_id="t", expect={"status": 200}),
        get("/v1/series/${t.x}", expect={"status": 200}),
    )
    failure = failed(runner, check)
    assert failure.step == 2
    assert failure.actual.startswith("${t.x}: step t has no body: not JSON")


def test_a_failed_request_over_http_reports_it(
    runner: Runner, server: FakeServer
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    server.answers[("GET", "/v1/meta")] = refuse
    failure = failed(
        runner, scenario(get("/v1/meta", credential=None, expect={"status": 200}))
    )
    assert (failure.at, failure.expected, failure.actual) == (
        "response",
        "a response",
        "ConnectError: refused",
    )


# Test actions and the scenario


def test_test_actions_use_the_test_control_key(
    runner: Runner, server: FakeServer
) -> None:
    check = scenario(
        {"set_clock": "2026-10-01T00:00:00Z"},
        {"set_credential": {"credential_id": "cred_research", "active": False}},
        {"reset": {"clock": "2025-06-20T16:01:09Z"}},
        clock="2026-09-10T12:30:20Z",
    )
    assert runner.run(check) is None
    assert server.control == [
        ("POST", "/test/reset", {"clock": "2026-09-10T12:30:20Z"}),
        ("PUT", "/test/clock", {"now": "2026-10-01T00:00:00Z"}),
        ("PUT", "/test/credentials/cred_research", {"active": False}),
        ("POST", "/test/reset", {"clock": "2025-06-20T16:01:09Z"}),
    ]
    assert {r.headers["Authorization"] for r in server.sent} == {
        f"Bearer {CONTROL_KEY}"
    }


def test_a_test_action_must_answer_200(runner: Runner, server: FakeServer) -> None:
    server.answer(
        "PUT", "/test/clock", json_response(problem(409, "clock_backwards"), 409)
    )
    check = scenario(
        {"set_clock": "2020-01-01T00:00:00Z"}, get("/v1/series", expect={"status": 200})
    )
    failure = failed(runner, check)
    assert (failure.step, failure.request, failure.at, failure.expected) == (
        1,
        "PUT /test/clock",
        "status",
        "200",
    )
    assert failure.actual.startswith('409 {"status": 409, "code": "clock_backwards"')
    assert server.customer_requests == []  # the first failing step ends the scenario


def test_a_failed_reset_is_step_0(runner: Runner, server: FakeServer) -> None:
    server.answer(
        "POST", "/test/reset", json_response(problem(400, "invalid_parameter"), 400)
    )
    failure = failed(runner, scenario(get("/v1/series", expect={"status": 200})))
    assert (failure.step, failure.request) == (0, "POST /test/reset")
    assert str(failure).splitlines()[1] == "POST /test/reset"


def test_counts_are_kept_per_file(runner: Runner) -> None:
    runner.run(vendored("august-2026-at-cutoffs", "query check", "latest"))
    runner.run(vendored("change-stream", "read check", "end of the stream"))
    runner.run(
        scenario(
            get("/v1/series", expect={"status": 200}),
            get("/v1/meta", credential=None, expect={"status": 200}),
            get("/v1/series", expect={"status": 200}),
        )
    )
    assert runner.counts == {
        "august-2026-at-cutoffs": Counts(checks=1, sdk=0, http=0),
        "change-stream": Counts(checks=1, sdk=0, http=0),
        "pagination": Counts(checks=0, sdk=2, http=1),
    }


# Reports


def test_a_report_names_the_check_step_request_and_difference(runner: Runner) -> None:
    failure = failed(
        runner, scenario(get("/v1/series/no-such-series", expect={"status": 200}))
    )
    assert str(failure) == "\n".join(
        [
            'pagination: scenario "written here"',
            "step 1: client.series.get('no-such-series') as cred_research",
            "at exception",
            "  expected: a result for status 200",
            "  actual:   NotFoundError: status 404, code 'not_found', parameter None, "
            "attempts 1",
        ]
    )


def test_no_report_contains_a_key(runner: Runner, server: FakeServer) -> None:
    server.answer(
        "GET",
        "/v1/meta",
        httpx.Response(500, text=f"Bearer {CONTROL_KEY} and demo-unentitled-key"),
    )
    reports = [
        failed(
            runner,
            scenario(get("/v1/series/demo-research-key", expect={"status": 200})),
        ),
        failed(
            runner, scenario(get("/v1/meta", credential=None, expect={"status": 200}))
        ),
        failed(
            runner,
            scenario(
                get(
                    "/v1/observations",
                    query={"series_id": "x", "page_token": "demo-research-key"},
                    expect={"status": 200, "body": {"x": "demo-test-control-key"}},
                )
            ),
        ),
    ]
    for report in map(str, reports):
        assert "[redacted]" in report
        for key in KEYS:
            assert key not in report


def test_the_exception_table_follows_the_client_contract() -> None:
    assert exception_class(400, "invalid_parameter") is InvalidRequestError
    assert exception_class(410, "page_token_expired") is PageTokenExpiredError
    assert exception_class(401, "anything") is AuthenticationError
    assert exception_class(429, None) is RateLimitError
    assert exception_class(503, "internal") is ServerError
    assert exception_class(404, None) is APIError
    assert exception_class(410, "position_ahead") is APIError
    assert exception_class(200, None) is None
    assert exception_class(302, None) is None


def test_the_recorded_query_keeps_plus_and_decodes_escapes() -> None:
    assert parse_query("a=1%2B2&b=x+y&c=&d&e=%ED%A0%80") == (
        ("a", "1+2"),
        ("b", "x+y"),
        ("c", ""),
        ("d", ""),
        ("e", "\ud800"),
    )
    assert parse_query("") == ()


def test_a_request_line_names_method_and_target() -> None:
    assert str(Request("GET", "/v1/series")) == "GET /v1/series"
