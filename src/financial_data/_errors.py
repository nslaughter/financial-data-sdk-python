"""The exceptions the SDK raises, and choosing one for a response.

Every exception's `args` is its message alone, so the attributes are the
only other place an exception holds anything. Choosing an exception for a
response is pure: it takes the response's status, the headers it needs, and
its body, and replaces the key with `[redacted]` in any text taken from the
response before an exception holds it.
"""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from types import MappingProxyType
from typing import Any, Final

from ._decode import json_object, media_type

REDACTED: Final = "[redacted]"


class FinancialDataError(Exception):
    """Base class of every exception the SDK raises."""

    def __reduce__(self) -> tuple[Any, ...]:
        # pickle and copy would call the class with `args`, the message
        # alone, which an `__init__` with required keyword arguments refuses.
        # A process pool pickles the exception a worker raises, so make it
        # from `args` without `__init__`, then restore its attributes.
        return (_rebuild, (type(self), self.args), dict(vars(self)))


def _rebuild(
    cls: type[FinancialDataError], args: tuple[Any, ...]
) -> FinancialDataError:
    """Make an exception with these `args` without calling its `__init__`.

    `args` is set after `__new__`, because `OSError.__new__`, which
    `DeadlineExceededError` inherits through `TimeoutError`, leaves `args`
    empty for a subclass with its own `__init__`.
    """
    error = cls.__new__(cls)
    error.args = args
    return error


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

    def __reduce__(self) -> tuple[Any, ...]:
        rebuild, args, state = super().__reduce__()
        if self.problem is not None:  # a mapping proxy cannot be pickled
            state["problem"] = dict(self.problem)
        return (rebuild, args, state)

    def __setstate__(self, state: dict[str, Any] | None, /) -> None:
        if state is not None and state.get("problem") is not None:
            state = {**state, "problem": MappingProxyType(dict(state["problem"]))}
        super().__setstate__(state)


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


# Choosing an exception for a response

