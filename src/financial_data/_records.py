"""The records the SDK returns, and the enums of their known values.

Every record is a frozen dataclass with slots. Field names are the API's JSON
member names, in the contract's order, so a record converts to the wire form
by name.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class ChangeType(StrEnum):
    """What created a revision. A value the SDK does not know stays a `str`."""

    INITIAL_RELEASE = "initial_release"
    SOURCE_REVISION = "source_revision"
    PROVIDER_CORRECTION = "provider_correction"
    WITHDRAWAL = "withdrawal"


class MissingReason(StrEnum):
    """Why a released observation has no value.

    A value the SDK does not know stays a `str`, as the contract requires.
    """

    NOT_COLLECTED = "not_collected"


@dataclass(frozen=True, slots=True)
class Revision:
    """One version of an observation, the same on every endpoint.

    A query returns the selected revision of each observation, and the change
    stream returns every revision in `sequence` order.
    """

    sequence: int
    series_id: str
    observation_id: str
    revision_id: str
    revision_number: int
    change_type: ChangeType | str
    period_start: date
    period_end: date
    """Exclusive."""
    value: Decimal | None
    missing_reason: MissingReason | str | None
    unit: str
    published_at: datetime
    received_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class Meta:
    """What `GET /v1/meta` describes: the server, its versions, and its clock."""

    api_version: str
    supported_api_versions: tuple[str, ...]
    contract_version: str
    server_time: datetime


@dataclass(frozen=True, slots=True)
class Dataset:
    """A dataset in the catalog, and whether the key may read its data."""

    dataset_id: str
    name: str
    description: str
    entitled: bool
    head_position: int | None
    """`None` when the key is not entitled."""


@dataclass(frozen=True, slots=True)
class Series:
    """A series in the catalog, and whether the key may read its data."""

    series_id: str
    dataset_id: str
    name: str
    description: str
    frequency: str
    unit: str
    base_period: str
    seasonal_adjustment: str
    source: str
    release_schedule: str
    entitled: bool


@dataclass(frozen=True, slots=True)
class ObservationPage:
    """One page of an observation query."""

    data: tuple[Revision, ...]
    position: int
    """The query's snapshot position, the same on every page."""
    snapshot_expires_at: datetime
    next_page_token: str | None
    """`None` on the last page."""


@dataclass(frozen=True, slots=True)
class ChangePage:
    """One page of a dataset's change stream."""

    data: tuple[Revision, ...]
    next_position: int
    head_position: int

    @property
    def caught_up(self) -> bool:
        """Whether this page reached the dataset's head position."""
        return self.next_position == self.head_position
