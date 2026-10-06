"""Choosing an exception for a response, and filling its attributes and `str`."""

from __future__ import annotations

import json
import traceback
from collections.abc import Mapping
from typing import Any

import pytest

from financial_data import (
    APIError,
    AuthenticationError,
    InvalidRequestError,
    NotEntitledError,
    NotFoundError,
    PageTokenError,
    PageTokenExpiredError,
    PositionAheadError,
    PositionExpiredError,
    RateLimitError,
    ServerError,
    UnexpectedResponseError,
    UnsupportedAPIVersionError,
)
from financial_data._decode import (
    InvalidResponse,
    decode_change_page,
    decode_object,
)
from financial_data._errors import (
    _redact_json,
    api_error,
    redact,
    unexpected_response,
)

KEY = "demo-research-key"
PROBLEM_JSON = "application/problem+json"
PATH = "/v1/observations?series_id=activity-index"


def problem_body(status: int, code: str, **members: Any) -> bytes:
    body = {
        "status": status,
        "code": code,
        "title": "A title",
        "detail": "A detail.",
        "parameter": None,
        **members,
    }
    return json.dumps(body).encode()


def error_for(
    status: int,
    body: bytes | None = None,
    content_type: str | None = PROBLEM_JSON,
    **options: Any,
) -> APIError:
    arguments: dict[str, Any] = {
        "retry_after": None,
        "request_id": None,
        "method": "GET",
        "path": PATH,
        "attempts": 1,
        "secret": KEY,
    }
    arguments.update(options)
    return api_error(status=status, content_type=content_type, body=body, **arguments)


# The exceptions table: each row, chosen by the code and the status together.
ROWS = [
    (400, "unknown_parameter", InvalidRequestError),
    (400, "missing_parameter", InvalidRequestError),
    (400, "invalid_parameter", InvalidRequestError),
    (400, "conflicting_cutoffs", InvalidRequestError),
    (400, "cutoff_in_future", InvalidRequestError),
    (400, "invalid_page_token", PageTokenError),
    (400, "page_token_mismatch", PageTokenError),
    (410, "page_token_expired", PageTokenExpiredError),
    (400, "position_ahead", PositionAheadError),
    (410, "position_expired", PositionExpiredError),
    (401, "unauthenticated", AuthenticationError),
    (401, "something_new", AuthenticationError),
    (403, "not_entitled", NotEntitledError),
    (404, "not_found", NotFoundError),
    (404, "unsupported_api_version", UnsupportedAPIVersionError),
    (429, "rate_limited", RateLimitError),
    (429, "something_new", RateLimitError),
    (500, "internal", ServerError),
    (502, "bad_gateway", ServerError),
    (503, "something_new", ServerError),
    (599, "internal", ServerError),
]


@pytest.mark.parametrize(("status", "code", "exception"), ROWS)
def test_problem_response_raises_its_rows_exception(
    status: int, code: str, exception: type[APIError]
) -> None:
    body = problem_body(status, code, parameter="page_token")
    error = error_for(status, body, retry_after=2.0, request_id="req_1", attempts=3)
    assert type(error) is exception
    assert error.status == status
    assert error.code == code
    assert error.title == "A title"
    assert error.detail == "A detail."
    assert error.parameter == "page_token"
    assert error.problem == json.loads(body)
    assert error.retry_after == 2.0
    assert error.request_id == "req_1"
    assert (error.method, error.path, error.attempts) == ("GET", PATH, 3)


# A known code with a status its row does not have claims nothing more than
# the status says.
@pytest.mark.parametrize(
    ("status", "code", "exception"),
    [
        (400, "not_found", APIError),
        (404, "not_entitled", APIError),
        (403, "page_token_expired", APIError),
        (410, "invalid_page_token", APIError),
        (400, "position_expired", APIError),
        (404, "page_token_mismatch", APIError),
        (405, "method_not_allowed", APIError),
        (410, "export_expired", APIError),
        (409, "clock_backwards", APIError),
        (500, "not_found", ServerError),
        (429, "not_entitled", RateLimitError),
        (401, "not_found", AuthenticationError),
    ],
)
def test_code_with_another_status_falls_back_to_the_status(
    status: int, code: str, exception: type[APIError]
) -> None:
    error = error_for(status, problem_body(status, code))
    assert type(error) is exception
    assert error.code == code


def test_unknown_code_raises_exactly_api_error() -> None:
    body = problem_body(400, "something_new", parameter="series_id")
    error = error_for(400, body)
    assert type(error) is APIError
    assert (error.status, error.code, error.parameter) == (
        400,
        "something_new",
        "series_id",
    )


