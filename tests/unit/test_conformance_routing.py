"""Which request steps the SDK runner sends through the SDK, and the calls it makes.

The table below classifies every step of every stage 1 scenario, written
from the rule in `spec/conformance.md` rather than from the code: `S` for a
request step sent through the SDK, `H` for one sent over HTTP, and `-` for
a test action.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conformance.matching import Body
from tests.conformance.routing import (
    ABSENT,
    InvalidStep,
    Parameter,
    Request,
    SDKCall,
    build,
    canonical_integer,
    route,
)
from tests.conformance.suite import Check, select

ROUTES = {
    "change-stream": {
        "an event is retained until 1,095 days after it became available": "S",
        "a position expires when the next event is past retention": "SS",
        "a caught-up position does not expire": "SS",
        "a position ahead of the stream is refused": "SS",
        "events that share an available_at arrive together": "S",
    },
    "simulated-clock": {
        # No key.
        "meta reports the clock": "H",
        "a revision after the clock is invisible": "SSS",
        "moving the clock reveals the revision at its available_at": "-S-SS",
        # /test paths with the test-control key.
        "the clock does not move backwards": "HHH",
        "the clock moves to its latest value and no further": "HH",
        "a reset without a clock restores the start": "-HS",
        "a reset can move the clock back": "-SS",
        "a reset accepts the latest clock and no later": "HH",
        "before the first revision": "SSS",
        "a cutoff after the clock is refused": "SS",
    },
    "pagination": {
        "pages keep their snapshot while a revision arrives": "SSS-SS",
        "a cutoff cannot be dropped when resuming": "SS",
        "a filter cannot change when resuming": "SS",
        "the page size cannot change when resuming": "SSS",
        # cred_unentitled is a customer credential, with its own client.
        "a token belongs to its credential": "SS",
        "a snapshot expires after an hour": "S-S-S",
        "a reset invalidates tokens": "S-S",
        "a malformed token is refused": "S",
        "the page holding the last result has no token": "SSS",
        "an empty result has no token": "S",
    },
    "access-control": {
        "meta needs no key": "H",
        # No key, and expected headers.
        "a request without a key is refused": "HHH",
        # Raw Authorization headers.
        "unknown keys and malformed headers are refused": "HHHH",
        "the scheme name is case-insensitive": "H",
        # The test-control key under /v1, and a customer key under /test.
        "keys are accepted only by their own kind": "HH",
        "the catalog is visible without entitlement": "SSSSSS",
        "data is refused without entitlement": "SS",
        "an empty result is not a refusal": "S",
        "unknown series and datasets are not found": "SSSS",
        "a revoked key is refused on a resumed page": "S-SS",
        "a removed entitlement is enforced on a resumed page": "S-SSS",
        "a granted entitlement takes effect at once": "S-S",
        "a reset restores credentials": "--S",
        "test control cannot change its own credential": "HHH",
    },
    "request-errors": {
        # Expected headers.
        "error bodies": "H",
        # Unknown parameters, the last without a key.
        "unknown parameters are refused": "HHH",
        # An unknown parameter, and an unknown path.
        "a stage 1 server refuses stage 2 parameters and paths": "HH",
        # Missing required parameters.
        "required parameters": "HH",
        # A repeated parameter; an empty string is sent as given.
        "repeated and empty parameters": "HS",
        # Malformed timestamps are strings, which the SDK sends as given.
        "timestamps must have the exact format": "SSSSSSSS",
        "dates must be valid": "SSS",
        "a period range must not be empty or reversed": "SSSS",
        "a period filter matches only periods within the range": "SS",
        "a cutoff may equal the clock but not pass it": "SS",
        # 0, 1001, -1, and 1000 are canonical; ten, 010, +5, 1.5, and 01 are not.
        "page sizes and limits": "SSHHSHSSSSSHH",
        # /test, with bodies.
        "request bodies": "HH",
        "a position ahead of the stream": "SS",
        # Other versions, unknown paths, a trailing slash, and /test.
        "unsupported versions and unknown paths": "HHHHH",
        # Other methods.
        "methods": "HHH",
        # No key, another version, another method; then customer requests.
        "errors are reported in a fixed order": "HHHSSSS",
    },
}


def _scenarios() -> list[Check]:
    return [check for check in select(1).checks if check.kind == "scenario"]


def _routes(check: Check) -> str:
    """Classify each step, resolving each reference to a stand-in page token."""
    bodies: dict[str, Body] = {}
    routes = ""
    for step in check.body["steps"]:
        if "request" not in step:
            routes += "-"
            continue
        request = build(step["request"], bodies)
        routes += "H" if route(request, step["expect"]) is None else "S"
        if "id" in step:
            bodies[step["id"]] = Body({"next_page_token": f"token-{step['id']}"})
    return routes


def test_the_table_names_every_stage_1_scenario() -> None:
    named = {(file, name) for file, scenarios in ROUTES.items() for name in scenarios}
    assert {(check.file, check.name) for check in _scenarios()} == named


@pytest.mark.parametrize("check", _scenarios(), ids=lambda check: check.id)
def test_each_step_goes_where_the_conformance_document_says(check: Check) -> None:
    assert _routes(check) == ROUTES[check.file][check.name]


def _step(file: str, scenario: str, number: int) -> dict[str, Any]:
    (check,) = [c for c in _scenarios() if (c.file, c.name) == (file, scenario)]
    step: dict[str, Any] = check.body["steps"][number - 1]
    return step


def _call(file: str, scenario: str, number: int, **bodies: Body) -> SDKCall | None:
    step = _step(file, scenario, number)
    return route(build(step["request"], bodies), step["expect"])


def test_a_malformed_timestamp_goes_through_the_sdk_as_written() -> None:
    call = _call("request-errors", "timestamps must have the exact format", 5)
    assert call == SDKCall(
        "observations.page",
        (),
        (("series_id", "activity-index"), ("available_as_of", "2026-09-04 00:00:00Z")),
        "cred_research",
    )


def test_page_size_010_goes_over_http() -> None:
    step = _step("request-errors", "page sizes and limits", 4)
    assert step["request"]["query"]["page_size"] == "010"
    assert _call("request-errors", "page sizes and limits", 4) is None


def test_zero_and_negative_integers_reach_the_sdk_as_ints() -> None:
    call = _call("request-errors", "page sizes and limits", 8)
    assert call == SDKCall(
        "changes.read",
        ("core-indicators",),
        (("after", 0), ("limit", 0)),
        "cred_research",
    )
    call = _call("request-errors", "page sizes and limits", 11)
    assert call is not None
    assert call.kwargs == (("after", -1),)


def test_a_resumed_page_sends_the_referenced_token_with_the_other_arguments() -> None:
    call = _call(
        "pagination",
        "a token belongs to its credential",
        2,
        p1=Body({"next_page_token": "tok"}),
    )
    assert call == SDKCall(
        "observations.page",
        (),
        (("series_id", "activity-index"), ("page_size", 10), ("page_token", "tok")),
        "cred_unentitled",
    )


def test_path_parameters_are_passed_positionally() -> None:
    call = _call("access-control", "unknown series and datasets are not found", 2)
    assert call == SDKCall("series.get", ("no-such-series",), (), "cred_research")
    call = _call("access-control", "unknown series and datasets are not found", 3)
    assert call == SDKCall("datasets.get", ("no-such-dataset",), (), "cred_research")
    call = _call("access-control", "the catalog is visible without entitlement", 1)
    assert call == SDKCall("datasets.list", (), (), "cred_unentitled")


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("0", 0),
        ("-1", -1),
        ("10", 10),
        ("1000", 1000),
        ("010", None),
        ("+5", None),
        ("1.5", None),
        ("ten", None),
        ("-0", None),
        (" 5", None),
        ("1_0", None),
        ("\uff15", None),  # a fullwidth 5
        ("", None),
        ("9" * 5000, None),
    ],
)
def test_canonical_integers(text: str, value: int | None) -> None:
    assert canonical_integer(text) == value


def _request(
    path: str,
    *query: Parameter,
    method: str = "GET",
    credential: str | None = "cred_research",
    authorization: str | None = None,
) -> Request:
    return Request(method, path, query, credential, authorization)


def _p(name: str, *values: str, array: bool = False) -> Parameter:
    return Parameter(name, values, array)


OBSERVATIONS = (_p("series_id", "activity-index"),)


@pytest.mark.parametrize(
    "request_",
    [
        _request("/v1/observations", *OBSERVATIONS, method="POST"),
        _request("/v1/observations", *OBSERVATIONS, method="get"),
        _request("/v1/observations/", *OBSERVATIONS),
        _request("/v1/series/"),
        _request("/v1/series/."),
        _request("/v1/series/.."),
        _request("/v1/series/a/b"),
        _request("/v1/datasets//changes", _p("after", "0")),
        _request("/v1/datasets/./changes", _p("after", "0")),
        _request("/v2/series"),
        _request("/v1/revisions", *OBSERVATIONS),
        _request("/test/clock"),
        _request("/v1/series", credential=None),
        _request("/v1/series", credential="cred_test_control"),
        _request("/v1/series", authorization="Bearer demo-research-key"),
        _request("/v1/series", _p("dataset_id", "core-indicators")),
        _request("/v1/meta", _p("verbose", "true")),
        _request("/v1/observations"),
        _request("/v1/observations", _p("series_id")),
        _request("/v1/observations", _p("series_id", "a", "a", array=True)),
        _request("/v1/observations", _p("series_id", "a", array=True)),
        _request("/v1/observations", _p("series_id", array=True)),
        _request("/v1/observations", *OBSERVATIONS, _p("published_as_of", "x")),
        _request("/v1/observations", *OBSERVATIONS, _p("page_size", "010")),
        _request("/v1/datasets/core-indicators/changes"),
        _request("/v1/datasets/core-indicators/changes", _p("after", "1.5")),
        _request(
            "/v1/datasets/core-indicators/changes", _p("after", "0"), _p("limit", "+1")
        ),
        _request(
            "/v1/datasets/core-indicators/changes",
            _p("after", "0"),
            _p("page_size", "1"),
        ),
    ],
    ids=str,
)
def test_requests_that_go_over_http(request_: Request) -> None:
    assert route(request_, {"status": 200}) is None


@pytest.mark.parametrize("member", ["headers", "body_sha256", "body_lines"])
def test_expectations_of_the_raw_response_go_over_http(member: str) -> None:
    request = _request("/v1/series")
    assert route(request, {"status": 200}) is not None
    assert route(request, {"status": 200, member: {}}) is None


@pytest.mark.parametrize(
    ("request_", "call"),
    [
        (_request("/v1/meta"), SDKCall("meta", (), (), "cred_research")),
        (_request("/v1/series"), SDKCall("series.list", (), (), "cred_research")),
        (
            _request("/v1/series/demo%20x"),
            SDKCall("series.get", ("demo%20x",), (), "cred_research"),
        ),
        (
            # A null member is left out of the call.
            _request("/v1/observations", *OBSERVATIONS, _p("period_end")),
            SDKCall(
                "observations.page",
                (),
                (("series_id", "activity-index"),),
                "cred_research",
            ),
        ),
        (
            _request(
                "/v1/observations",
                _p("page_token", "t"),
                _p("page_size", "5"),
                _p("available_as_of", ""),
                _p("period_end", "b"),
                _p("period_start", "a"),
                _p("series_id", ""),
            ),
            SDKCall(
                "observations.page",
                (),
                (
                    ("series_id", ""),
                    ("period_start", "a"),
                    ("period_end", "b"),
                    ("available_as_of", ""),
                    ("page_size", 5),
                    ("page_token", "t"),
                ),
                "cred_research",
            ),
        ),
        (
            _request("/v1/datasets/x/changes", _p("limit", "2"), _p("after", "-1")),
            SDKCall(
                "changes.read", ("x",), (("after", -1), ("limit", 2)), "cred_research"
            ),
        ),
    ],
    ids=lambda value: str(value) if isinstance(value, Request) else "",
)
def test_requests_that_go_through_the_sdk(request_: Request, call: SDKCall) -> None:
    assert route(request_, {"status": 200}) == call


def test_the_sdk_call_describes_itself_with_its_credential() -> None:
    call = SDKCall(
        "observations.page",
        (),
        (("series_id", "activity-index"), ("page_size", 10)),
        "cred_research",
    )
    assert str(call) == (
        "client.observations.page(series_id='activity-index', page_size=10) "
        "as cred_research"
    )
    call = SDKCall("changes.read", ("core-indicators",), (("after", 0),), "x")
    assert str(call) == "client.changes.read('core-indicators', after=0) as x"


# Building a request


def test_build_defaults_to_get_and_the_default_credential() -> None:
    request = build({"path": "/v1/series"}, {})
    assert request == Request("GET", "/v1/series", (), "cred_research", None, ABSENT)


def test_build_resolves_references_in_every_member() -> None:
    bodies = {
        "a": Body(
            {
                "method": "PUT",
                "path": "/test/clock",
                "token": "tok",
                "n": 37,
                "none": None,
                "cred": "cred_unentitled",
                "list": ["x", 2],
            }
        )
    }
    request = build(
        {
            "method": "${a.method}",
            "path": "${a.path}",
            "query": {
                "page_token": "${a.token}",
                "after": "${a.n}",
                "gone": "${a.none}",
                "many": "${a.list}",
                "text": "n=${a.n}",
            },
            "credential": "${a.cred}",
            "body": {"now": "${a.token}"},
        },
        bodies,
    )
    assert request.method == "PUT"
    assert request.path == "/test/clock"
    assert request.query == (
        Parameter("after", ("37",)),
        Parameter("gone", ()),
        Parameter("many", ("x", "2"), array=True),
        Parameter("page_token", ("tok",)),
        Parameter("text", ("n=37",)),
    )
    assert request.credential == "cred_unentitled"
    assert request.body == {"now": "tok"}


def test_a_null_credential_sends_no_key_and_authorization_replaces_it() -> None:
    assert build({"path": "/v1/meta", "credential": None}, {}).credential is None
    request = build(
        {
            "path": "/v1/series",
            "credential": "cred_unentitled",
            "authorization": "Basic x",
        },
        {},
    )
    assert request.authorization == "Basic x"
    assert route(request, {"status": 401}) is None


@pytest.mark.parametrize(
    ("written", "message"),
    [
        ({"path": "v1/series"}, "does not start with /"),
        ({"path": "/v1/series", "method": ""}, "is empty"),
        ({"path": "/v1/series", "credential": "cred_nobody"}, "not in fixtures"),
        ({"path": "/v1/series", "credential": 3}, "must be a string or null"),
        ({"path": "/v1/series", "query": {"a": True}}, "is a boolean"),
        ({"path": "/v1/series", "query": {"a": [{}]}}, "an array holding an object"),
        ({"path": "/v1/series", "query": []}, "is not an object"),
    ],
)
def test_build_refuses_members_the_format_does_not_allow(
    written: dict[str, Any], message: str
) -> None:
    with pytest.raises(InvalidStep, match=message):
        build(written, {})


def test_a_request_writes_its_target_as_sent() -> None:
    request = _request(
        "/v1/observations",
        _p("series_id", "a b"),
        _p("available_as_of", "2026-09-04T00:00:00+00:00"),
        _p("x", "1", "2", array=True),
    )
    assert request.pairs() == [
        ("series_id", "a b"),
        ("available_as_of", "2026-09-04T00:00:00+00:00"),
        ("x", "1"),
        ("x", "2"),
    ]
    assert str(request) == (
        "GET /v1/observations?series_id=a%20b"
        "&available_as_of=2026-09-04T00%3A00%3A00%2B00%3A00&x=1&x=2"
    )
