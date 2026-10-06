"""The records' fields, their order, and their immutability."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from financial_data import (
    ChangePage,
    ChangeType,
    Dataset,
    Meta,
    MissingReason,
    ObservationPage,
    Revision,
    Series,
)

CONTRACT = Path(__file__).resolve().parents[2] / "contract"


def first_fixture_revision() -> dict[str, Any]:
    revisions = json.loads((CONTRACT / "fixtures" / "revisions.json").read_text())
    first: dict[str, Any] = revisions["revisions"][0]
    return first


def timestamp(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def revision() -> Revision:
    record = first_fixture_revision()
    return Revision(
        sequence=record["sequence"],
        series_id=record["series_id"],
        observation_id=record["observation_id"],
        revision_id=record["revision_id"],
        revision_number=record["revision_number"],
        change_type=ChangeType(record["change_type"]),
        period_start=date.fromisoformat(record["period_start"]),
        period_end=date.fromisoformat(record["period_end"]),
        value=Decimal(record["value"]),
        missing_reason=record["missing_reason"],
        unit=record["unit"],
        published_at=timestamp(record["published_at"]),
        received_at=timestamp(record["received_at"]),
        available_at=timestamp(record["available_at"]),
    )


def records() -> list[object]:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    return [
        revision(),
        Meta(
            api_version="v1",
            supported_api_versions=("v1",),
            contract_version="0.3.0",
            server_time=now,
        ),
        Dataset(
            dataset_id="core-indicators",
            name="Core indicators (synthetic)",
            description="Synthetic economic indicators.",
            entitled=True,
            head_position=37,
        ),
        Series(
            series_id="activity-index",
            dataset_id="core-indicators",
            name="Activity index (synthetic)",
            description="A fictional monthly index.",
            frequency="monthly",
            unit="index_points",
            base_period="2025 = 100",
            seasonal_adjustment="seasonally_adjusted",
            source="Fictional statistics office (synthetic)",
            release_schedule="Third day of the following month at 12:30 UTC",
            entitled=True,
        ),
        ObservationPage(
            data=(revision(),),
            position=37,
            snapshot_expires_at=now,
            next_page_token=None,
        ),
        ChangePage(data=(revision(),), next_position=37, head_position=37),
    ]


def test_revision_fields_follow_the_fixture_order() -> None:
    names = [field.name for field in dataclasses.fields(Revision)]
    assert names == list(first_fixture_revision())


@pytest.mark.parametrize(
    ("record", "names"),
    [
        (
            Meta,
            [
                "api_version",
                "supported_api_versions",
                "contract_version",
                "server_time",
            ],
        ),
        (
            Dataset,
            ["dataset_id", "name", "description", "entitled", "head_position"],
        ),
        (
            Series,
            [
                "series_id",
                "dataset_id",
                "name",
                "description",
                "frequency",
                "unit",
                "base_period",
                "seasonal_adjustment",
                "source",
                "release_schedule",
                "entitled",
            ],
        ),
        (
            ObservationPage,
            ["data", "position", "snapshot_expires_at", "next_page_token"],
        ),
        (ChangePage, ["data", "next_position", "head_position"]),
    ],
)
def test_record_fields(record: type, names: list[str]) -> None:
    assert [field.name for field in dataclasses.fields(record)] == names


@pytest.mark.parametrize("record", records(), ids=lambda record: type(record).__name__)
def test_records_are_frozen_with_slots(record: object) -> None:
    assert not hasattr(record, "__dict__")
    name = dataclasses.fields(record)[0].name  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(record, name, None)


@pytest.mark.parametrize(
    ("next_position", "head_position", "caught_up"),
    [(37, 37, True), (0, 0, True), (20, 37, False)],
)
def test_change_page_is_caught_up_at_the_head(
    next_position: int, head_position: int, caught_up: bool
) -> None:
    page = ChangePage(data=(), next_position=next_position, head_position=head_position)
    assert page.caught_up is caught_up


def test_change_types_are_the_contracts() -> None:
    assert {member.value for member in ChangeType} == {
        "initial_release",
        "source_revision",
        "provider_correction",
        "withdrawal",
    }
    withdrawal: str = "withdrawal"
    assert withdrawal == ChangeType.WITHDRAWAL
    assert isinstance(ChangeType.WITHDRAWAL, str)


def test_missing_reasons_are_the_contracts() -> None:
    assert {member.value for member in MissingReason} == {"not_collected"}
    not_collected: str = "not_collected"
    assert not_collected == MissingReason.NOT_COLLECTED
    assert isinstance(MissingReason.NOT_COLLECTED, str)
