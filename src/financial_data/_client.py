"""The client and its resources."""

from __future__ import annotations

import enum
from collections.abc import Iterator
from datetime import date, datetime
from types import TracebackType
from typing import Final

import httpx

from ._config import (
    DEFAULT_TIMEOUT,
    RetryPolicy,
    check_http_client,
    check_retry,
    check_timeout,
    resolve_api_key,
    resolve_base_url,
)
from ._records import ChangePage, Dataset, Meta, ObservationPage, Revision, Series


class _Omitted(enum.Enum):
    """The value of an argument the caller left out."""

    OMITTED = enum.auto()

    def __repr__(self) -> str:
        return "..."


_OMITTED: Final = _Omitted.OMITTED


class Client:
    """A client of the financial data API.

    The constructor checks its arguments and does no I/O. It raises
    `ConfigError` for an invalid value or a missing key.

    Args:
        api_key: The customer key. When `None`, it is read from
            `FINANCIAL_DATA_API_KEY`.
        base_url: The API's URL, with an optional path prefix. When `None`,
            it is read from `FINANCIAL_DATA_BASE_URL`, or else is
            `http://localhost:8080`.
        timeout: The deadline for one call, in seconds, covering every
            attempt and every wait, or `None` for none.
        retry: The retry policy; `None` means `RetryPolicy()`.
        http_client: An `httpx.Client` to send every request through. The
            SDK never closes it.
    """

    _api_key: str
    _base_url: str
    _timeout: float | None
    _retry: RetryPolicy
    _http_client: httpx.Client | None
    datasets: _Datasets
    series: _SeriesCatalog
    observations: _Observations
    changes: _Changes

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = DEFAULT_TIMEOUT,
        retry: RetryPolicy | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._api_key = resolve_api_key(api_key)
        self._base_url = resolve_base_url(base_url)
        self._timeout = check_timeout(timeout)
        self._retry = check_retry(retry)
        self._http_client = check_http_client(http_client)
        self._add_resources()

    def _add_resources(self) -> None:
        self.datasets = _Datasets(self)
        self.series = _SeriesCatalog(self)
        self.observations = _Observations(self)
        self.changes = _Changes(self)

    def with_options(
        self,
        *,
        timeout: float | _Omitted | None = _OMITTED,
        retry: RetryPolicy | _Omitted | None = _OMITTED,
    ) -> Client:
        """Return a client with the same key, base URL, and connection pool.

        The given settings are changed, and an omitted argument keeps its
        value. `timeout=None` removes the deadline, and `retry=None` means
        `RetryPolicy()`, as in the constructor.
        """
        new_timeout = self._timeout if timeout is _OMITTED else check_timeout(timeout)
        new_retry = self._retry if retry is _OMITTED else check_retry(retry)
        derived = Client.__new__(Client)
        derived._api_key = self._api_key
        derived._base_url = self._base_url
        derived._timeout = new_timeout
        derived._retry = new_retry
        derived._http_client = self._http_client
        derived._add_resources()
        return derived

    def __repr__(self) -> str:
        return f"Client(base_url={self._base_url!r})"

    def meta(self) -> Meta:
        """Describe the server: its API versions, contract version, and clock."""
        raise NotImplementedError

    def close(self) -> None:
        """Close the HTTP client the SDK created. A supplied one stays open."""
        raise NotImplementedError

    def __enter__(self) -> Client:
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError


class _Resource:
    def __init__(self, client: Client) -> None:
        self._client = client


class _Datasets(_Resource):
    """The dataset catalog."""

    def list(self) -> tuple[Dataset, ...]:
        """Return every dataset, in the API's order."""
        raise NotImplementedError

    def get(self, dataset_id: str) -> Dataset:
        """Return one dataset."""
        raise NotImplementedError


class _SeriesCatalog(_Resource):
    """The series catalog."""

    def list(self) -> tuple[Series, ...]:
        """Return every series, in the API's order."""
        raise NotImplementedError

    def get(self, series_id: str) -> Series:
        """Return one series."""
        raise NotImplementedError


class _Observations(_Resource):
    """Queries for the revision of each observation selected at a cutoff."""

    def page(
        self,
        series_id: str,
        *,
        period_start: date | str | None = None,
        period_end: date | str | None = None,
        available_as_of: datetime | str | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> ObservationPage:
        """Send one request and return its page."""
        raise NotImplementedError

    def pages(
        self,
        series_id: str,
        *,
        period_start: date | str | None = None,
        period_end: date | str | None = None,
        available_as_of: datetime | str | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> Iterator[ObservationPage]:
        """Return the query's pages, from the first or from `page_token`."""
        raise NotImplementedError

    def iterate(
        self,
        series_id: str,
        *,
        period_start: date | str | None = None,
        period_end: date | str | None = None,
        available_as_of: datetime | str | None = None,
        page_size: int | None = None,
    ) -> Iterator[Revision]:
        """Return the revisions of every page of the query."""
        raise NotImplementedError


class _Changes(_Resource):
    """A dataset's change stream."""

    def read(
        self, dataset_id: str, *, after: int, limit: int | None = None
    ) -> ChangePage:
        """Read once: the visible revisions after `after`, at most `limit`."""
        raise NotImplementedError

    def pages(
        self, dataset_id: str, *, after: int, limit: int | None = None
    ) -> Iterator[ChangePage]:
        """Return the stream's pages, from `after` until caught up."""
        raise NotImplementedError