@pytest.mark.parametrize(
    ("status", "exception"),
    [
        (400, APIError),
        (401, AuthenticationError),
        (403, APIError),
        (404, APIError),
        (405, APIError),
        (410, APIError),
        (418, APIError),
        (429, RateLimitError),
        (499, APIError),
        (500, ServerError),
        (502, ServerError),
        (503, ServerError),
        (504, ServerError),
        (599, ServerError),
    ],
)
def test_response_without_a_problem_body_raises_by_status(
    status: int, exception: type[APIError]
) -> None:
    for content_type, body in [
        ("text/html", b"<html><body>Not Found</body></html>"),
        ("text/plain", b"Service Unavailable"),
        (None, b""),
        (PROBLEM_JSON, None),  # a response a caller's event hook refused
    ]:
        error = error_for(
            status, body, content_type, retry_after=2.0, request_id="req_1", attempts=3
        )
        assert type(error) is exception
        # The headers' values are kept without a problem body, as for a 429
        # or a 503 from a proxy.
        assert (error.retry_after, error.request_id) == (2.0, "req_1")
        assert (error.method, error.path, error.attempts) == ("GET", PATH, 3)
        assert error.status == status
        assert error.code is None
        assert error.title is None
        assert error.detail is None
        assert error.parameter is None
        assert error.problem is None


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        # The body of a problem, but not marked as one.
        ("application/json", problem_body(403, "not_entitled")),
        ("text/plain", problem_body(403, "not_entitled")),
        (None, problem_body(403, "not_entitled")),
        # Marked as a problem, but not a JSON object.
        (PROBLEM_JSON, b'[{"code": "not_entitled"}]'),
        (PROBLEM_JSON, b'"not_entitled"'),
        (PROBLEM_JSON, b"null"),
        (PROBLEM_JSON, b'{"code": "not_entitled"'),
        (PROBLEM_JSON, b'{"code": "not_entitled", "n": NaN}'),
        (PROBLEM_JSON, b"<html>Forbidden</html>"),
        (PROBLEM_JSON, b""),
    ],
)
def test_response_that_is_not_a_problem_response(
    content_type: str | None, body: bytes
) -> None:
    error = error_for(403, body, content_type)
    assert type(error) is APIError
    assert error.code is None
    assert error.problem is None


@pytest.mark.parametrize(
    "content_type",
    [
        "application/problem+json; charset=utf-8",
        "Application/Problem+JSON",
        " application/problem+json ",
    ],
)
def test_problem_content_type_ignores_case_and_parameters(content_type: str) -> None:
    error = error_for(404, problem_body(404, "not_found"), content_type)
    assert type(error) is NotFoundError


def test_problem_members_that_are_not_strings_are_none() -> None:
    body = json.dumps(
        {"status": 404, "code": 404, "title": None, "detail": ["x"], "parameter": 1}
    ).encode()
    error = error_for(404, body)
    assert type(error) is APIError
    assert (error.code, error.title, error.detail, error.parameter) == (
        None,
        None,
        None,
        None,
    )
    assert error.problem == json.loads(body)


def test_problem_without_members_keeps_its_body() -> None:
    error = error_for(500, b"{}")
    assert type(error) is ServerError
    assert error.code is None
    assert error.problem == {}


def test_problem_keeps_members_the_sdk_does_not_know() -> None:
    body = problem_body(400, "invalid_parameter", hint={"try": ["2026-09-04"]})
    error = error_for(400, body)
    assert error.problem is not None
    assert error.problem["hint"] == {"try": ["2026-09-04"]}
    with pytest.raises(TypeError):
        error.problem["code"] = "changed"  # type: ignore[index]


@pytest.mark.parametrize("status", [100, 200, 204, 302, 399, 600])
def test_status_that_is_not_an_error_is_refused(status: int) -> None:
    with pytest.raises(ValueError, match="is not an error status"):
        error_for(status)


# str


def test_str_names_the_status_code_detail_request_and_request_id() -> None:
    body = problem_body(403, "not_entitled", detail="Not entitled to core-indicators.")
    error = error_for(403, body, request_id="req_1")
    assert str(error) == (
        "403 not_entitled: Not entitled to core-indicators. "
        "(GET /v1/observations?series_id=activity-index; request_id req_1)"
    )
    assert error.args == (str(error),)


def test_str_without_a_request_id() -> None:
    error = error_for(404, problem_body(404, "not_found", detail="No such series."))
    assert str(error) == f"404 not_found: No such series. (GET {PATH})"


def test_str_uses_the_title_without_a_detail() -> None:
    body = json.dumps({"status": 410, "code": "page_token_expired", "title": "Expired"})
    error = error_for(410, body.encode())
    assert str(error) == f"410 page_token_expired: Expired (GET {PATH})"


def test_str_of_a_problem_without_text() -> None:
    error = error_for(500, b"{}")
    assert str(error) == f"500 (GET {PATH})"


