"""Building a request step's request, and choosing whether the SDK sends it.

`build` resolves a step's references and checks its request, as the API
runner does. `route` applies the rule of "Which steps go through the SDK"
in `spec/conformance.md`, and returns the SDK call that sends the request,
or `None` when the runner sends it over HTTP.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import InvalidOperation
from typing import Any, Final
from urllib.parse import quote

from financial_data import Client
from tests.support.api import CREDENTIALS, DEFAULT_CREDENTIAL

from .matching import (
    Body,
    decimal_text,
    is_number,
    json_type,
    render,
    resolve,
)


class _Absent:
    """The value of a request member that the step leaves out."""

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT: Final = _Absent()


@dataclass(frozen=True, slots=True)
class Parameter:
    """A query member as sent: its values, in order, and whether it is an array.

    A `null` member, or a `null` element of an array, sends nothing.
    """

    name: str
    values: tuple[str, ...]
    array: bool = False


@dataclass(frozen=True, slots=True)
class Request:
    """A request step's request, with its references resolved.

    `credential` is the credential whose key the request sends, or `None`
    for no `Authorization` header; an absent `credential` means
    `cred_research`. `authorization`, when not `None`, is a raw header value
    that replaces it. `body` is a JSON value, or `ABSENT` for none.
    """

    method: str
    path: str
    query: tuple[Parameter, ...] = ()
    credential: str | None = DEFAULT_CREDENTIAL
    authorization: str | None = None
    body: Any = ABSENT

    def pairs(self) -> list[tuple[str, str]]:
        """Each query parameter as sent, in order."""
        return [(p.name, value) for p in self.query for value in p.values]

    def target(self) -> str:
        """The path and the query string, as sent."""
        pairs = self.pairs()
        if not pairs:
            return self.path
        query = "&".join(
            f"{quote(name, safe='')}={quote(value, safe='')}" for name, value in pairs
        )
        return f"{self.path}?{query}"

    def __str__(self) -> str:
        return f"{self.method} {self.target()}"


class InvalidStep(Exception):
    """A request step that cannot be sent as written."""


def build(request: Mapping[str, Any], bodies: Mapping[str, Body]) -> Request:
    """Resolve a step's references and return the request to send.

    Raises `UnresolvedReference` for a reference that does not resolve, and
    `InvalidStep` for a member that is not of a type the format allows once
    resolved.
    """
    method = "GET"
    if "method" in request:
        method = _text("method", resolve(request["method"], bodies))
        if not method:
            raise InvalidStep(f"method {render(request['method'])} is empty")
    path = _text("path", resolve(request["path"], bodies))
    if not path.startswith("/"):
        raise InvalidStep(f"path {render(path)} does not start with /")
    query = request.get("query")
    if query is None:
        query = {}
    if not isinstance(query, Mapping):
        raise InvalidStep(f"query {render(query)} is not an object")
    parameters = tuple(
        _parameter(name, resolve(query[name], bodies)) for name in sorted(query)
    )
    authorization = None
    credential: str | None = DEFAULT_CREDENTIAL
    if "authorization" in request:
        authorization = _text(
            "authorization", resolve(request["authorization"], bodies)
        )
    elif "credential" in request:
        # Resolved before its type is known, because a string that is
        # exactly one reference may stand for null.
        resolved = resolve(request["credential"], bodies)
        if resolved is not None and not isinstance(resolved, str):
            raise InvalidStep(
                f"credential {render(request['credential'])} is {json_type(resolved)}"
                "; it must be a string or null"
            )
        if resolved is not None and resolved not in CREDENTIALS:
            raise InvalidStep(
                f"credential {resolved} is not in fixtures/credentials.json"
            )
        credential = resolved
    body = resolve(request["body"], bodies) if "body" in request else ABSENT
    return Request(method, path, parameters, credential, authorization, body)


def _text(member: str, value: object) -> str:
    if not isinstance(value, str):
        raise InvalidStep(f"{member} is {json_type(value)}; it must be a string")
    return value


def _parameter(name: str, value: object) -> Parameter:
    array = isinstance(value, list)
    elements = value if isinstance(value, list) else [value]
    values = []
    for element in elements:
        if element is None:
            continue
        if isinstance(element, str):
            values.append(element)
        elif is_number(element):
            # A number is sent in decimal, however the body wrote it.
            try:
                values.append(decimal_text(element))
            except (ValueError, InvalidOperation) as error:
                raise InvalidStep(f"query parameter {name}: {error}") from None
        else:
            what = json_type(element)
            if array:
                what = f"an array holding {what}"
            raise InvalidStep(
                f"query parameter {name} is {what}; it must be a string, a "
                "number, null, or an array of them"
            )
    return Parameter(name, tuple(values), array)


# The SDK's calls


@dataclass(frozen=True, slots=True)
class _Endpoint:
    pattern: re.Pattern[str]
    method: str
    """The SDK method, such as `observations.page`, on a `Client`."""
    arguments: tuple[str, ...] = ()
    """The query parameters the method takes, in its signature's order."""
    required: frozenset[str] = frozenset()
    integers: frozenset[str] = frozenset()


