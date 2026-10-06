"""The exceptions the SDK raises.

Every exception's `args` is its message alone, so the attributes are the
only other place an exception holds anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


class FinancialDataError(Exception):
    """Base class of every exception the SDK raises."""


class ConfigError(FinancialDataError, ValueError):
    """An argument of `Client`, `with_options`, or `RetryPolicy` is invalid.

    Also raised when no key is given or set in the environment. The message
    never repeats the value it refuses.
    """


class ClientClosedError(FinancialDataError, RuntimeError):
    """A call was made on a closed client, or through a closed HTTP client."""


class APIError(FinancialDataError):
    """The API, or something in front of it, answered with a 4xx or 5xx status.

    A subclass says which; `APIError` itself is raised when no subclass
    applies.
    """

    status: int
    """The HTTP status."""
    code: str | None
    """The problem response's `code`, or `None`."""
    title: str | None
    """The problem response's `title`, or `None`."""
    detail: str | None
    """The problem response's `detail`, or `None`."""
    parameter: str | None
    """The parameter at fault, from the problem response, or `None`."""
    problem: Mapping[str, Any] | None
    """The whole problem body, read-only, or `None`."""
    retry_after: float | None
    """The seconds the response's `Retry-After` asked for, or `None`."""
    request_id: str | None
    """The response's `Request-Id` header, or `None`."""
    method: str
    """The request's method."""
    path: str
    """The request's path with its query string, as sent."""
    attempts: int
    """How many requests the call sent, retries included."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        method: str,
        path: str,
        attempts: int,
        code: str | None = None,
        title: str | None = None,
        detail: str | None = None,
        parameter: str | None = None,
        problem: Mapping[str, Any] | None = None,
        retry_after: float | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.parameter = parameter
        self.problem = None if problem is None else MappingProxyType(dict(problem))
        self.retry_after = retry_after
        self.request_id = request_id
        self.method = method
        self.path = path
        self.attempts = attempts


class InvalidRequestError(APIError):
    """400 with a code naming an invalid argument; `parameter` names it.

    The codes are `unknown_parameter`, `missing_parameter`,
    `invalid_parameter`, `conflicting_cutoffs`, and `cutoff_in_future`.
    """


class PageTokenError(APIError):
    """400 with `invalid_page_token` or `page_token_mismatch`."""


class PageTokenExpiredError(PageTokenError):
    """410 `page_token_expired`. Restart the query."""


class PositionAheadError(APIError):
    """400 `position_ahead`."""


class PositionExpiredError(APIError):
    """410 `position_expired`. Load the data again with a query."""


class AuthenticationError(APIError):
    """401, with any code."""


class NotEntitledError(APIError):
    """403 `not_entitled`."""


class NotFoundError(APIError):
    """404 `not_found`."""


class UnsupportedAPIVersionError(APIError):
    """404 `unsupported_api_version`."""


class RateLimitError(APIError):
    """429, with any code, once retries stop."""


class ServerError(APIError):
    """Any 5xx, with any code, once retries stop."""


class TransportError(FinancialDataError):
    """httpx failed to send the request or to receive its response.

    Raised once retries stop. `__cause__` is httpx's exception.
    """

    method: str
    """The request's method."""
    path: str
    """The request's path with its query string, as sent."""
    attempts: int
    """How many requests the call sent, retries included."""

    def __init__(self, message: str, *, method: str, path: str, attempts: int) -> None:
        super().__init__(message)
        self.method = method
        self.path = path
        self.attempts = attempts


class DeadlineExceededError(FinancialDataError, TimeoutError):
    """The call's deadline passed while a request was in flight."""

    method: str
    """The request's method."""
    path: str
    """The request's path with its query string, as sent."""
    attempts: int
    """How many requests the call sent, retries included."""

    def __init__(self, message: str, *, method: str, path: str, attempts: int) -> None:
        super().__init__(message)
        self.method = method
        self.path = path
        self.attempts = attempts


class UnexpectedResponseError(FinancialDataError):
    """A response the API's documents do not allow.

    A successful response that fails validation, a body that does not
    decode, a 1xx or 3xx status, or a page that breaks a pagination
    guarantee.
    """

    status: int | None
    """The HTTP status, or `None` when a pagination check raised it."""
    request_id: str | None
    """The response's `Request-Id` header, or `None`."""
    method: str
    """The request's method."""
    path: str
    """The request's path with its query string, as sent."""
    attempts: int
    """How many requests the call sent, retries included."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None,
        method: str,
        path: str,
        attempts: int,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.request_id = request_id
        self.method = method
        self.path = path
        self.attempts = attempts
