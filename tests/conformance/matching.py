"""Matching and references, as the API's conformance format defines them.

JSON values here are what `json.loads` returns, with numbers as `int`,
`float`, or `Decimal`. The runner parses the contract's files and the API's
bodies with `parse_float=Decimal`, so a number keeps its exact value, and a
`float` from elsewhere, such as an SDK exception's `problem`, compares by the
shortest text that writes it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final, TypeGuard

MAX_RENDERED: Final = 400
"""The most characters of a value that a report shows."""

_REFERENCE: Final = re.compile(r"\$\{([^{}]*)\}")
_INDEX: Final = re.compile(r"0|[1-9][0-9]*")


@dataclass(frozen=True, slots=True)
class Difference:
    """Where an actual value first differs from an expected one."""

    at: str
    expected: str
    actual: str


def is_number(value: object) -> TypeGuard[int | float | Decimal]:
    """Whether a value is a JSON number. A `bool` is not, though it is an `int`."""
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)


def _exact(number: int | float | Decimal) -> Decimal:
    if isinstance(number, float):
        return Decimal(repr(number))
    return Decimal(number)


def equal_numbers(a: int | float | Decimal, b: int | float | Decimal) -> bool:
    """Whether two JSON numbers have the same value, however they are written."""
    return _exact(a) == _exact(b)


def decimal_text(number: int | float | Decimal) -> str:
    """Write a JSON number in decimal, without an exponent.

    An integer value has no fractional part: 36, 36.0, and 3.6e1 are all
    `36`, and 1.50 and 15e-1 are both `1.5`.
    """
    exact = _exact(number)
    if not exact.is_finite():
        raise ValueError(f"the number {number} cannot be written in decimal")
    if exact == exact.to_integral_value():
        return str(int(exact))
    return format(exact.normalize(), "f")


def match(at: str, expected: Any, actual: Any) -> Difference | None:
    """Compare an expected value with an actual one, and return the first difference.

    An expected object matches an object with every member it names, each
    matching; members it does not name are not compared. An expected array
    matches an array of the same length whose elements match in order. Any
    other expected value matches an equal value of the same JSON type, so
    `"102.0"` matches neither `"102"` nor `102`, and `null` matches only a
    member that is present and `null`. `at` names where the values are, such
    as `body`.
    """
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return Difference(at, render(expected), render(actual))
        for name in sorted(expected):
            where = f"{at}.{name}" if at else name
            if name not in actual:
                return Difference(where, render(expected[name]), "no member")
            difference = match(where, expected[name], actual[name])
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return Difference(at, render(expected), render(actual))
        for index, (want, got) in enumerate(zip(expected, actual, strict=False)):
            difference = match(f"{at}[{index}]", want, got)
            if difference is not None:
                return difference
        if len(expected) != len(actual):
            return Difference(
                at,
                f"{len(expected)} elements",
                f"{len(actual)} elements: {render(actual)}",
            )
        return None
    if is_number(expected):
        if not is_number(actual) or not equal_numbers(expected, actual):
            return Difference(at, render(expected), render(actual))
        return None
    # A string, a boolean, or null, which must have the same type as well as
    # the same value: True == 1 in Python, but not in JSON.
    if type(expected) is not type(actual) or expected != actual:
        return Difference(at, render(expected), render(actual))
    return None


def json_type(value: object) -> str:
    """Name the JSON type of a value, for a report."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if is_number(value):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, Mapping):
        return "an object"
    return f"a {type(value).__name__}"


def render(value: object) -> str:
    """Write a value as JSON for a report, shortened if it is long."""
    try:
        text = json.dumps(_plain(value), ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return shorten(text)


def shorten(text: str) -> str:
    """Return text, or its start if it is long."""
    if len(text) <= MAX_RENDERED:
        return text
    return text[:MAX_RENDERED] + "…"


def _plain(value: object) -> object:
    """Copy a value with each `Decimal` as an `int` or `float`, for `json.dumps`."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Mapping):
        return {name: _plain(member) for name, member in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(element) for element in value]
    return value


# References


class UnresolvedReference(Exception):
    """A reference names a step that has not run, or a member it lacks."""


@dataclass(frozen=True, slots=True)
class Body:
    """What a step's reference resolves against: a JSON value, or why it has none."""

    value: Any = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _Reference:
    start: int
    end: int
    step: str
    path: tuple[str, ...]

    def __str__(self) -> str:
        return "${" + ".".join((self.step, *self.path)) + "}"


def _references(text: str) -> list[_Reference]:
    found = []
    for m in _REFERENCE.finditer(text):
        step, dot, path = m.group(1).partition(".")
        if not dot or not step:
            raise UnresolvedReference(
                f"malformed reference {m.group(0)}: want ${{<id>.<path>}}"
            )
        names = tuple(path.split("."))
        if "" in names:
            raise UnresolvedReference(
                f"malformed reference {m.group(0)}: its path has an empty member name"
            )
        found.append(_Reference(m.start(), m.end(), step, names))
    return found


def _lookup(reference: _Reference, bodies: Mapping[str, Body]) -> Any:
    body = bodies.get(reference.step)
    if body is None:
        raise UnresolvedReference(f"{reference} names no step that has run")
    if body.error is not None:
        raise UnresolvedReference(
            f"{reference}: step {reference.step} has no body: {body.error}"
        )
    value = body.value
    for index, name in enumerate(reference.path):
        where = (
            f"the body of step {reference.step}"
            if index == 0
            else ".".join((reference.step, *reference.path[:index]))
        )
        if isinstance(value, Mapping):
            if name not in value:
                raise UnresolvedReference(
                    f"{reference}: {where} has no member {name!r}"
                )
            value = value[name]
        elif isinstance(value, list):
            if not _INDEX.fullmatch(name) or int(name) >= len(value):
                raise UnresolvedReference(
                    f"{reference}: {where}, an array of {len(value)} elements, "
                    f"has no element {name}"
                )
            value = value[int(name)]
        else:
            raise UnresolvedReference(
                f"{reference}: {where} is {json_type(value)}, "
                f"which has no member {name!r}"
            )
    return value


def resolve_text(text: str, bodies: Mapping[str, Body]) -> Any:
    """Replace the references in a string.

    A string that is exactly one reference becomes the referenced value, of
    any JSON type. A reference that is part of a longer string must name a
    string or a number, which is inserted as text.
    """
    found = _references(text)
    if not found:
        return text
    if len(found) == 1 and found[0].start == 0 and found[0].end == len(text):
        return _lookup(found[0], bodies)
    parts = []
    last = 0
    for reference in found:
        parts.append(text[last : reference.start])
        value = _lookup(reference, bodies)
        if isinstance(value, str):
            parts.append(value)
        elif is_number(value):
            try:
                parts.append(decimal_text(value))
            except (ValueError, InvalidOperation) as error:
                raise UnresolvedReference(f"{reference}: {error}") from None
        else:
            raise UnresolvedReference(
                f"{reference} is part of a longer string, so it must name a string "
                f"or a number, not {json_type(value)}"
            )
        last = reference.end
    parts.append(text[last:])
    return "".join(parts)


def resolve(value: Any, bodies: Mapping[str, Body]) -> Any:
    """Copy a JSON value with the references in its strings replaced.

    Member names are not resolved, and `value` is not changed.
    """
    if isinstance(value, str):
        return resolve_text(value, bodies)
    if isinstance(value, list):
        return [resolve(element, bodies) for element in value]
    if isinstance(value, Mapping):
        return {name: resolve(member, bodies) for name, member in value.items()}
    return value
