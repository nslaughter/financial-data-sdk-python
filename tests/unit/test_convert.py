"""The conversion of SDK results to JSON that the SDK runner uses."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from financial_data import ChangePage, ChangeType, Dataset, Meta, MissingReason
from tests.support.convert import to_json


def dataset(dataset_id: str) -> Dataset:
    return Dataset(
        dataset_id=dataset_id,
        name="Name",
        description="Description.",
        entitled=False,
        head_position=None,
    )


def test_tuple_from_list_converts_to_a_data_object() -> None:
    assert to_json((dataset("a"), dataset("b"))) == {
        "data": [
            {
                "dataset_id": "a",
                "name": "Name",
                "description": "Description.",
                "entitled": False,
                "head_position": None,
            },
            {
                "dataset_id": "b",
                "name": "Name",
                "description": "Description.",
                "entitled": False,
                "head_position": None,
            },
        ]
    }
    assert to_json(()) == {"data": []}


def test_tuple_inside_a_record_converts_to_an_array() -> None:
    meta = Meta(
        api_version="v1",
        supported_api_versions=("v1", "v2"),
        contract_version="0.3.0",
        server_time=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert to_json(meta) == {
        "api_version": "v1",
        "supported_api_versions": ["v1", "v2"],
        "contract_version": "0.3.0",
        "server_time": "2026-10-01T00:00:00Z",
    }


def test_property_is_left_out() -> None:
    page = ChangePage(data=(), next_position=20, head_position=20)
    assert to_json(page) == {"data": [], "next_position": 20, "head_position": 20}


@pytest.mark.parametrize(
    ("value", "converted"),
    [
        (ChangeType.WITHDRAWAL, "withdrawal"),
        (MissingReason.NOT_COLLECTED, "not_collected"),
        ("future_type", "future_type"),
        (True, True),
        (0, 0),
        (None, None),
        (Decimal("102.0"), "102.0"),
        (Decimal("0.0000001"), "0.0000001"),
        (Decimal("-0"), "-0"),
        (date(999, 1, 2), "0999-01-02"),
        (datetime(5, 1, 2, 3, 4, 5, tzinfo=UTC), "0005-01-02T03:04:05Z"),
        (datetime(2026, 9, 4, tzinfo=timezone(timedelta(0))), "2026-09-04T00:00:00Z"),
    ],
)
def test_value_converts_by_the_table(value: object, converted: object) -> None:
    assert to_json((value,)) == {"data": [converted]}
    assert type(to_json((value,))["data"][0]) is type(converted)


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 9, 4),
        datetime(2026, 9, 4, tzinfo=timezone(timedelta(hours=1))),
        datetime(2026, 9, 4, 0, 0, 0, 1, tzinfo=UTC),
    ],
    ids=["naive", "not-utc", "fraction"],
)
def test_timestamp_that_the_sdk_never_returns_is_refused(value: datetime) -> None:
    with pytest.raises(ValueError, match="not whole seconds in UTC"):
        to_json((value,))


def test_value_without_a_json_form_is_refused() -> None:
    with pytest.raises(TypeError, match="no JSON form: float"):
        to_json((1.5,))