@pytest.mark.parametrize(
    ("status", "text"),
    [
        (404, "404: Not Found"),
        (503, "503: Service Unavailable"),
        (429, "429: Too Many Requests"),
        (499, "499"),
        (599, "599"),
    ],
)
def test_str_without_a_problem_body_names_what_the_status_means(
    status: int, text: str
) -> None:
    error = error_for(status, b"<html>Gateway page</html>", "text/html")
    assert str(error) == f"{text} (GET {PATH})"


# The key in a response


def strings_in(value: object) -> list[str]:
    """Every string in an attribute, at any depth, member names included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [
            text
            for name, item in value.items()
            for text in [*strings_in(name), *strings_in(item)]
        ]
    if isinstance(value, list | tuple):
        return [text for item in value for text in strings_in(item)]
    return []


def assert_without_key(error: BaseException, key: str = KEY) -> None:
    """The key is in no rendering of the exception and none of its attributes.

    A string's repr doubles a backslash, so the strings themselves are
    searched too.
    """
    texts = [
        str(error),
        repr(error),
        repr(error.args),
        "".join(traceback.format_exception(error)),
        *(repr(value) for value in vars(error).values()),
        *(text for value in vars(error).values() for text in strings_in(value)),
    ]
    for text in texts:
        assert key not in text


def test_key_in_a_problem_body_is_redacted_at_every_depth() -> None:
    body = {
        "status": 401,
        "code": "unauthenticated",
        "title": "Unauthenticated",
        "detail": f"Bearer {KEY} is not valid.",
        "parameter": KEY,
        "echo": {
            "headers": [f"Authorization: Bearer {KEY}"],
            KEY: {"nested": [[f"{KEY}{KEY}"], 1, None, True]},
        },
    }
    error = error_for(401, json.dumps(body).encode(), request_id=KEY, retry_after=1.0)
    assert type(error) is AuthenticationError
    assert error.detail == "Bearer [redacted] is not valid."
    assert error.parameter == "[redacted]"
    assert error.request_id == "[redacted]"
    assert error.problem == {
        "status": 401,
        "code": "unauthenticated",
        "title": "Unauthenticated",
        "detail": "Bearer [redacted] is not valid.",
        "parameter": "[redacted]",
        "echo": {
            "headers": ["Authorization: Bearer [redacted]"],
            "[redacted]": {"nested": [["[redacted][redacted]"], 1, None, True]},
        },
    }
    assert str(error) == (
        "401 unauthenticated: Bearer [redacted] is not valid. "
        f"(GET {PATH}; request_id [redacted])"
    )
    assert_without_key(error)


def test_key_as_an_unknown_code_is_redacted() -> None:
    error = error_for(404, problem_body(404, KEY))
    assert type(error) is APIError
    assert error.code == "[redacted]"
    assert_without_key(error)


@pytest.mark.parametrize(
    ("status", "code", "key", "exception", "redacted_code"),
    [
        (
            410,
            "page_token_expired",
            "token",
            PageTokenExpiredError,
            "page_[redacted]_expired",
        ),
        (404, "not_found", "not_found", NotFoundError, "[redacted]"),
        (400, "invalid_parameter", "code", InvalidRequestError, None),
    ],
    ids=["inside-a-code", "a-whole-code", "a-member-name"],
)
def test_key_in_a_code_does_not_change_the_exception(
    status: int,
    code: str,
    key: str,
    exception: type[APIError],
    redacted_code: str | None,
) -> None:
    # The exception is chosen from the code the response sent, and only the
    # text it holds is redacted.
    error = error_for(status, problem_body(status, code), secret=key)
    assert type(error) is exception
    assert error.code == redacted_code
    assert_without_key(error, key)


def test_key_in_a_body_that_is_not_a_problem_is_not_kept() -> None:
    error = error_for(401, f"<html>Bearer {KEY}</html>".encode(), "text/html")
    assert type(error) is AuthenticationError
    assert error.problem is None
    assert_without_key(error)


def test_redacting_a_deeply_nested_body_does_not_exhaust_the_stack() -> None:
    depth = 100_000
    document: dict[str, Any] = {}
    inner = document
    for _ in range(depth):
        inner["a"] = {}
        inner = inner["a"]
    inner["key"] = [KEY]
    redacted = _redact_json(document, KEY)
    for _ in range(depth):
        redacted = redacted["a"]
    assert redacted == {"key": ["[redacted]"]}


def test_deeply_nested_problem_body_raises_an_api_error() -> None:
    body = b'{"code": "internal", "a": ' + b"[" * 50_000 + b"]" * 50_000 + b"}"
    error = error_for(500, body)
    assert type(error) is ServerError


def test_redact_replaces_every_occurrence() -> None:
    assert redact(f"{KEY} and {KEY}!", KEY) == "[redacted] and [redacted]!"
    assert redact("nothing here", KEY) == "nothing here"


@pytest.mark.parametrize(
    ("text", "key"),
    [("xx[", "x["), ("acted]zz", "acted]z"), ("one x[ two xx[ three", "x[")],
    ids=["before", "after", "among-others"],
)
def test_text_where_redacting_forms_the_key_again_is_replaced_whole(
    text: str, key: str
) -> None:
    # One replacement leaves the key where [redacted] meets the text beside it.
    assert key in text.replace(key, "[redacted]")
    assert redact(text, key) == "[redacted]"


def test_key_in_the_repr_of_a_decoded_string_is_redacted() -> None:
    # A gateway wrote the key into JSON unescaped, so its \t decoded to a
    # tab. The text then lacks the key, but its repr writes the tab as \t.
    key = "sk_live\\t9Qz"
    body = b'{"code": "unauthenticated", "detail": "Bearer sk_live\\t9Qz is bad"}'
    error = error_for(401, body, secret=key)
    assert error.detail == "[redacted]"
    assert error.problem == {"code": "unauthenticated", "detail": "[redacted]"}
    assert_without_key(error, key)


def test_member_names_that_redact_to_the_same_text_keep_the_later_value() -> None:
    body = json.dumps({"code": "internal", KEY: 1, "[redacted]": 2}).encode()
    error = error_for(500, body)
    assert error.problem == {"code": "internal", "[redacted]": 2}


def test_key_spanning_the_code_and_the_detail_is_redacted() -> None:
    key = "found:"
    error = error_for(404, problem_body(404, "not_found", detail="No such series."))
    assert key in str(error)
    error = error_for(
        404, problem_body(404, "not_found", detail="No such series."), secret=key
    )
    assert type(error) is NotFoundError
    assert error.code == "not_found"
    assert str(error) == f"404 not_[redacted] No such series. (GET {PATH})"
    assert_without_key(error, key)


def test_key_spanning_the_request_id_and_the_text_after_it_is_redacted() -> None:
    key = "req_1)"
    error = error_for(500, None, None, request_id="req_1", secret=key)
    assert error.request_id == "req_1"
    assert str(error) == (
        f"500: Internal Server Error (GET {PATH}; request_id [redacted]"
    )
    assert_without_key(error, key)


def test_key_spanning_the_status_and_the_colon_after_it_is_redacted() -> None:
    key = "302:"
    error = unexpected_response(
        "a 302 response",
        status=302,
        request_id=None,
        method="GET",
        path=PATH,
        attempts=1,
        secret=key,
    )
    assert str(error) == f"[redacted] a 302 response (GET {PATH})"
    assert_without_key(error, key)


# Unexpected responses


def test_unexpected_response_names_the_reason_status_and_request() -> None:
    error = unexpected_response(
        "data[0].available_at is missing",
        status=200,
        request_id="req_2",
        method="GET",
        path=PATH,
        attempts=3,
        secret=KEY,
    )
    assert type(error) is UnexpectedResponseError
    assert str(error) == (
        f"200: data[0].available_at is missing (GET {PATH}; request_id req_2)"
    )
    assert (error.status, error.request_id) == (200, "req_2")
    assert (error.method, error.path, error.attempts) == ("GET", PATH, 3)


def test_unexpected_response_from_a_pagination_check_has_no_status() -> None:
    error = unexpected_response(
        "the page's position differs from the query's",
        status=None,
        request_id=None,
        method="GET",
        path=PATH,
        attempts=1,
        secret=KEY,
    )
    assert str(error) == f"the page's position differs from the query's (GET {PATH})"
    assert error.status is None


def test_unexpected_response_redacts_the_request_id() -> None:
    error = unexpected_response(
        "a 302 response",
        status=302,
        request_id=f"id-{KEY}",
        method="GET",
        path=PATH,
        attempts=1,
        secret=KEY,
    )
    assert error.request_id == "id-[redacted]"
    assert_without_key(error)


def test_refused_body_becomes_an_unexpected_response_naming_member_and_index() -> None:
    body = json.dumps({"data": [{}, {"sequence": 1}]}).encode()
    reason = None
    try:
        decode_change_page(decode_object("application/json", body))
    except InvalidResponse as invalid:
        reason = str(invalid)
    assert reason is not None
    # Built outside the except block, as the transport raises it.
    error = unexpected_response(
        reason,
        status=200,
        request_id=None,
        method="GET",
        path="/v1/datasets/core-indicators/changes?after=0",
        attempts=1,
        secret=KEY,
    )
    assert str(error) == (
        "200: data[0].sequence is missing "
        "(GET /v1/datasets/core-indicators/changes?after=0)"
    )
    with pytest.raises(UnexpectedResponseError) as caught:
        raise error
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