_SEGMENT: Final = r"([^/]+)"
_ENDPOINTS: Final = (
    _Endpoint(re.compile(r"/v1/meta"), "meta"),
    _Endpoint(re.compile(r"/v1/datasets"), "datasets.list"),
    _Endpoint(re.compile(rf"/v1/datasets/{_SEGMENT}"), "datasets.get"),
    _Endpoint(re.compile(r"/v1/series"), "series.list"),
    _Endpoint(re.compile(rf"/v1/series/{_SEGMENT}"), "series.get"),
    _Endpoint(
        re.compile(r"/v1/observations"),
        "observations.page",
        (
            "series_id",
            "period_start",
            "period_end",
            "available_as_of",
            "page_size",
            "page_token",
        ),
        frozenset({"series_id"}),
        frozenset({"page_size"}),
    ),
    _Endpoint(
        re.compile(rf"/v1/datasets/{_SEGMENT}/changes"),
        "changes.read",
        ("after", "limit"),
        frozenset({"after"}),
        frozenset({"after", "limit"}),
    ),
)
_UNSENT_EXPECTATIONS: Final = ("headers", "body_sha256", "body_lines")
"""Expectations about the raw response, which an SDK result does not keep."""


@dataclass(frozen=True, slots=True)
class SDKCall:
    """One SDK call, made with the client of a customer credential.

    `args` are the path parameters, passed positionally, and `kwargs` the
    query parameters, passed by name: strings as written and integers
    converted with `int()`.
    """

    method: str
    args: tuple[str, ...]
    kwargs: tuple[tuple[str, str | int], ...]
    credential: str

    def __call__(self, client: Client) -> object:
        target: Any = client
        for name in self.method.split("."):
            target = getattr(target, name)
        return target(*self.args, **dict(self.kwargs))

    def __str__(self) -> str:
        arguments = [repr(arg) for arg in self.args]
        arguments += [f"{name}={value!r}" for name, value in self.kwargs]
        return f"client.{self.method}({', '.join(arguments)}) as {self.credential}"


def canonical_integer(text: str) -> int | None:
    """Return `int(text)` when the SDK would send the same text, else `None`.

    `0` and `-1` are canonical; `010`, `+5`, `1.5`, `ten`, and `-0` are not.
    """
    try:
        value = int(text)
    except ValueError:
        return None
    return value if str(value) == text else None


def route(request: Request, expect: Mapping[str, Any]) -> SDKCall | None:
    """Return the SDK call that sends a request, or `None` to send it over HTTP.

    A request goes through the SDK when all of these hold: its method is
    `GET` and its path matches an endpoint exactly, with no empty, `.`, or
    `..` path parameter and no trailing slash; it has no raw
    `authorization` and its credential is a customer's; every query member
    is an argument of the endpoint's method, every required argument is
    present, and no member is an array; every integer argument is in the
    form `str(int(s))`; and its `expect` has none of `headers`,
    `body_sha256`, or `body_lines`.
    """
    if request.method != "GET":
        return None
    for endpoint in _ENDPOINTS:
        found = endpoint.pattern.fullmatch(request.path)
        if found is not None:
            break
    else:
        return None
    args = found.groups()
    if any(arg in (".", "..") for arg in args):
        return None
    if request.authorization is not None or request.credential is None:
        return None
    if CREDENTIALS[request.credential].kind != "customer":
        return None
    given: dict[str, str] = {}
    for parameter in request.query:
        if parameter.array or parameter.name not in endpoint.arguments:
            return None
        if parameter.values:  # a null member is left out of the call
            (given[parameter.name],) = parameter.values
    if not endpoint.required <= given.keys():
        return None
    kwargs: list[tuple[str, str | int]] = []
    for name in endpoint.arguments:
        if name not in given:
            continue
        if name in endpoint.integers:
            number = canonical_integer(given[name])
            if number is None:
                return None
            kwargs.append((name, number))
        else:
            kwargs.append((name, given[name]))
    if any(member in expect for member in _UNSENT_EXPECTATIONS):
        return None
    return SDKCall(endpoint.method, args, tuple(kwargs), request.credential)
