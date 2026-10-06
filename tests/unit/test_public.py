"""The public names, the version, and the exceptions' classes and attributes."""

from __future__ import annotations

import importlib.metadata
from typing import Any

import pytest

import financial_data
from financial_data import (
    APIError,
    AuthenticationError,
    ClientClosedError,
    ConfigError,
    DeadlineExceededError,
    FinancialDataError,
    InvalidRequestError,
    NotEntitledError,
    NotFoundError,
    PageTokenError,
    PageTokenExpiredError,
    PositionAheadError,
    PositionExpiredError,
    RateLimitError,
    ServerError,
    TransportError,
    UnexpectedResponseError,
    UnsupportedAPIVersionError,
)

# Every public name in spec/client.md.
PUBLIC_NAMES = {
    "Client",
    "RetryPolicy",
    "Revision",
    "Meta",
    "Dataset",
    "Series",
    "ObservationPage",
    "ChangePage",
    "ChangeType",
    "MissingReason",
    "FinancialDataError",
    "ConfigError",
    "ClientClosedError",
    "APIError",
    "InvalidRequestError",
    "PageTokenError",
    "PageTokenExpiredError",
    "PositionAheadError",
    "PositionExpiredError",
    "AuthenticationError",
    "NotEntitledError",
    "NotFoundError",
    "UnsupportedAPIVersionError",
    "RateLimitError",
    "ServerError",
    "TransportError",
    "DeadlineExceededError",
    "UnexpectedResponseError",
    "__version__",
}


def test_all_lists_every_public_name_once() -> None:
    assert len(financial_data.__all__) == len(set(financial_data.__all__))
    assert set(financial_data.__all__) == PUBLIC_NAMES


@pytest.mark.parametrize("name", sorted(PUBLIC_NAMES))
def test_public_name_is_importable_from_the_top_level(name: str) -> None:
    namespace: dict[str, Any] = {}
    exec(f"from financial_data import {name}", namespace)
    assert namespace[name] is getattr(financial_data, name)


def test_star_import_gives_every_public_name() -> None:
    namespace: dict[str, Any] = {}
    exec("from financial_data import *", namespace)
    assert set(namespace) - {"__builtins__"} == PUBLIC_NAMES


def test_every_name_without_an_underscore_is_in_all() -> None:
    public = {name for name in vars(financial_data) if not name.startswith("_")}
    assert public <= set(financial_data.__all__)


def test_version_is_the_distributions() -> None:
    assert financial_data.__version__ == importlib.metadata.version(
        "financial-data-sdk"
    )


@pytest.mark.parametrize(
    ("exception", "bases"),
    [
        (FinancialDataError, (Exception,)),
        (ConfigError, (FinancialDataError, ValueError)),
        (ClientClosedError, (FinancialDataError, RuntimeError)),
        (APIError, (FinancialDataError,)),
        (InvalidRequestError, (APIError,)),
        (PageTokenError, (APIError,)),
        (PageTokenExpiredError, (PageTokenError,)),
        (PositionAheadError, (APIError,)),
        (PositionExpiredError, (APIError,)),
        (AuthenticationError, (APIError,)),
        (NotEntitledError, (APIError,)),
        (NotFoundError, (APIError,)),
        (UnsupportedAPIVersionError, (APIError,)),
        (RateLimitError, (APIError,)),
        (ServerError, (APIError,)),
        (TransportError, (FinancialDataError,)),
        (DeadlineExceededError, (FinancialDataError, TimeoutError)),
        (UnexpectedResponseError, (FinancialDataError,)),
    ],
)
def test_exception_bases(exception: type[Exception], bases: tuple[type, ...]) -> None:
    assert exception.__bases__ == bases


API_ERRORS = [
    APIError,
    InvalidRequestError,
    PageTokenError,
    PageTokenExpiredError,
    PositionAheadError,
    PositionExpiredError,
    AuthenticationError,
    NotEntitledError,
    NotFoundError,
    UnsupportedAPIVersionError,
    RateLimitError,
    ServerError,
]


@pytest.mark.parametrize("exception", API_ERRORS)
def test_api_error_attributes(exception: type[APIError]) -> None:
    problem = {"status": 403, "code": "not_entitled", "extra": {"nested": [1]}}
    error = exception(
        "403 not_entitled: no access",
        status=403,
        method="GET",
        path="/v1/observations?series_id=activity-index",
        attempts=2,
        code="not_entitled",
        title="Not entitled",
        detail="no access",
        parameter=None,
        problem=problem,
        retry_after=1.5,
        request_id="req_1",
    )
    assert error.args == ("403 not_entitled: no access",)
    assert str(error) == "403 not_entitled: no access"
    assert (error.status, error.method, error.path, error.attempts) == (
        403,
        "GET",
        "/v1/observations?series_id=activity-index",
        2,
    )
    assert (error.code, error.title, error.detail, error.parameter) == (
        "not_entitled",
        "Not entitled",
        "no access",
        None,
    )
    assert (error.retry_after, error.request_id) == (1.5, "req_1")
    assert error.problem == problem
    with pytest.raises(TypeError):
        error.problem["code"] = "changed"  # type: ignore[index]
    # Changing the caller's mapping does not change the exception's.
    problem["code"] = "changed"
    assert error.problem["code"] == "not_entitled"


def test_api_error_attributes_default_to_none() -> None:
    error = APIError("502", status=502, method="GET", path="/v1/meta", attempts=4)
    assert error.code is None
    assert error.title is None
    assert error.detail is None
    assert error.parameter is None
    assert error.problem is None
    assert error.retry_after is None
    assert error.request_id is None


@pytest.mark.parametrize("exception", [TransportError, DeadlineExceededError])
def test_request_failure_attributes(
    exception: type[TransportError | DeadlineExceededError],
) -> None:
    error = exception("connection refused", method="GET", path="/v1/meta", attempts=4)
    assert error.args == ("connection refused",)
    assert str(error) == "connection refused"
    assert (error.method, error.path, error.attempts) == ("GET", "/v1/meta", 4)


@pytest.mark.parametrize("status", [200, 302, None])
def test_unexpected_response_attributes(status: int | None) -> None:
    error = UnexpectedResponseError(
        "data[0].value: not a string",
        status=status,
        method="GET",
        path="/v1/observations?series_id=activity-index",
        attempts=1,
        request_id="req_2",
    )
    assert error.args == ("data[0].value: not a string",)
    assert (error.status, error.request_id, error.attempts) == (status, "req_2", 1)
    assert (error.method, error.path) == (
        "GET",
        "/v1/observations?series_id=activity-index",
    )


def test_unexpected_response_request_id_defaults_to_none() -> None:
    error = UnexpectedResponseError(
        "page repeated a token", status=None, method="GET", path="/", attempts=1
    )
    assert error.request_id is None
