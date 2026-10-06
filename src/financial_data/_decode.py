"""Validating responses and decoding them into records.

Every function here is pure. A successful response that does not have its
documented form raises `InvalidResponse`, whose message names the member
and, for a record, its index in `data`. The message repeats nothing the
response holds, so it can never hold the key. The SDK raises
`UnexpectedResponseError` from it, outside any `except` block, so that no
exception it raises holds the body.

Members the SDK does not know are ignored, as the API's versioning requires.
The contract's invariants across records are the API's to enforce, and are
not checked here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final, TypeVar

from ._records import (
    ChangePage,
    ChangeType,
    Dataset,
    Meta,
    MissingReason,
    ObservationPage,
    Revision,
    Series,
)

# `[0-9]` rather than `\d`, which matches every Unicode digit, and fullmatch
# rather than `$`, which matches before a final newline.
_VALUE: Final = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_DATE: Final = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_TIMESTAMP: Final = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z"
)

_T = TypeVar("_T")

_CHANGE_TYPES: Final[dict[str, ChangeType]] = {
    member.value: member for member in ChangeType
}
_MISSING_REASONS: Final[dict[str, MissingReason]] = {
    member.value: member for member in MissingReason
}


class InvalidResponse(Exception):
    """A successful response that does not have its documented form.

    Its only argument is a message naming what is wrong, which repeats
    nothing the response holds.
    """


# Parsing


class _NotJSON:
    """The result of parsing a body that is not JSON."""


_NOT_JSON: Final = _NotJSON()


def _refuse_constant(name: str) -> object:
    # NaN, Infinity, and -Infinity, which Python's json accepts but JSON
    # does not have.
    raise ValueError("not JSON")


def _parse(body: bytes) -> object:
    """Parse a body as JSON, or return `_NOT_JSON` if it is not JSON."""
    try:
        return json.loads(body, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        return _NOT_JSON


def media_type(content_type: str | None) -> str | None:
    """Return a `Content-Type`'s media type in lowercase, without parameters."""
    if content_type is None:
        return None
    return content_type.partition(";")[0].strip().lower()


def json_object(body: bytes) -> dict[str, Any] | None:
    """Return a body as a JSON object, or `None` if it is not one."""
    document = _parse(body)
    return document if isinstance(document, dict) else None


def decode_object(content_type: str | None, body: bytes) -> dict[str, Any]:
    """Return a successful response's body, which must be a JSON object.

    The `Content-Type` must be `application/json`, ignoring parameters.
    """
    if media_type(content_type) != "application/json":
        raise InvalidResponse("the response's Content-Type is not application/json")
    document = _parse(body)
    if isinstance(document, _NotJSON):
        raise InvalidResponse("the response's body is not JSON")
    if not isinstance(document, dict):
        raise InvalidResponse(
            f"the response's body must be a JSON object, not {_json_type(document)}"
        )
    return document


# Members


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int | float):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    return "an object"


def _at(where: str, name: str) -> str:
    return f"{where}.{name}" if where else name


def _member(document: dict[str, Any], name: str, where: str) -> object:
    if name not in document:
        raise InvalidResponse(f"{_at(where, name)} is missing")
    return document[name]


def _string(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise InvalidResponse(f"{where} must be a string, not {_json_type(value)}")
    return value


def _optional_string(value: object, where: str) -> str | None:
    return None if value is None else _string(value, where)


def _integer(value: object, where: str) -> int:
    # bool is a subclass of int, and a JSON number with a fraction or an
    # exponent, such as 37.0, parses as a float.
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float):
            found = "a number with a fraction or an exponent"
        else:
            found = _json_type(value)
        raise InvalidResponse(f"{where} must be an integer, not {found}")
    return value


def _optional_integer(value: object, where: str) -> int | None:
    return None if value is None else _integer(value, where)


