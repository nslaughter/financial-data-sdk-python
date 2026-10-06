"""Checking arguments and formatting them into paths and query parameters.

Every function here is pure. Before anything is sent, the SDK checks only
types and the two inputs it cannot send faithfully: a naive `datetime`,
whose instant is unknown, and a path segment that is empty, `.`, or `..`,
which would address a different resource. Everything else, such as a
malformed date string or a page size out of range, is the API's to judge.

No message repeats the value it refuses, which may be the key passed by
mistake.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final
from urllib.parse import quote

_MICROSECOND: Final = timedelta(microseconds=1)
_LAST_ORDINAL: Final = date.max.toordinal()


@dataclass(frozen=True, slots=True)
class Target:
    """A request's path segments and query parameters, before percent-encoding."""

    segments: tuple[str, ...]
    query: tuple[tuple[str, str], ...] = ()

    @property
    def path(self) -> str:
        """The path, each segment percent-encoded with no safe characters."""
        return "".join(f"/{encode_segment(segment)}" for segment in self.segments)


def encode_segment(text: str) -> str:
    """Percent-encode a path segment with no safe characters, so `/` is too.

    A lone surrogate, which has no UTF-8 encoding, is encoded as its code
    unit, so the text is sent as Python holds it and the API judges it. A
    strict encoding would raise a `UnicodeEncodeError` holding the whole
    text.
    """
    return quote(text, safe="", errors="surrogatepass")


# Arguments


def _type_error(name: str, accepted: str, value: object) -> TypeError:
    return TypeError(f"{name} must be {accepted}, not {type(value).__name__}")


def _text(value: str) -> str:
    """Return a string, or a subclass's text, such as a `StrEnum` member's."""
    return str.__str__(value)


def string(name: str, value: object) -> str:
    """Check a `str` argument and return it exactly as given."""
    if not isinstance(value, str):
        raise _type_error(name, "a str", value)
    return _text(value)


def segment(name: str, value: object) -> str:
    """Check a `str` argument that is a path segment, before percent-encoding.

    `""`, `"."`, and `".."` are refused: percent-encoding leaves `.`
    unchanged, and httpx removes `.` and `..` segments, so each would address
    a different resource.
    """
    text = string(name, value)
    if text in ("", ".", ".."):
        raise ValueError(
            f"{name} must not be empty, '.', or '..', which in a path would "
            "address a different resource"
        )
    return text


def date_text(name: str, value: object) -> str:
    """Write a `date` that is not a `datetime` as `YYYY-MM-DD`; keep a `str`."""
    # datetime is a subclass of date, so it is refused first.
    if isinstance(value, datetime):
        raise _type_error(name, "a date that is not a datetime, or a str", value)
    if isinstance(value, date):
        return f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
    if isinstance(value, str):
        return _text(value)
    raise _type_error(name, "a date or a str", value)


def timestamp_text(name: str, value: object) -> str:
    """Write an aware `datetime` as `YYYY-MM-DDTHH:MM:SSZ`; keep a `str`.

    The instant is converted to UTC and truncated to whole seconds. Every
    `available_at` is a whole second, so truncating never changes a result.
    """
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if offset is None:
            raise ValueError(
                f"{name} must be timezone-aware: a naive datetime's instant is unknown"
            )
        return _utc_text(value, offset)
    if isinstance(value, str):
        return _text(value)
    raise _type_error(name, "a timezone-aware datetime or a str", value)


def _utc_text(value: datetime, offset: timedelta) -> str:
    """Write an instant in UTC, truncated to whole seconds.

    The arithmetic is on integers, so an instant on the day just outside the
    years `datetime` holds, where `astimezone` raises `OverflowError`, is
    still written, and the API judges it.
    """
    local = (
        value.toordinal() * 86_400
        + value.hour * 3_600
        + value.minute * 60
        + value.second
    ) * 1_000_000 + value.microsecond
    seconds = (local - offset // _MICROSECOND) // 1_000_000
    ordinal, second_of_day = divmod(seconds, 86_400)
    # An offset is less than a day, so the instant is at most one day
    # outside the ordinals date accepts.
    if ordinal < 1:
        year, month, day = 0, 12, 31
    elif ordinal > _LAST_ORDINAL:
        year, month, day = 10_000, 1, 1
    else:
        utc_date = date.fromordinal(ordinal)
        year, month, day = utc_date.year, utc_date.month, utc_date.day
    hour, rest = divmod(second_of_day, 3_600)
    minute, second = divmod(rest, 60)
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}Z"


def integer_text(name: str, value: object) -> str:
    """Write an `int` that is not a `bool` in decimal digits, `-` if negative."""
    # bool is a subclass of int, so it is refused first.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _type_error(name, "an int that is not a bool", value)
    return str(int(value))


def _query(
    *arguments: tuple[str, object, Callable[[str, object], str]],
) -> tuple[tuple[str, str], ...]:
    """Format each argument that is not `None`, in order, and leave out the rest."""
    return tuple(
        (name, format_argument(name, value))
        for name, value, format_argument in arguments
        if value is not None
    )


# Requests


def meta() -> Target:
    """`GET /v1/meta`."""
    return Target(("v1", "meta"))


def datasets() -> Target:
    """`GET /v1/datasets`."""
    return Target(("v1", "datasets"))


def dataset(dataset_id: object) -> Target:
    """`GET /v1/datasets/{dataset_id}`."""
    return Target(("v1", "datasets", segment("dataset_id", dataset_id)))


def series_list() -> Target:
    """`GET /v1/series`."""
    return Target(("v1", "series"))


def series(series_id: object) -> Target:
    """`GET /v1/series/{series_id}`."""
    return Target(("v1", "series", segment("series_id", series_id)))


def observations(
    series_id: object,
    period_start: object,
    period_end: object,
    available_as_of: object,
    page_size: object,
    page_token: object,
) -> Target:
    """`GET /v1/observations`, with each argument that is not `None`.

    `series_id` is a query parameter here, so it is sent as given, even
    empty: the API refuses an empty one.
    """
    return Target(
        ("v1", "observations"),
        _query(
            ("series_id", series_id, string),
            ("period_start", period_start, date_text),
            ("period_end", period_end, date_text),
            ("available_as_of", available_as_of, timestamp_text),
            ("page_size", page_size, integer_text),
            ("page_token", page_token, string),
        ),
    )


def changes(dataset_id: object, after: object, limit: object) -> Target:
    """`GET /v1/datasets/{dataset_id}/changes`, with each argument not `None`."""
    return Target(
        ("v1", "datasets", segment("dataset_id", dataset_id), "changes"),
        _query(("after", after, integer_text), ("limit", limit, integer_text)),
    )
