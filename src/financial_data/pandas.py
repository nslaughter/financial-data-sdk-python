"""The conversion of revisions to a pandas DataFrame (D6).

It needs the `pandas` extra, `financial-data-sdk[pandas]`. Importing
`financial_data` never imports this module, so the SDK works without pandas.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

try:
    import pandas as pd
except ImportError as error:
    raise ImportError(
        "financial_data.pandas needs pandas 2.2 or later: "
        "install the extra financial-data-sdk[pandas]",
        name="pandas",
    ) from error

from ._records import Revision

__all__ = ["to_dataframe"]

# Every dtype is given, because pandas infers them differently by version.
# Before 3.0 it infers nanoseconds from `datetime` values, and from 3.0
# microseconds, and `str` columns from strings. Neither infers `datetime64`
# from `date` values. Microseconds are `datetime`'s own resolution, and reach
# every year from 1 to 9999, where nanoseconds reach only 1677 to 2262.
_DATE: Final = "datetime64[us]"
_TIMESTAMP: Final = pd.DatetimeTZDtype(unit="us", tz="UTC")


def to_dataframe(revisions: Iterable[Revision]) -> pd.DataFrame:
    """Return a DataFrame with one row per revision, in the order given.

    The columns are the 14 fields of `Revision`, in the contract's order. No
    row is dropped, combined, or reordered, and no index is set:

    - `value` holds each `Decimal` or `None`, never a `float` or `NaN`;
    - the six text columns hold `str`, and `None` where `missing_reason` is
      null. `change_type` and `missing_reason` hold plain strings, not enum
      members;
    - `published_at`, `received_at`, and `available_at` are timezone-aware
      `datetime64` columns in UTC;
    - `period_start` and `period_end` are `datetime64` columns without a
      timezone, at midnight, and `period_end` stays exclusive;
    - `sequence` and `revision_number` are `int64`.

    An empty input gives an empty DataFrame with the same columns. The
    conversion never pivots, deduplicates, or selects a current revision.

    Raises `TypeError` if an item is not a `Revision`.
    """
    # Read once, so an iterator such as `client.observations.iterate()` works.
    rows = list(revisions)
    for index, revision in enumerate(rows):
        if not isinstance(revision, Revision):
            raise TypeError(
                f"item {index} is not a Revision: {type(revision).__name__}"
            )
    # The columns are in the contract's order, which is `Revision`'s.
    return pd.DataFrame(
        {
            "sequence": pd.Series([r.sequence for r in rows], dtype="int64"),
            "series_id": pd.Series([r.series_id for r in rows], dtype=object),
            "observation_id": pd.Series([r.observation_id for r in rows], dtype=object),
            "revision_id": pd.Series([r.revision_id for r in rows], dtype=object),
            "revision_number": pd.Series(
                [r.revision_number for r in rows], dtype="int64"
            ),
            "change_type": pd.Series(
                [_plain(r.change_type) for r in rows], dtype=object
            ),
            "period_start": pd.Series([r.period_start for r in rows], dtype=_DATE),
            "period_end": pd.Series([r.period_end for r in rows], dtype=_DATE),
            "value": pd.Series([r.value for r in rows], dtype=object),
            "missing_reason": pd.Series(
                [_plain(r.missing_reason) for r in rows], dtype=object
            ),
            "unit": pd.Series([r.unit for r in rows], dtype=object),
            "published_at": pd.Series([r.published_at for r in rows], dtype=_TIMESTAMP),
            "received_at": pd.Series([r.received_at for r in rows], dtype=_TIMESTAMP),
            "available_at": pd.Series([r.available_at for r in rows], dtype=_TIMESTAMP),
        }
    )


def _plain(text: str | None) -> str | None:
    """Return a `ChangeType` or `MissingReason` member as the `str` it equals."""
    return None if text is None else str.__str__(text)