_BY_CODE: Final[dict[tuple[int, str], type[APIError]]] = {
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


def redact(text: str, secret: str) -> str:
    """Replace every occurrence of the key in text with `[redacted]`.

    A key with `[` or `]` in it can form again where `[redacted]` meets the
    text beside it, as the key `x[` does in `xx[`. A key can also be in the
    text's `repr` though not in the text: the key `a\\tb` is when a
    response's JSON held it unescaped and `\\t` decoded to a tab, and a key
    with a quote can join the quotes `repr` adds. Keeping the key out comes
    before keeping the text exact, so such text is replaced whole.
    """
    redacted = text.replace(secret, REDACTED)
    if secret in redacted or secret in repr(redacted):
        return REDACTED
    return redacted


def _redact_json(document: dict[str, Any], secret: str) -> dict[str, Any]:
    """Copy a JSON object with the key replaced in every string, at any depth.

    Member names are strings too, so two names can become the same, and
    the later member's value is kept, as when JSON repeats a name. The copy
    walks the document with a stack instead of recursion, so a body nested
    as deeply as the JSON parser accepts cannot exhaust Python's.
    """
    root: dict[str, Any] = {}
    pending: list[tuple[Any, Any]] = [(document, root)]

    def copy(value: Any) -> Any:
        if isinstance(value, dict | list):
            container = type(value)()
            pending.append((value, container))
            return container
        return redact(value, secret) if isinstance(value, str) else value

    while pending:
        source, target = pending.pop()
        if isinstance(source, dict):
            for name, value in source.items():
                target[redact(name, secret)] = copy(value)
        else:
            target.extend(copy(value) for value in source)
    return root


def _problem(content_type: str | None, body: bytes | None) -> dict[str, Any] | None:
    """Return a problem response's body, or `None` if it is not one.

    A problem response's `Content-Type` is `application/problem+json`,
    ignoring parameters, and its body is a JSON object.
    """
    if body is None or media_type(content_type) != "application/problem+json":
        return None
    return json_object(body)


def _text_member(problem: Mapping[str, Any] | None, name: str) -> str | None:
    """Return a problem member that is a string, or `None`."""
    value = None if problem is None else problem.get(name)
    return value if isinstance(value, str) else None


def _exception_class(status: int, code: str | None) -> type[APIError]:
    """Choose by the code and the status together, then by the status alone."""
    chosen = None if code is None else _BY_CODE.get((status, code))
    if chosen is not None:
        return chosen
    if status == 401:
        return AuthenticationError
    if status == 429:
        return RateLimitError
    if status >= 500:
        return ServerError
    return APIError


def _phrase(status: int) -> str | None:
    """Return the standard reason phrase of a status, or `None` for none."""
    try:
        return HTTPStatus(status).phrase
    except ValueError:  # a status HTTP does not name, such as 499
        return None


def _message(
    head: str, method: str, path: str, request_id: str | None, secret: str
) -> str:
    """Write an exception's message, naming the request.

    Each part is already redacted, but the key can span where two meet, as
    `found:` does in `404 not_found: ...`, so the whole is redacted too.
    """
    if request_id is None:
        message = f"{head} ({method} {path})"
    else:
        message = f"{head} ({method} {path}; request_id {request_id})"
    return redact(message, secret)


def api_error(
    *,
    status: int,
    content_type: str | None,
    body: bytes | None,
    retry_after: float | None,
    request_id: str | None,
    method: str,
    path: str,
    attempts: int,
    secret: str,
) -> APIError:
    """Return the exception for a `4xx` or `5xx` response.

    `body` is `None` when the SDK read no body, as for a response a caller's
    event hook refused, which is then handled as one without a problem body.
    `retry_after` is the wait `Retry-After` asked for, already read.
    `request_id` is the `Request-Id` header, and `path` is the request's
    path with its query string, with each argument that holds the key
    already redacted. `secret` is the key, which is replaced with
    `[redacted]` in the problem body and the request ID. The exception is
    chosen from the response's own `code`, so a key inside a code, such as
    `token` in `page_token_expired`, does not change it.
    """
    if not 400 <= status <= 599:
        raise ValueError(f"status {status} is not an error status")
    problem = _problem(content_type, body)
    # Chosen before redaction, so the key cannot change which exception it is.
    exception_class = _exception_class(status, _text_member(problem, "code"))
    if problem is not None:
        problem = _redact_json(problem, secret)
    code = _text_member(problem, "code")
    title = _text_member(problem, "title")
    detail = _text_member(problem, "detail")
    if request_id is not None:
        request_id = redact(request_id, secret)
    head = str(status) if code is None else f"{status} {code}"
    # Without a problem body, name what the status means rather than quote
    # the reason phrase, which anything in front of the API may have written.
    text = _phrase(status) if problem is None else detail or title
    if text:
        head = f"{head}: {text}"
    return exception_class(
        _message(head, method, path, request_id, secret),
        status=status,
        method=method,
        path=path,
        attempts=attempts,
        code=code,
        title=title,
        detail=detail,
        parameter=_text_member(problem, "parameter"),
        problem=problem,
        retry_after=retry_after,
        request_id=request_id,
    )


def unexpected_response(
    reason: str,
    *,
    status: int | None,
    request_id: str | None,
    method: str,
    path: str,
    attempts: int,
    secret: str,
) -> UnexpectedResponseError:
    """Return the exception for a response the API's documents do not allow.

    `reason` is the SDK's own text, such as an `InvalidResponse`'s message,
    and `status` is `None` for a page that breaks a pagination guarantee.
    `request_id` and `path` are as for `api_error`.
    """
    if request_id is not None:
        request_id = redact(request_id, secret)
    head = reason if status is None else f"{status}: {reason}"
    return UnexpectedResponseError(
        _message(head, method, path, request_id, secret),
        status=status,
        method=method,
        path=path,
        attempts=attempts,
        request_id=request_id,
    )
