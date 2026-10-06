"""The conversion of SDK results to JSON, as the SDK runner converts a result.

`spec/conformance.md` defines it, under "Converting a result". It is the
round trip the client contract promises, so an expected `"102.0"` matches
only if the SDK kept the value as `102.0`.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any


def to_json(result: object) -> Any:
    """Convert the result of one SDK call to JSON.

    A tuple is returned at the top level only by `list()`, and converts to
    `{"data": [...]}`. Any other tuple converts to an array.
    """
    if isinstance(result, tuple):
        return {"data": [_convert(item) for item in result]}
    return _convert(result)


def _convert(value: object) -> Any:
    # A record converts by its fields, so a property such as `caught_up` is
    # left out.
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _convert(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, tuple):
        return [_convert(item) for item in value]
    if isinstance(value, Enum):
        return _convert(value.value)
    if isinstance(value, str):
        return str.__str__(value)
    # bool is a subclass of int, and is kept as a boolean.
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    # datetime is a subclass of date, so it is converted first.
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, date):
        return f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
    raise TypeError(f"an SDK result has no JSON form: {type(value).__name__}")


def _timestamp(value: datetime) -> str:
    """Write a UTC timestamp with whole seconds as `YYYY-MM-DDTHH:MM:SSZ`.

    The SDK's timestamps are always so, and anything else is an SDK error,
    which the conversion reports rather than hides.
    """
    if value.utcoffset() != timedelta(0) or value.microsecond:
        raise ValueError(f"an SDK timestamp is not whole seconds in UTC: {value!r}")
    return (
        f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
        f"T{value.hour:02d}:{value.minute:02d}:{value.second:02d}Z"
    )