def _boolean(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidResponse(f"{where} must be a boolean, not {_json_type(value)}")
    return value


def _strings(value: object, where: str) -> tuple[str, ...]:
    return tuple(
        _string(item, f"{where}[{index}]")
        for index, item in enumerate(_array(value, where))
    )


def _array(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise InvalidResponse(f"{where} must be an array, not {_json_type(value)}")
    return value


def _object(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidResponse(f"{where} must be an object, not {_json_type(value)}")
    return value


def _value(value: object, where: str) -> Decimal | None:
    # Built from the text, never through float, so the digits and the
    # exponent are kept and format(value, "f") returns the text.
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidResponse(
            f"{where} must be a decimal string or null, not {_json_type(value)}"
        )
    if _VALUE.fullmatch(value) is None:
        raise InvalidResponse(
            rf"{where} must be a decimal string matching ^-?[0-9]+(\.[0-9]+)?$"
        )
    return Decimal(value)


def _date(value: object, where: str) -> date:
    match = _DATE.fullmatch(_string(value, where))
    parsed = None if match is None else _construct(date, *map(int, match.groups()))
    if parsed is None:
        raise InvalidResponse(f"{where} must be a valid date, YYYY-MM-DD")
    return parsed


def _timestamp(value: object, where: str) -> datetime:
    match = _TIMESTAMP.fullmatch(_string(value, where))
    parsed = (
        None
        if match is None
        else _construct(datetime, *map(int, match.groups()), tzinfo=UTC)
    )
    if parsed is None:
        raise InvalidResponse(
            f"{where} must be a valid timestamp, YYYY-MM-DDTHH:MM:SSZ"
        )
    return parsed


def _construct(kind: Callable[..., _T], *fields: int, **options: object) -> _T | None:
    """Construct a date or time from its fields, or return `None` if invalid.

    It returns rather than raises, so the `InvalidResponse` its caller
    raises has no `__context__`.
    """
    try:
        return kind(*fields, **options)
    except ValueError:  # an impossible date or time, such as February 30
        return None


def _change_type(value: object, where: str) -> ChangeType | str:
    text = _string(value, where)
    return _CHANGE_TYPES.get(text, text)


def _missing_reason(value: object, where: str) -> MissingReason | str | None:
    text = _optional_string(value, where)
    return None if text is None else _MISSING_REASONS.get(text, text)


# Records

_Decoder = Callable[[object, str], object]

_REVISION: Final[dict[str, _Decoder]] = {
    "sequence": _integer,
    "series_id": _string,
    "observation_id": _string,
    "revision_id": _string,
    "revision_number": _integer,
    "change_type": _change_type,
    "period_start": _date,
    "period_end": _date,
    "value": _value,
    "missing_reason": _missing_reason,
    "unit": _string,
    "published_at": _timestamp,
    "received_at": _timestamp,
    "available_at": _timestamp,
}

_META: Final[dict[str, _Decoder]] = {
    "api_version": _string,
    "supported_api_versions": _strings,
    "contract_version": _string,
    "server_time": _timestamp,
}

_DATASET: Final[dict[str, _Decoder]] = {
    "dataset_id": _string,
    "name": _string,
    "description": _string,
    "entitled": _boolean,
    "head_position": _optional_integer,
}

_SERIES: Final[dict[str, _Decoder]] = {
    "series_id": _string,
    "dataset_id": _string,
    "name": _string,
    "description": _string,
    "frequency": _string,
    "unit": _string,
    "base_period": _string,
    "seasonal_adjustment": _string,
    "source": _string,
    "release_schedule": _string,
    "entitled": _boolean,
}


def _fields(
    document: dict[str, Any], decoders: dict[str, _Decoder], where: str
) -> dict[str, Any]:
    """Decode each documented member, in order, and ignore any other."""
    return {
        name: decode(_member(document, name, where), _at(where, name))
        for name, decode in decoders.items()
    }


def _items(document: dict[str, Any], decoders: dict[str, _Decoder]) -> list[Any]:
    """Decode the records in `data`, each named by its index."""
    data = _array(_member(document, "data", ""), "data")
    return [
        _fields(_object(item, f"data[{index}]"), decoders, f"data[{index}]")
        for index, item in enumerate(data)
    ]


def _revisions(document: dict[str, Any]) -> tuple[Revision, ...]:
    return tuple(Revision(**fields) for fields in _items(document, _REVISION))


def decode_meta(document: dict[str, Any]) -> Meta:
    """Decode the body of `GET /v1/meta`."""
    return Meta(**_fields(document, _META, ""))


def decode_dataset(document: dict[str, Any]) -> Dataset:
    """Decode the body of `GET /v1/datasets/{dataset_id}`."""
    return Dataset(**_fields(document, _DATASET, ""))


def decode_datasets(document: dict[str, Any]) -> tuple[Dataset, ...]:
    """Decode the body of `GET /v1/datasets`."""
    return tuple(Dataset(**fields) for fields in _items(document, _DATASET))


def decode_series(document: dict[str, Any]) -> Series:
    """Decode the body of `GET /v1/series/{series_id}`."""
    return Series(**_fields(document, _SERIES, ""))


def decode_series_list(document: dict[str, Any]) -> tuple[Series, ...]:
    """Decode the body of `GET /v1/series`."""
    return tuple(Series(**fields) for fields in _items(document, _SERIES))


def decode_observation_page(document: dict[str, Any]) -> ObservationPage:
    """Decode the body of `GET /v1/observations`."""
    return ObservationPage(
        data=_revisions(document),
        position=_integer(_member(document, "position", ""), "position"),
        snapshot_expires_at=_timestamp(
            _member(document, "snapshot_expires_at", ""), "snapshot_expires_at"
        ),
        next_page_token=_optional_string(
            _member(document, "next_page_token", ""), "next_page_token"
        ),
    )


def decode_change_page(document: dict[str, Any]) -> ChangePage:
    """Decode the body of `GET /v1/datasets/{dataset_id}/changes`."""
    return ChangePage(
        data=_revisions(document),
        next_position=_integer(_member(document, "next_position", ""), "next_position"),
        head_position=_integer(_member(document, "head_position", ""), "head_position"),
    )
