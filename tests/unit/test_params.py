"""Checking arguments and formatting them into paths and query parameters."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from enum import IntEnum, StrEnum
from typing import Any

import pytest

from financial_data import _params
from financial_data._params import Target

KEY = "demo-research-key"


def observations(series_id: object = "activity-index", **arguments: object) -> Target:
    """The target of `client.observations.page()`, with keyword arguments."""
    given: dict[str, object] = {
        "period_start": None,
        "period_end": None,
        "available_as_of": None,
        "page_size": None,
        "page_token": None,
    }
    given.update(arguments)
    return _params.observations(series_id, **given)


def query_value(target: Target, name: str) -> str:
    return dict(target.query)[name]


# Paths


def test_paths_without_arguments() -> None:
    assert _params.meta().path == "/v1/meta"
    assert _params.datasets().path == "/v1/datasets"
    assert _params.series_list().path == "/v1/series"
    assert _params.meta().query == ()


def test_paths_with_an_id() -> None:
    assert _params.dataset("core-indicators").path == "/v1/datasets/core-indicators"
    assert _params.series("activity-index").path == "/v1/series/activity-index"
    changes = _params.changes("core-indicators", 0, None)
    assert changes.path == "/v1/datasets/core-indicators/changes"
    assert changes.segments == ("v1", "datasets", "core-indicators", "changes")


@pytest.mark.parametrize(
    ("given", "encoded"),
    [
        ("a/b", "a%2Fb"),
        ("/", "%2F"),
        ("a b", "a%20b"),
        ("100%", "100%25"),
        ("%2F", "%252F"),
        ("a?b#c", "a%3Fb%23c"),
        ("a;b=c&d", "a%3Bb%3Dc%26d"),
        ("a:b@c", "a%3Ab%40c"),
        ("a+b", "a%2Bb"),
        ("café", "caf%C3%A9"),
        ("...", "..."),
        (".a", ".a"),
        ("a-b_c.d~e", "a-b_c.d~e"),
        # A lone surrogate has no UTF-8 encoding, and is sent as its code unit.
        ("a\ud800", "a%ED%A0%80"),
    ],
)
def test_path_id_is_percent_encoded_with_no_safe_characters(
    given: str, encoded: str
) -> None:
    assert _params.series(given).path == f"/v1/series/{encoded}"
    assert _params.dataset(given).path == f"/v1/datasets/{encoded}"
    assert _params.changes(given, 0, None).path == f"/v1/datasets/{encoded}/changes"
    # The segment itself is kept before encoding.
    assert _params.series(given).segments[-1] == given


PATH_ARGUMENTS = {
    "dataset": lambda value: _params.dataset(value),
    "series": lambda value: _params.series(value),
    "changes": lambda value: _params.changes(value, 0, None),
}
PATH_NAMES = {"dataset": "dataset_id", "series": "series_id", "changes": "dataset_id"}


@pytest.mark.parametrize("target", PATH_ARGUMENTS)
@pytest.mark.parametrize("value", ["", ".", ".."])
def test_id_that_would_address_another_path_is_refused(target: str, value: str) -> None:
    refusal = re.escape(f"{PATH_NAMES[target]} must not be empty, '.', or '..'")
    with pytest.raises(ValueError, match=f"^{refusal}") as caught:
        PATH_ARGUMENTS[target](value)
    assert caught.value.__context__ is None


@pytest.mark.parametrize("target", PATH_ARGUMENTS)
@pytest.mark.parametrize("value", [None, 1, b"activity-index", ["activity-index"]])
def test_path_id_that_is_not_a_string_is_refused(target: str, value: object) -> None:
    refusal = f"^{PATH_NAMES[target]} must be a str, not {type(value).__name__}$"
    with pytest.raises(TypeError, match=refusal):
        PATH_ARGUMENTS[target](value)


# Query parameters


@pytest.mark.parametrize("series_id", ["", ".", "..", "a/b", " ", "a\ud800", KEY])
def test_query_series_id_is_sent_as_given(series_id: str) -> None:
    target = observations(series_id)
    assert target.path == "/v1/observations"
    assert target.query == (("series_id", series_id),)


def test_arguments_left_none_are_not_sent() -> None:
    assert observations().query == (("series_id", "activity-index"),)
    assert observations(None).query == ()
    assert _params.changes("core-indicators", None, None).query == ()


def test_every_argument_is_sent_in_order() -> None:
    target = observations(
        period_start=date(2026, 8, 1),
        period_end="2026-09-01",
        available_as_of=datetime(2026, 9, 4, tzinfo=UTC),
        page_size=10,
        page_token="token-1",
    )
    assert target.query == (
        ("series_id", "activity-index"),
        ("period_start", "2026-08-01"),
        ("period_end", "2026-09-01"),
        ("available_as_of", "2026-09-04T00:00:00Z"),
        ("page_size", "10"),
        ("page_token", "token-1"),
    )
    changes = _params.changes("core-indicators", 16, 2)
    assert changes.query == (("after", "16"), ("limit", "2"))


@pytest.mark.parametrize(
    "value",
    ["", "yesterday", "2026-02-30", "2026-8-01", "2026-09-01T00:00:00Z", KEY],
)
@pytest.mark.parametrize("name", ["period_start", "period_end"])
def test_date_string_is_sent_as_given(name: str, value: str) -> None:
    assert query_value(observations(**{name: value}), name) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "yesterday",
        "2026-09-04T00:00:00.000Z",
        "2026-09-04T00:00:00+00:00",
        "2026-09-04t00:00:00z",
    ],
)
def test_timestamp_string_is_sent_as_given(value: str) -> None:
    target = observations(available_as_of=value)
    assert query_value(target, "available_as_of") == value


@pytest.mark.parametrize("value", ["", "token with spaces", "a/b?c=d&e", KEY])
def test_page_token_is_sent_as_given(value: str) -> None:
    assert query_value(observations(page_token=value), "page_token") == value


class Series(StrEnum):
    ACTIVITY = "activity-index"


def test_string_subclass_is_sent_as_its_text() -> None:
    target = observations(Series.ACTIVITY, page_token=Series.ACTIVITY)
    for _, value in target.query:
        assert type(value) is str
        assert value == "activity-index"
    segment = _params.series(Series.ACTIVITY).segments[-1]
    assert type(segment) is str


class Shown(str):
    """A str that shows something other than its text."""

    def __str__(self) -> str:
        return "shown"


def test_string_subclass_is_sent_as_its_text_not_as_it_shows() -> None:
    target = observations(Shown("activity-index"), period_start=Shown("2026-08-01"))
    assert target.query == (
        ("series_id", "activity-index"),
        ("period_start", "2026-08-01"),
    )
    assert _params.series(Shown("activity-index")).path == "/v1/series/activity-index"


# The path with the query string


def test_path_with_query_without_a_query() -> None:
    assert _params.meta().path_with_query == "/v1/meta"
    assert _params.series("a/b").path_with_query == "/v1/series/a%2Fb"


def test_path_with_query_encodes_names_and_values_as_path_segments() -> None:
    target = observations(
        "activity index",
        period_start="2026-08-01",
        available_as_of="2026-09-04T00:00:00Z",
        page_size=10,
        page_token="a+b/c=d&e",
    )
    assert target.path_with_query == (
        "/v1/observations?series_id=activity%20index&period_start=2026-08-01"
        "&available_as_of=2026-09-04T00%3A00%3A00Z&page_size=10"
        "&page_token=a%2Bb%2Fc%3Dd%26e"
    )
    changes = _params.changes("core-indicators", 16, 2)
    assert (
        changes.path_with_query
        == "/v1/datasets/core-indicators/changes?after=16&limit=2"
    )


def test_path_with_query_keeps_an_empty_value() -> None:
    assert observations("").path_with_query == "/v1/observations?series_id="


def test_query_value_with_a_lone_surrogate_is_sent_as_its_code_unit() -> None:
    target = observations("activity-index", page_token="a\ud800")
    assert target.path_with_query.endswith("&page_token=a%ED%A0%80")


def test_reported_path_without_the_key_is_the_path_as_sent() -> None:
    target = observations("activity-index", page_token="token/=")
    assert target.reported(KEY) == target.path_with_query


@pytest.mark.parametrize(
    "key", [KEY, "key/with=slash", "k%2F", "key+plus&amp", "a\\b", 'x"y']
)
def test_reported_path_redacts_each_segment_and_value_holding_the_key(
    key: str,
) -> None:
    assert _params.series(key).reported(key) == "/v1/series/[redacted]"
    assert _params.series(f"my-{key}-id").reported(key) == "/v1/series/[redacted]"
    changes = _params.changes(f"{key}!", 1, 2)
    assert changes.reported(key) == "/v1/datasets/[redacted]/changes?after=1&limit=2"
    target = observations(key, page_size=5, page_token=f"{key}{key}")
    assert target.reported(key) == (
        "/v1/observations?series_id=[redacted]&page_size=5&page_token=[redacted]"
    )


def test_reported_path_redacts_a_key_that_spans_two_parts() -> None:
    # The key is in no argument, but forms across the SDK's own text.
    assert _params.series("activity").reported("series/activity") == "/v1/[redacted]"
    assert (
        observations("abc").reported("id=abc") == "/v1/observations?series_[redacted]"
    )


# Dates


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (date(2026, 8, 1), "2026-08-01"),
        (date(2024, 2, 29), "2024-02-29"),
        (date(999, 1, 2), "0999-01-02"),
        (date(1, 1, 1), "0001-01-01"),
        (date(9999, 12, 31), "9999-12-31"),
    ],
)
@pytest.mark.parametrize("name", ["period_start", "period_end"])
def test_date_is_written_with_zero_padding(name: str, value: date, text: str) -> None:
    assert query_value(observations(**{name: value}), name) == text


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 8, 1),
        datetime(2026, 8, 1, tzinfo=UTC),
    ],
    ids=["naive", "aware"],
)
@pytest.mark.parametrize("name", ["period_start", "period_end"])
def test_datetime_is_refused_as_a_date(name: str, value: datetime) -> None:
    refusal = f"^{name} must be a date that is not a datetime, or a str, not datetime$"
    with pytest.raises(TypeError, match=refusal):
        observations(**{name: value})


@pytest.mark.parametrize("value", [20260801, True, b"2026-08-01", 1.5])
@pytest.mark.parametrize("name", ["period_start", "period_end"])
def test_date_of_the_wrong_type_is_refused(name: str, value: object) -> None:
    with pytest.raises(TypeError, match=f"{name} must be a date or a str, not "):
        observations(**{name: value})


# Timestamps


def offset(**delta: float) -> timezone:
    return timezone(timedelta(**delta))


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (datetime(2026, 9, 4, tzinfo=UTC), "2026-09-04T00:00:00Z"),
        (datetime(2026, 9, 4, 2, 0, tzinfo=offset(hours=2)), "2026-09-04T00:00:00Z"),
        (
            datetime(2026, 9, 3, 19, 30, tzinfo=offset(hours=-4, minutes=-30)),
            "2026-09-04T00:00:00Z",
        ),
        # Across a day, a month, and a year.
        (datetime(2026, 1, 1, 0, 30, tzinfo=offset(hours=1)), "2025-12-31T23:30:00Z"),
        (
            datetime(2025, 12, 31, 23, 30, tzinfo=offset(hours=-1)),
            "2026-01-01T00:30:00Z",
        ),
        (datetime(2024, 2, 29, 23, 0, tzinfo=offset(hours=-2)), "2024-03-01T01:00:00Z"),
        (datetime(5, 1, 1, tzinfo=UTC), "0005-01-01T00:00:00Z"),
        (datetime(999, 12, 31, 23, 59, 59, tzinfo=UTC), "0999-12-31T23:59:59Z"),
    ],
)
def test_datetime_is_converted_to_utc(value: datetime, text: str) -> None:
    target = observations(available_as_of=value)
    assert query_value(target, "available_as_of") == text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (datetime(2026, 9, 4, 0, 0, 0, 999_999, tzinfo=UTC), "2026-09-04T00:00:00Z"),
        (datetime(2026, 9, 4, 0, 0, 0, 1, tzinfo=UTC), "2026-09-04T00:00:00Z"),
        (
            datetime(2026, 9, 4, 0, 0, 59, 500_000, tzinfo=offset(hours=5)),
            "2026-09-03T19:00:59Z",
        ),
        # An offset with a fraction of a second: the instant is
        # 2026-09-03T23:59:59.999999Z, which truncates to the second before.
        (datetime(2026, 9, 4, tzinfo=offset(microseconds=1)), "2026-09-03T23:59:59Z"),
        (
            datetime(2026, 9, 4, 0, 0, 0, 999_999, tzinfo=offset(microseconds=-1)),
            "2026-09-04T00:00:01Z",
        ),
    ],
)
def test_datetime_is_truncated_to_whole_seconds(value: datetime, text: str) -> None:
    target = observations(available_as_of=value)
    assert query_value(target, "available_as_of") == text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (datetime(1, 1, 1, tzinfo=offset(hours=1)), "0000-12-31T23:00:00Z"),
        (datetime.max.replace(tzinfo=offset(hours=-1)), "10000-01-01T00:59:59Z"),
    ],
    ids=["before-year-1", "after-year-9999"],
)
def test_instant_outside_the_years_datetime_holds_is_written(
    value: datetime, text: str
) -> None:
    # astimezone would raise OverflowError; the API judges the instant.
    target = observations(available_as_of=value)
    assert query_value(target, "available_as_of") == text


class EasternFall(tzinfo):
    """US Eastern time on 2026-11-01, when 01:00 to 02:00 happens twice."""

    def utcoffset(self, value: datetime | None) -> timedelta:
        return timedelta(hours=-5 if value is not None and value.fold else -4)

    def dst(self, value: datetime | None) -> None:
        return None

    def tzname(self, value: datetime | None) -> None:
        return None


@pytest.mark.parametrize(("fold", "text"), [(0, "05:30:00Z"), (1, "06:30:00Z")])
def test_repeated_local_time_is_converted_by_its_fold(fold: int, text: str) -> None:
    value = datetime(2026, 11, 1, 1, 30, tzinfo=EasternFall(), fold=fold)
    target = observations(available_as_of=value)
    assert query_value(target, "available_as_of") == f"2026-11-01T{text}"


class Unknown(tzinfo):
    """A time zone that does not know its offset, which makes a datetime naive."""

    def utcoffset(self, value: datetime | None) -> None:
        return None

    def dst(self, value: datetime | None) -> None:
        return None

    def tzname(self, value: datetime | None) -> None:
        return None


@pytest.mark.parametrize(
    "value",
    [datetime(2026, 9, 4), datetime(2026, 9, 4, tzinfo=Unknown())],
    ids=["no-tzinfo", "tzinfo-without-offset"],
)
def test_naive_datetime_is_refused(value: datetime) -> None:
    with pytest.raises(ValueError, match="available_as_of must be timezone-aware"):
        observations(available_as_of=value)


@pytest.mark.parametrize(
    "value",
    [date(2026, 9, 4), 1_790_000_000, 1.5, True, b"2026-09-04T00:00:00Z"],
)
def test_timestamp_of_the_wrong_type_is_refused(value: object) -> None:
    with pytest.raises(
        TypeError,
        match="available_as_of must be a timezone-aware datetime or a str, not ",
    ):
        observations(available_as_of=value)


# Integers


class Size(IntEnum):
    TEN = 10


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0, "0"),
        (10, "10"),
        (-1, "-1"),
        (2**64, "18446744073709551616"),
        (Size.TEN, "10"),
    ],
)
def test_integer_is_written_in_decimal_digits(value: int, text: str) -> None:
    assert query_value(observations(page_size=value), "page_size") == text
    changes = _params.changes("core-indicators", value, value)
    assert changes.query == (("after", text), ("limit", text))


class Shows(int):
    """An int that converts and shows itself as something other than its value."""

    def __int__(self) -> int:
        return 5

    def __index__(self) -> int:
        return 6

    def __abs__(self) -> int:
        return 7

    def __repr__(self) -> str:
        return "repr"

    def __str__(self) -> str:
        return "str"

    def __format__(self, spec: str) -> str:
        return "format"


@pytest.mark.parametrize(("value", "text"), [(10, "10"), (-10, "-10")])
def test_integer_subclass_is_written_as_its_value(value: int, text: str) -> None:
    assert query_value(observations(page_size=Shows(value)), "page_size") == text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (10**5000, "1" + "0" * 5000),
        (-(10**4500) + 1, "-" + "9" * 4500),
        (10**1200 + 7, "1" + "0" * 1199 + "7"),
        (10**600, "1" + "0" * 600),
        (10**600 - 1, "9" * 600),
    ],
    ids=["5001-digits", "negative-4500-digits", "zero-part", "one-part", "no-part"],
)
def test_integer_longer_than_str_allows_is_written(value: int, text: str) -> None:
    # str() refuses more than 4300 digits by default; the API judges the size.
    assert query_value(observations(page_size=value), "page_size") == text


@pytest.mark.parametrize("value", [True, False, "10", 10.0, b"10", [10]])
def test_integer_of_the_wrong_type_is_refused(value: object) -> None:
    refusal = "must be an int that is not a bool, not "
    with pytest.raises(TypeError, match=f"page_size {refusal}"):
        observations(page_size=value)
    with pytest.raises(TypeError, match=f"after {refusal}"):
        _params.changes("core-indicators", value, None)
    with pytest.raises(TypeError, match=f"limit {refusal}"):
        _params.changes("core-indicators", 0, value)


@pytest.mark.parametrize("value", [1, b"token", ["token"]])
def test_query_string_of_the_wrong_type_is_refused(value: object) -> None:
    with pytest.raises(TypeError, match="series_id must be a str, not "):
        observations(value)
    with pytest.raises(TypeError, match="page_token must be a str, not "):
        observations(page_token=value)


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (
            lambda: observations(page_size=True),
            "page_size must be an int that is not a bool, not bool",
        ),
        (
            lambda: observations(period_start=b"2026-08-01"),
            "period_start must be a date or a str, not bytes",
        ),
        (
            lambda: observations(available_as_of=1.5),
            "available_as_of must be a timezone-aware datetime or a str, not float",
        ),
        (lambda: observations(page_token=["t"]), "page_token must be a str, not list"),
    ],
)
def test_type_error_names_the_argument_and_the_type_given(
    call: Any, message: str
) -> None:
    with pytest.raises(TypeError) as caught:
        call()
    assert str(caught.value) == message


def test_first_wrong_argument_is_named() -> None:
    with pytest.raises(TypeError, match="series_id"):
        observations(1, page_size=True)
    with pytest.raises(TypeError, match="dataset_id"):
        _params.changes(1, True, None)


# Messages


@pytest.mark.parametrize(
    "call",
    [
        lambda: _params.series(KEY.encode()),
        lambda: observations(KEY.encode()),
        lambda: observations(period_start=KEY.encode()),
        lambda: observations(available_as_of=KEY.encode()),
        lambda: observations(page_size=KEY.encode()),
        lambda: observations(page_token=KEY.encode()),
        lambda: observations(available_as_of=datetime(2026, 9, 4)),
        lambda: _params.series(".."),
    ],
)
def test_message_does_not_repeat_the_value(call: Any) -> None:
    with pytest.raises((TypeError, ValueError)) as caught:
        call()
    error = caught.value
    for text in [str(error), repr(error), *map(repr, error.args)]:
        assert KEY not in text
        assert "2026" not in text
    assert error.__cause__ is None
    assert error.__context__ is None
