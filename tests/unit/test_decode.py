"""Validating responses and decoding them into records."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from financial_data import ChangeType, MissingReason, Revision
from financial_data._decode import (
    InvalidResponse,
    decode_change_page,
    decode_dataset,
    decode_datasets,
    decode_meta,
    decode_object,
    decode_observation_page,
    decode_series,
    decode_series_list,
    json_object,
    media_type,
)
from tests.support.convert import to_json

CONTRACT = Path(__file__).resolve().parents[2] / "contract"
KEY = "demo-research-key"


def contract_file(*parts: str) -> Any:
    return json.loads(CONTRACT.joinpath(*parts).read_text())


def fixture_revisions() -> list[dict[str, Any]]:
    revisions: list[dict[str, Any]] = contract_file("fixtures", "revisions.json")[
        "revisions"
    ]
    return revisions


def fixture_revision(revision_id: str) -> dict[str, Any]:
    return next(r for r in fixture_revisions() if r["revision_id"] == revision_id)


# Valid responses, built from the fixtures and the API specification's
# examples, with exactly the documented members.


def meta_document() -> dict[str, Any]:
    return {
        "api_version": "v1",
        "supported_api_versions": ["v1"],
        "contract_version": "0.3.0",
        "server_time": "2026-10-01T00:00:00Z",
    }


def dataset_document() -> dict[str, Any]:
    dataset = contract_file("fixtures", "datasets.json")["datasets"][0]
    return {**dataset, "entitled": True, "head_position": 37}


def series_document() -> dict[str, Any]:
    series = contract_file("fixtures", "series.json")["series"][0]
    return {**series, "entitled": True}


def observation_page_document() -> dict[str, Any]:
    return {
        "data": [
            fixture_revision("rev_oct24_1"),
            fixture_revision("rev_jul26_1"),
            fixture_revision("rev_aug26_2"),
        ],
        "position": 37,
        "snapshot_expires_at": "2026-10-01T01:00:00Z",
        "next_page_token": "token-1",
    }


def change_page_document() -> dict[str, Any]:
    return {
        "data": [fixture_revision("rev_may25_2"), fixture_revision("rev_jun25_1")],
        "next_position": 19,
        "head_position": 37,
    }


Decode = Callable[[dict[str, Any]], object]

ENDPOINTS: dict[str, tuple[Decode, Callable[[], dict[str, Any]]]] = {
    "meta": (decode_meta, meta_document),
    "dataset": (decode_dataset, dataset_document),
    "datasets": (decode_datasets, lambda: {"data": [dataset_document()]}),
    "series": (decode_series, series_document),
    "series-list": (decode_series_list, lambda: {"data": [series_document()]}),
    "observation-page": (decode_observation_page, observation_page_document),
    "change-page": (decode_change_page, change_page_document),
}

# Each member's kind, by name. A record in data is an "object".
KINDS = {
    "api_version": "string",
    "supported_api_versions": "strings",
    "contract_version": "string",
    "server_time": "timestamp",
    "dataset_id": "string",
    "name": "string",
    "description": "string",
    "entitled": "boolean",
    "head_position": "integer",
    "series_id": "string",
    "frequency": "string",
    "unit": "string",
    "base_period": "string",
    "seasonal_adjustment": "string",
    "source": "string",
    "release_schedule": "string",
    "data": "array",
    "position": "integer",
    "snapshot_expires_at": "timestamp",
    "next_page_token": "string",
    "next_position": "integer",
    "sequence": "integer",
    "observation_id": "string",
    "revision_id": "string",
    "revision_number": "integer",
    "change_type": "string",
    "period_start": "date",
    "period_end": "date",
    "value": "decimal",
    "missing_reason": "string",
    "published_at": "timestamp",
    "received_at": "timestamp",
    "available_at": "timestamp",
}


def nullable(endpoint: str, name: str) -> bool:
    if name == "head_position":
        # Null when the key is not entitled; a change page always has one.
        return endpoint in ("dataset", "datasets")
    return name in ("value", "missing_reason", "next_page_token")


Location = tuple[str | int, ...]


def members(document: dict[str, Any]) -> list[tuple[str, str, Location]]:
    """Every documented member of a response: its name, where, and location."""
    found: list[tuple[str, str, Location]] = []
    for name, value in document.items():
        found.append((name, name, (name,)))
        if name == "data":
            for index, record in enumerate(value):
                where = f"data[{index}]"
                found.append(("", where, ("data", index)))
                found += [(m, f"{where}.{m}", ("data", index, m)) for m in record]
    return found


def changed(
    document: dict[str, Any], location: Location, value: Any = ...
) -> dict[str, Any]:
    """Set the member at a location, or remove it when no value is given."""
    parent: Any = document
    for step in location[:-1]:
        parent = parent[step]
    if value is ...:
        del parent[location[-1]]
    else:
        parent[location[-1]] = value
    return document


def refused(decode: Decode, document: dict[str, Any]) -> str:
    """Decode a response that must be refused, and return the message."""
    with pytest.raises(InvalidResponse) as caught:
        decode(document)
    error = caught.value
    # Raised outside any except block, so nothing holds the body.
    assert error.__cause__ is None
    assert error.__context__ is None
    assert len(error.args) == 1
    message: str = error.args[0]
    return message


MEMBERS = [
    (endpoint, name, where, location)
    for endpoint, (_, build) in ENDPOINTS.items()
    for name, where, location in members(build())
]


def member_cases(keep: Callable[[str, str], bool]) -> list[Any]:
    """The members `keep(endpoint, name)` selects, each named for its test."""
    return [
        pytest.param(endpoint, name, where, location, id=f"{endpoint}:{where}")
        for endpoint, name, where, location in MEMBERS
        if keep(endpoint, name)
    ]


# Malformed dates and timestamps: every form the API's request-errors file
# refuses as invalid_parameter. Its period-range scenario refuses valid
# dates in a reversed or empty range, so it is left out.


def malformed(parameters: set[str]) -> list[str]:
    forms: list[str] = []
    for scenario in contract_file("expected", "request-errors.json")["scenarios"]:
        if scenario["name"] == "a period range must not be empty or reversed":
            continue
        for step in scenario["steps"]:
            expect = step.get("expect", {})
            parameter = expect.get("body", {}).get("parameter")
            value = step.get("request", {}).get("query", {}).get(parameter)
            if (
                expect.get("code") == "invalid_parameter"
                and parameter in parameters
                and isinstance(value, str)
                and value not in forms
            ):
                forms.append(value)
    return forms


MALFORMED_TIMESTAMPS = malformed({"available_as_of"})
MALFORMED_DATES = malformed({"period_start", "period_end"})


def test_malformed_forms_come_from_the_request_errors_file() -> None:
    assert MALFORMED_TIMESTAMPS == [
        "",
        "2026-09-04T00:00:00.000Z",
        "2026-09-04T00:00:00+00:00",
        "2026-09-04",
        "2026-09-04t00:00:00z",
        "2026-09-04 00:00:00Z",
        "2026-09-04T24:00:00Z",
        "2026-09-04T00:00:60Z",
        "2026-02-30T00:00:00Z",
        "yesterday",
    ]
    assert MALFORMED_DATES == ["2026-02-30", "2026-8-01", "2026-09-01T00:00:00Z"]


# Wrong JSON types for each kind of member. null is added for every member
# that is not nullable.
WRONG_TYPES: dict[str, list[Any]] = {
    "string": [1, 1.5, True, [], {}],
    "strings": ["v1", 1, {}],
    "integer": ["37", 37.0, 1e2, True, False, [], {}],
    "boolean": ["true", 1, 0, [], {}],
    "date": [20260801, True, [], {}],
    "timestamp": [1_790_000_000, True, [], {}],
    "decimal": [102.1, 102, True, [], {}],
    "array": [{}, "data", 1, True],
    "object": [[], "record", 1, True],
}

WRONG_TYPE_CASES = [
    pytest.param(
        endpoint, where, location, value, id=f"{endpoint}:{where}={json.dumps(value)}"
    )
    for endpoint, name, where, location in MEMBERS
    for value in WRONG_TYPES[KINDS.get(name, "object")]
    + ([] if nullable(endpoint, name) else [None])
]


# Every response


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_valid_response_converts_back_to_exactly_its_json(endpoint: str) -> None:
    decode, build = ENDPOINTS[endpoint]
    document = build()
    converted = to_json(decode(document))
    assert converted == document
    # The same members in the same order, with the same JSON types.
    assert json.dumps(converted) == json.dumps(document)


@pytest.mark.parametrize(
    ("endpoint", "name", "where", "location"),
    # A record in data is not a member, and removing one leaves a valid list.
    member_cases(lambda endpoint, name: bool(name)),
)
def test_missing_member_is_refused_and_named(
    endpoint: str, name: str, where: str, location: Location
) -> None:
    decode, build = ENDPOINTS[endpoint]
    message = refused(decode, changed(build(), location))
    assert message == f"{where} is missing"


@pytest.mark.parametrize(("endpoint", "where", "location", "value"), WRONG_TYPE_CASES)
def test_wrong_json_type_is_refused_and_named(
    endpoint: str, where: str, location: Location, value: Any
) -> None:
    decode, build = ENDPOINTS[endpoint]
    message = refused(decode, changed(build(), location, value))
    assert message.startswith(f"{where} must be ")


@pytest.mark.parametrize(
    ("endpoint", "name", "where", "location"),
    member_cases(nullable),
)
def test_nullable_member_may_be_null(
    endpoint: str, name: str, where: str, location: Location
) -> None:
    decode, build = ENDPOINTS[endpoint]
    document = changed(build(), location, None)
    record = decode(document)
    assert to_json(record) == document


def test_null_is_refused_where_it_is_not_allowed() -> None:
    message = refused(
        decode_observation_page,
        changed(observation_page_document(), ("data", 2, "change_type"), None),
    )
    assert message == "data[2].change_type must be a string, not null"


def test_integer_with_a_fraction_is_refused() -> None:
    document = changed(observation_page_document(), ("data", 0, "sequence"), 37.0)
    message = refused(decode_observation_page, document)
    assert message == (
        "data[0].sequence must be an integer, "
        "not a number with a fraction or an exponent"
    )


@pytest.mark.parametrize("value", [True, False])
def test_boolean_integer_is_refused(value: bool) -> None:
    document = changed(change_page_document(), ("next_position",), value)
    message = refused(decode_change_page, document)
    assert message == "next_position must be an integer, not a boolean"


def test_large_integer_is_kept() -> None:
    document = changed(change_page_document(), ("head_position",), 2**53 + 1)
    assert decode_change_page(document).head_position == 2**53 + 1


def test_boolean_must_be_a_json_boolean() -> None:
    message = refused(decode_dataset, changed(dataset_document(), ("entitled",), 1))
    assert message == "entitled must be a boolean, not a number"


def test_each_supported_version_must_be_a_string() -> None:
    document = changed(meta_document(), ("supported_api_versions",), ["v1", 2])
    message = refused(decode_meta, document)
    assert message == "supported_api_versions[1] must be a string, not a number"


def test_record_in_data_must_be_an_object() -> None:
    document = changed(observation_page_document(), ("data", 1), ["sequence"])
    message = refused(decode_observation_page, document)
    assert message == "data[1] must be an object, not an array"


def test_unknown_members_are_ignored() -> None:
    document = observation_page_document()
    expected = decode_observation_page(observation_page_document())
    document["quality"] = "final"
    document["data"][0]["quality"] = "final"
    document["data"][0]["nested"] = {"anything": [1, None]}
    assert decode_observation_page(document) == expected


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_unknown_members_of_every_response_are_ignored(endpoint: str) -> None:
    decode, build = ENDPOINTS[endpoint]
    document = build()
    document["later_field"] = {"added": "in a later version"}
    assert decode(document) == decode(build())


def test_message_repeats_nothing_from_the_response() -> None:
    for location in [("data", 0, "available_at"), ("data", 0, "value")]:
        document = changed(observation_page_document(), location, f"{KEY}!")
        message = refused(decode_observation_page, document)
        assert KEY not in message


# Revisions


def test_every_fixture_revision_decodes_and_converts_back_to_its_json() -> None:
    revisions = fixture_revisions()
    pages = [
        decode_change_page(
            {"data": revisions, "next_position": 37, "head_position": 37}
        ),
        decode_observation_page(
            {
                "data": revisions,
                "position": 37,
                "snapshot_expires_at": "2026-10-01T01:00:00Z",
                "next_page_token": None,
            }
        ),
    ]
    for page in pages:
        converted = to_json(page)["data"]
        assert len(converted) == len(revisions) == 37
        for record, revision in zip(converted, revisions, strict=True):
            # The same members, in the same order, with the same values.
            assert list(record.items()) == list(revision.items())
            assert json.dumps(record) == json.dumps(revision)


def test_fixture_revisions_decode_to_their_types() -> None:
    page = decode_change_page(
        {"data": fixture_revisions(), "next_position": 37, "head_position": 37}
    )
    for revision in page.data:
        assert type(revision) is Revision
        assert type(revision.sequence) is int
        assert type(revision.revision_number) is int
        assert type(revision.change_type) is ChangeType
        assert type(revision.period_start) is date
        assert type(revision.period_end) is date
        assert revision.value is None or type(revision.value) is Decimal
        assert revision.missing_reason is None or (
            type(revision.missing_reason) is MissingReason
        )
        for timestamp in (
            revision.published_at,
            revision.received_at,
            revision.available_at,
        ):
            assert type(timestamp) is datetime
            assert timestamp.tzinfo is UTC
            assert timestamp.microsecond == 0


def test_missing_value_and_withdrawal_are_records() -> None:
    page = decode_change_page(
        {"data": fixture_revisions(), "next_position": 37, "head_position": 37}
    )
    by_id = {revision.revision_id: revision for revision in page.data}
    not_collected = by_id["rev_oct24_1"]
    assert not_collected.value is None
    assert not_collected.missing_reason == MissingReason.NOT_COLLECTED
    withdrawal = by_id["rev_may25_2"]
    assert withdrawal.change_type is ChangeType.WITHDRAWAL
    assert withdrawal.value is None
    assert withdrawal.missing_reason is None


def test_aug26_revision_decodes_to_its_fields() -> None:
    page = decode_change_page(
        {
            "data": [fixture_revision("rev_aug26_2")],
            "next_position": 37,
            "head_position": 37,
        }
    )
    assert page.data == (
        Revision(
            sequence=37,
            series_id="activity-index",
            observation_id="obs_aug26",
            revision_id="rev_aug26_2",
            revision_number=2,
            change_type=ChangeType.SOURCE_REVISION,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 9, 1),
            value=Decimal("102.1"),
            missing_reason=None,
            unit="index_points",
            published_at=datetime(2026, 9, 10, 12, 30, 0, tzinfo=UTC),
            received_at=datetime(2026, 9, 10, 12, 30, 4, tzinfo=UTC),
            available_at=datetime(2026, 9, 10, 12, 30, 40, tzinfo=UTC),
        ),
    )


def revision_with(member: str, value: Any) -> dict[str, Any]:
    return {
        "data": [{**fixture_revision("rev_aug26_2"), member: value}],
        "next_position": 37,
        "head_position": 37,
    }


@pytest.mark.parametrize(
    "text",
    [
        "102.0",
        "0.0000001",
        "-0",
        "12345678901234567890.123",
        "0",
        "-0.0",
        "1.10",
        "100",
        "0.000",
        "-12.5",
        "99999999999999999999999999999999999999.000000000000000000000001",
    ],
)
def test_value_keeps_its_text(text: str) -> None:
    value = decode_change_page(revision_with("value", text)).data[0].value
    assert type(value) is Decimal
    assert format(value, "f") == text


def test_value_with_leading_zeros_is_accepted() -> None:
    # The contract's pattern admits leading zeros, which a Decimal drops; no
    # fixture has one (spec/client.md, Open questions).
    value = decode_change_page(revision_with("value", "007.5")).data[0].value
    assert value == Decimal("7.5")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "-",
        "1e5",
        "1E5",
        "+1",
        "1.",
        ".5",
        "-.5",
        " 1",
        "1 ",
        "1\n",
        "1.2.3",
        "--1",
        "1,5",
        "1_000",
        "0x10",
        "NaN",
        "Infinity",
        chr(0x0661),  # ARABIC-INDIC DIGIT ONE, which \d would match
        chr(0xFF11),  # FULLWIDTH DIGIT ONE
    ],
)
def test_value_that_does_not_match_the_pattern_is_refused(text: str) -> None:
    message = refused(decode_change_page, revision_with("value", text))
    assert (
        message
        == r"data[0].value must be a decimal string matching ^-?[0-9]+(\.[0-9]+)?$"
    )


def test_value_that_is_a_json_number_is_refused() -> None:
    message = refused(decode_change_page, revision_with("value", 102.1))
    assert message == "data[0].value must be a decimal string or null, not a number"


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("future_type", "change_type"),
        ("INITIAL_RELEASE", "change_type"),
        ("", "change_type"),
        ("not_published_yet", "missing_reason"),
    ],
)
def test_unknown_change_type_and_missing_reason_are_kept_as_strings(
    text: str, kind: str
) -> None:
    revision = decode_change_page(revision_with(kind, text)).data[0]
    kept = getattr(revision, kind)
    assert type(kept) is str
    assert kept == text


def test_known_change_types_are_members() -> None:
    for member in ChangeType:
        revision = decode_change_page(revision_with("change_type", member.value))
        assert revision.data[0].change_type is member


# Dates and timestamps


@pytest.mark.parametrize("text", MALFORMED_DATES)
@pytest.mark.parametrize("member", ["period_start", "period_end"])
def test_malformed_date_is_refused(member: str, text: str) -> None:
    message = refused(decode_change_page, revision_with(member, text))
    assert message == f"data[0].{member} must be a valid date, YYYY-MM-DD"


@pytest.mark.parametrize(
    "text",
    [
        "2026-02-29",
        "2026-13-01",
        "2026-00-10",
        "2026-01-00",
        "2026-01-32",
        "2026-04-31",
        "0000-01-01",  # before the first year a date can hold
        "20260101",
        "2026-01-01\n",
        " 2026-01-01",
        "".join(map(chr, [0xFF12, 0xFF10, 0xFF12, 0xFF16])) + "-01-01",  # fullwidth
    ],
)
def test_impossible_or_malformed_date_is_refused(text: str) -> None:
    message = refused(decode_change_page, revision_with("period_start", text))
    assert message == "data[0].period_start must be a valid date, YYYY-MM-DD"


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("2024-02-29", date(2024, 2, 29)),
        ("0001-01-01", date(1, 1, 1)),
        ("9999-12-31", date(9999, 12, 31)),
    ],
)
def test_valid_date_is_decoded(text: str, parsed: date) -> None:
    revision = decode_change_page(revision_with("period_start", text)).data[0]
    assert revision.period_start == parsed
    assert to_json(revision)["period_start"] == text


TIMESTAMP_MEMBERS = [
    ("change-page", ("data", 0, "published_at")),
    ("change-page", ("data", 0, "received_at")),
    ("change-page", ("data", 0, "available_at")),
    ("observation-page", ("snapshot_expires_at",)),
    ("meta", ("server_time",)),
]


@pytest.mark.parametrize("text", MALFORMED_TIMESTAMPS)
@pytest.mark.parametrize(
    ("endpoint", "location"),
    TIMESTAMP_MEMBERS,
    ids=[location[-1] for _, location in TIMESTAMP_MEMBERS],
)
def test_malformed_timestamp_is_refused(
    endpoint: str, location: Location, text: str
) -> None:
    decode, build = ENDPOINTS[endpoint]
    message = refused(decode, changed(build(), location, text))
    where = ".".join(
        f"[{step}]" if isinstance(step, int) else str(step) for step in location
    ).replace(".[", "[")
    assert message == f"{where} must be a valid timestamp, YYYY-MM-DDTHH:MM:SSZ"


@pytest.mark.parametrize(
    "text",
    [
        "2026-09-10T12:30:40+00:00",
        "2026-09-10T12:30Z",
        "2026-09-10T12:60:00Z",
        "2026-09-10T12:30:40.5Z",
        "2026-09-10T12:30:40ZZ",
        "2026-09-10T12:30:40Z\n",
        "2026-09-10T12:30:40",
        "2025-02-29T00:00:00Z",
        "0000-01-01T00:00:00Z",
    ],
)
def test_impossible_or_malformed_timestamp_is_refused(text: str) -> None:
    message = refused(decode_change_page, revision_with("available_at", text))
    assert message == (
        "data[0].available_at must be a valid timestamp, YYYY-MM-DDTHH:MM:SSZ"
    )


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("2026-09-10T12:30:40Z", datetime(2026, 9, 10, 12, 30, 40, tzinfo=UTC)),
        ("2024-02-29T23:59:59Z", datetime(2024, 2, 29, 23, 59, 59, tzinfo=UTC)),
        ("0001-01-01T00:00:00Z", datetime(1, 1, 1, tzinfo=UTC)),
        ("9999-12-31T23:59:59Z", datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)),
    ],
)
def test_valid_timestamp_is_decoded_in_utc(text: str, parsed: datetime) -> None:
    revision = decode_change_page(revision_with("available_at", text)).data[0]
    assert revision.available_at == parsed
    assert revision.available_at.tzinfo is UTC
    assert to_json(revision)["available_at"] == text


# The body


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "application/json; charset=utf-8",
        "application/json;charset=UTF-8",
        "Application/JSON",
        " application/json ",
    ],
)
def test_json_body_is_accepted(content_type: str) -> None:
    assert decode_object(content_type, b'{"a": [1, "b"]}') == {"a": [1, "b"]}


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "",
        "text/html",
        "text/plain",
        "application/problem+json",
        "application/jsonx",
        "application/json-seq",
        "application/x-ndjson",
        "json",
    ],
)
def test_body_that_is_not_marked_json_is_refused(content_type: str | None) -> None:
    with pytest.raises(InvalidResponse) as caught:
        decode_object(content_type, b"{}")
    assert str(caught.value) == "the response's Content-Type is not application/json"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"<html></html>",
        b"{",
        b'{"a": 1,}',
        b'{"a": NaN}',
        b'{"a": Infinity}',
        b'{"a": -Infinity}',
        b'{"a": "\xff"}',
        KEY.encode(),
    ],
    ids=[
        "empty",
        "html",
        "unterminated",
        "trailing-comma",
        "nan",
        "infinity",
        "negative-infinity",
        "invalid-utf-8",
        "key",
    ],
)
def test_body_that_is_not_json_is_refused(body: bytes) -> None:
    with pytest.raises(InvalidResponse) as caught:
        decode_object("application/json", body)
    error = caught.value
    assert str(error) == "the response's body is not JSON"
    assert error.__cause__ is None
    assert error.__context__ is None


def test_body_too_deep_to_parse_is_not_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def too_deep(*args: object, **kwargs: object) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(json, "loads", too_deep)
    with pytest.raises(InvalidResponse) as caught:
        decode_object("application/json", b"[[[]]]")
    assert str(caught.value) == "the response's body is not JSON"
    assert caught.value.__context__ is None


def test_deeply_nested_body_is_refused() -> None:
    # How deep the parser goes depends on the Python version and the
    # platform's stack, so the body is refused either as too deep to parse
    # or as an array.
    body = b"[" * 100_000 + b"]" * 100_000
    with pytest.raises(InvalidResponse) as caught:
        decode_object("application/json", body)
    assert str(caught.value) in (
        "the response's body is not JSON",
        "the response's body must be a JSON object, not an array",
    )


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (b"[]", "an array"),
        (b"null", "null"),
        (b"1", "a number"),
        (b'"demo-research-key"', "a string"),
        (b"true", "a boolean"),
    ],
)
def test_body_that_is_not_an_object_is_refused(body: bytes, kind: str) -> None:
    with pytest.raises(InvalidResponse) as caught:
        decode_object("application/json", body)
    assert str(caught.value) == f"the response's body must be a JSON object, not {kind}"


def test_json_object_returns_none_for_anything_else() -> None:
    assert json_object(b'{"code": "x"}') == {"code": "x"}
    assert json_object(b"[]") is None
    assert json_object(b"<html>") is None
    assert json_object(b'{"a": NaN}') is None


@pytest.mark.parametrize(
    ("content_type", "kind"),
    [
        (None, None),
        ("application/json", "application/json"),
        ("Application/Problem+JSON; charset=utf-8", "application/problem+json"),
        ("text/html;charset=utf-8", "text/html"),
    ],
)
def test_media_type_ignores_case_and_parameters(
    content_type: str | None, kind: str | None
) -> None:
    assert media_type(content_type) == kind
