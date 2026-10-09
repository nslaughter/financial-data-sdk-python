"""The conversion of revisions to a pandas DataFrame (D6).

`spec/client.md` defines it, under "Dataframes". CI runs these tests with
the lowest and the latest pandas that install on each Python version, which
infer different dtypes, so each dtype here is checked against both.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from collections.abc import Hashable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pandas as pd
import pytest

from financial_data import ChangeType, Client, MissingReason, RetryPolicy, Revision
from financial_data._decode import decode_change_page
from financial_data.pandas import to_dataframe
from tests.support.convert import to_json
from tests.support.fake_api import REVISIONS, FakeAPI

# The contract's order, which is the fixture's.
COLUMNS = list(REVISIONS[0])

DTYPES = {
    "sequence": "int64",
    "series_id": "object",
    "observation_id": "object",
    "revision_id": "object",
    "revision_number": "int64",
    "change_type": "object",
    "period_start": "datetime64[us]",
    "period_end": "datetime64[us]",
    "value": "object",
    "missing_reason": "object",
    "unit": "object",
    "published_at": "datetime64[us, UTC]",
    "received_at": "datetime64[us, UTC]",
    "available_at": "datetime64[us, UTC]",
}


def fixture_revisions() -> tuple[Revision, ...]:
    """Every revision in `contract/fixtures/revisions.json`, decoded by the SDK."""
    page = decode_change_page(
        {"data": REVISIONS, "next_position": 37, "head_position": 37}
    )
    return page.data


def fixture_revision(revision_id: str) -> Revision:
    return next(r for r in fixture_revisions() if r.revision_id == revision_id)


def generate(revisions: tuple[Revision, ...]) -> Iterator[Revision]:
    yield from revisions


def lookalike(revision: Revision) -> SimpleNamespace:
    """An object that is not a `Revision` but has every field of `revision`."""
    fields = dataclasses.fields(revision)
    return SimpleNamespace(**{f.name: getattr(revision, f.name) for f in fields})


def row(frame: pd.DataFrame, revision_id: str) -> dict[Hashable, Any]:
    """The one row of the frame that holds `revision_id`."""
    rows = frame[frame["revision_id"] == revision_id].to_dict("records")
    assert len(rows) == 1
    return rows[0]


def cell_json(name: str, cell: object) -> object:
    """Write one cell as the API writes the field, checking the cell's type.

    A cell converts only if it holds what the contract's column holds, so a
    row matches its fixture record only if every field survived exactly.
    """
    if DTYPES[name] == "int64":
        assert type(cell) is int, (name, cell)
        return cell
    if name == "value":
        assert cell is None or type(cell) is Decimal, (name, cell)
        return to_json(cell)
    if DTYPES[name] == "object":
        # Only `missing_reason` is nullable among the text fields.
        assert type(cell) is str or (name == "missing_reason" and cell is None)
        return cell
    assert isinstance(cell, pd.Timestamp), (name, cell)
    if DTYPES[name] == "datetime64[us]":
        assert cell.tzinfo is None, (name, cell)
        assert cell == cell.normalize(), f"{name} is not at midnight: {cell}"
        return to_json(cell.date())
    assert cell.tzinfo is UTC, (name, cell)
    return to_json(cell.to_pydatetime())


def frame_json(frame: pd.DataFrame) -> list[dict[str, object]]:
    """Write each row as the API writes its record."""
    return [
        {str(name): cell_json(str(name), cell) for name, cell in record.items()}
        for record in frame.to_dict("records")
    ]


# The rows and columns


def test_every_fixture_revision_converts_back_to_its_record() -> None:
    frame = to_dataframe(fixture_revisions())
    assert frame.shape == (37, 14)
    assert frame_json(frame) == REVISIONS


@pytest.mark.parametrize("revisions", [fixture_revisions(), ()], ids=["37", "empty"])
def test_the_columns_are_the_fields_in_the_contracts_order(
    revisions: tuple[Revision, ...],
) -> None:
    assert list(to_dataframe(revisions).columns) == COLUMNS
    assert list(DTYPES) == COLUMNS


@pytest.mark.parametrize("revisions", [fixture_revisions(), ()], ids=["37", "empty"])
def test_each_column_has_its_dtype(revisions: tuple[Revision, ...]) -> None:
    frame = to_dataframe(revisions)
    assert {str(name): str(dtype) for name, dtype in frame.dtypes.items()} == DTYPES
    for name in ("published_at", "received_at", "available_at"):
        dtype = frame[name].dtype
        assert isinstance(dtype, pd.DatetimeTZDtype)
        assert dtype.tz is UTC


def test_rows_keep_the_order_given_with_nothing_dropped_or_combined() -> None:
    revisions = fixture_revisions()
    # Reversed, with the first revision three times. August 2026's two
    # revisions stay two rows.
    given = [*reversed(revisions), revisions[0], revisions[0]]
    frame = to_dataframe(given)
    assert frame["revision_id"].tolist() == [r.revision_id for r in given]
    assert frame_json(frame) == [to_json(r) for r in given]
    assert frame["observation_id"].tolist().count("obs_aug26") == 2
    assert isinstance(frame.index, pd.RangeIndex)
    assert frame.index.equals(pd.RangeIndex(len(given)))


@pytest.mark.parametrize(
    "revisions",
    [[], (), iter(()), generate(())],
    ids=["list", "tuple", "iterator", "generator"],
)
def test_an_empty_input_gives_an_empty_frame_with_the_14_columns(
    revisions: Any,
) -> None:
    frame = to_dataframe(revisions)
    assert frame.shape == (0, 14)
    assert list(frame.columns) == COLUMNS
    assert isinstance(frame.index, pd.RangeIndex)


# Values


def test_value_holds_decimals_and_none_never_floats_or_nan() -> None:
    frame = to_dataframe(fixture_revisions())
    values = frame["value"].tolist()
    assert all(type(v) is Decimal or v is None for v in values)
    assert values == [r.value for r in fixture_revisions()]
    assert values.count(None) == 2
    assert not any(isinstance(v, float) for v in values)
    assert not any(isinstance(v, Decimal) and v.is_nan() for v in values)


def test_a_missing_value_and_a_withdrawal_keep_their_nulls() -> None:
    frame = to_dataframe(fixture_revisions())
    missing = row(frame, "rev_oct24_1")
    assert missing["value"] is None
    assert missing["missing_reason"] == "not_collected"
    withdrawal = row(frame, "rev_may25_2")
    assert withdrawal["value"] is None
    assert withdrawal["missing_reason"] is None
    assert withdrawal["change_type"] == "withdrawal"


@pytest.mark.parametrize(
    "text", ["102.0", "0.0000001", "-0", "12345678901234567890.123", "100", "-3.50"]
)
def test_values_keep_their_text(text: str) -> None:
    revision = dataclasses.replace(fixture_revisions()[0], value=Decimal(text))
    value = to_dataframe([revision])["value"].tolist()[0]
    assert type(value) is Decimal
    assert format(value, "f") == text


def test_change_type_and_missing_reason_hold_plain_strings() -> None:
    revisions = fixture_revisions()
    # The decoder gives the enum members, which the frame must not keep.
    assert isinstance(revisions[0].change_type, ChangeType)
    oct24 = fixture_revision("rev_oct24_1")
    assert isinstance(oct24.missing_reason, MissingReason)
    frame = to_dataframe(revisions)
    assert {type(t) for t in frame["change_type"]} == {str}
    assert {type(r) for r in frame["missing_reason"]} == {str, type(None)}
    assert row(frame, "rev_oct24_1")["missing_reason"] == "not_collected"


def test_values_the_sdk_does_not_know_are_kept_as_strings() -> None:
    revision = dataclasses.replace(
        fixture_revisions()[0],
        change_type="reclassification",
        missing_reason="suppressed",
        value=None,
    )
    record = to_dataframe([revision]).to_dict("records")[0]
    assert (record["change_type"], record["missing_reason"]) == (
        "reclassification",
        "suppressed",
    )
    assert type(record["change_type"]) is str
    assert type(record["missing_reason"]) is str


def test_dates_are_naive_midnights_and_period_end_stays_exclusive() -> None:
    record = row(to_dataframe(fixture_revisions()), "rev_aug26_1")
    assert record["period_start"] == pd.Timestamp("2026-08-01")
    assert record["period_end"] == pd.Timestamp("2026-09-01")
    assert record["period_start"].tzinfo is None
    assert record["period_end"].tzinfo is None


def test_timestamps_are_the_same_instants_in_utc() -> None:
    record = row(to_dataframe(fixture_revisions()), "rev_aug26_1")
    revision = fixture_revision("rev_aug26_1")
    for name in ("published_at", "received_at", "available_at"):
        assert record[name].to_pydatetime() == getattr(revision, name)
        assert record[name].tzinfo is UTC
    assert record["available_at"] == pd.Timestamp("2026-09-03T12:31:10Z")


@pytest.mark.parametrize(
    "instant",
    [datetime(1, 1, 1, tzinfo=UTC), datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)],
    ids=["year-1", "year-9999"],
)
def test_every_year_a_record_can_hold_converts(instant: datetime) -> None:
    # Nanoseconds reach only 1677 to 2262.
    revision = dataclasses.replace(
        fixture_revisions()[0],
        period_start=instant.date(),
        period_end=instant.date(),
        published_at=instant,
        received_at=instant,
        available_at=instant,
    )
    record = frame_json(to_dataframe([revision]))[0]
    assert record["published_at"] == record["available_at"] == to_json(instant)
    assert record["period_start"] == record["period_end"] == to_json(instant.date())


# The input


def test_the_revisions_of_iterate_convert_in_one_pass() -> None:
    api = FakeAPI()
    with httpx.Client(transport=httpx.MockTransport(api)) as http:
        client = Client(
            api_key="demo-research-key",
            base_url="http://api.test",
            retry=RetryPolicy(max_attempts=1),
            http_client=http,
        )
        revisions = client.observations.iterate(
            "activity-index", available_as_of="2026-09-04T00:00:00Z", page_size=10
        )
        frame = to_dataframe(revisions)
    # The research example's frame: every observation at the September 4 cutoff.
    assert frame.shape == (32, 14)
    assert len(api.sent) == 4
    august = row(frame, "rev_aug26_1")
    assert format(august["value"], "f") == "102.4"


def test_a_generator_is_read_once_into_one_row_per_revision() -> None:
    frame = to_dataframe(generate(fixture_revisions()))
    assert frame_json(frame) == REVISIONS


@pytest.mark.parametrize(
    ("item", "name"),
    [
        (REVISIONS[0], "dict"),
        (None, "NoneType"),
        ("rev_jan24_1", "str"),
        # It has every attribute the conversion reads, so only the type check
        # refuses it.
        (lookalike(fixture_revisions()[0]), "SimpleNamespace"),
    ],
    ids=["dict", "none", "str", "lookalike"],
)
def test_an_item_that_is_not_a_revision_raises_type_error(
    item: object, name: str
) -> None:
    revisions: list[Any] = [fixture_revisions()[0], item]
    with pytest.raises(TypeError, match=rf"^item 1 is not a Revision: {name}$"):
        to_dataframe(revisions)


def test_pages_given_in_place_of_revisions_raise_type_error() -> None:
    page = decode_change_page(
        {"data": REVISIONS, "next_position": 37, "head_position": 37}
    )
    with pytest.raises(TypeError, match=r"^item 0 is not a Revision: ChangePage$"):
        to_dataframe([page])  # type: ignore[list-item]


# Importing


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    """Run `code` in a fresh interpreter of this environment."""
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_importing_financial_data_does_not_import_pandas() -> None:
    result = run_python(
        "import sys\n"
        "import financial_data\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] == 'pandas'))\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "[]\n"


def test_without_pandas_the_module_raises_import_error_naming_the_extra() -> None:
    # `None` in `sys.modules` makes `import pandas` fail, as it does when pandas
    # is not installed. A CI job checks the installed wheel without pandas too.
    result = run_python(
        "import sys\n"
        "sys.modules['pandas'] = None\n"
        "import financial_data\n"
        "try:\n"
        "    import financial_data.pandas\n"
        "except ImportError as error:\n"
        "    print(type(error).__name__, error.name, sep='\\n')\n"
        "    print(error)\n"
        "    print(type(error.__cause__).__name__)\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "ImportError",
        "pandas",
        "financial_data.pandas needs pandas 2.2 or later: "
        "install the extra financial-data-sdk[pandas]",
        "ModuleNotFoundError",
    ]
