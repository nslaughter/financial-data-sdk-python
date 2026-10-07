"""The client, its resources, and the iterators over pages.

Every method checks its arguments before it returns, and `pages()` and
`iterate()` send nothing until the caller asks for the first item. Each
page's request is one call, with its own deadline, and is read in full
before any of it is returned, so no response stays open between items.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Generator, Iterator
from datetime import date, datetime
from types import TracebackType
from typing import Final

import httpx

from . import _params
from ._config import (
    DEFAULT_TIMEOUT,
    RetryPolicy,
    check_http_client,
    check_retry,
    check_timeout,
    resolve_api_key,
    resolve_base_url,
)
from ._decode import (
    decode_change_page,
    decode_dataset,
    decode_datasets,
    decode_meta,
    decode_observation_page,
    decode_series,
    decode_series_list,
)
from ._errors import redact
from ._params import Target
from ._records import ChangePage, Dataset, Meta, ObservationPage, Revision, Series
from ._transport import Pool, Transport


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

    Use it as a context manager, or call `close()`, to close the HTTP client
    the SDK creates. A call on a closed client raises `ClientClosedError`.

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
    _pool: Pool
    _transport: Transport
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
        self._pool = Pool(self._http_client)
        self._transport = Transport(
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=self._timeout,
            retry=self._retry,
            pool=self._pool,
        )
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

        The derived client does not own the pool: closing it closes nothing
        but itself, and closing this client closes both.
        """
        new_timeout = self._timeout if timeout is _OMITTED else check_timeout(timeout)
        new_retry = self._retry if retry is _OMITTED else check_retry(retry)
        derived = Client.__new__(Client)
        derived._api_key = self._api_key
        derived._base_url = self._base_url
        derived._timeout = new_timeout
        derived._retry = new_retry
        derived._http_client = self._http_client
        derived._pool = self._pool.derive()
        derived._transport = self._transport.derive(
            timeout=new_timeout, retry=new_retry, pool=derived._pool
        )
        derived._add_resources()
        return derived

    def __repr__(self) -> str:
        # The base URL can hold the key, as a gateway's path prefix might. The
        # key can also span the URL and the text around it, so the whole is
        # redacted too, as an exception's message is.
        base_url = redact(self._base_url, self._api_key)
        return redact(f"Client(base_url={base_url!r})", self._api_key)

    def meta(self) -> Meta:
        """Describe the server: its API versions, contract version, and clock."""
        return self._transport.call(_params.meta(), decode_meta)

    def close(self) -> None:
        """Close the HTTP client the SDK created. A supplied one stays open.

        A client from `with_options` closes nothing but itself. Closing twice
        does nothing.
        """
        self._pool.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class _Resource:
    def __init__(self, client: Client) -> None:
        self._client = client


class _Datasets(_Resource):
    """The dataset catalog."""

    def list(self) -> tuple[Dataset, ...]:
        """Return every dataset, in the API's order."""
        return self._client._transport.call(_params.datasets(), decode_datasets)

    def get(self, dataset_id: str) -> Dataset:
        """Return one dataset."""
        return self._client._transport.call(_params.dataset(dataset_id), decode_dataset)


class _SeriesCatalog(_Resource):
    """The series catalog."""

    def list(self) -> tuple[Series, ...]:
        """Return every series, in the API's order."""
        return self._client._transport.call(_params.series_list(), decode_series_list)

    def get(self, series_id: str) -> Series:
        """Return one series."""
        return self._client._transport.call(_params.series(series_id), decode_series)


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
        """Send one request, with exactly these arguments, and return its page."""
        target = _params.observations(
            series_id, period_start, period_end, available_as_of, page_size, page_token
        )
        return self._client._transport.call(target, decode_observation_page)

    def pages(
        self,
        series_id: str,
        *,
        period_start: date | str | None = None,
        period_end: date | str | None = None,
        available_as_of: datetime | str | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> Generator[ObservationPage, None, None]:
        """Return the query's pages, from the first or from `page_token`, to the last.

        Each later page is requested with the same arguments and the previous
        page's `next_page_token`. Every page must have the snapshot position
        of the first, and no page may return a token the iterator has already
        sent, `page_token` included; otherwise the iterator raises
        `UnexpectedResponseError`. An expired token raises
        `PageTokenExpiredError`, and the query is not restarted.
        """

        def target(token: str | None) -> Target:
            return _params.observations(
                series_id, period_start, period_end, available_as_of, page_size, token
            )

        first = target(page_token)
        return _observation_pages(self._client, first, target, page_token)

    def iterate(
        self,
        series_id: str,
        *,
        period_start: date | str | None = None,
        period_end: date | str | None = None,
        available_as_of: datetime | str | None = None,
        page_size: int | None = None,
    ) -> Generator[Revision, None, None]:
        """Return the revisions of every page of the query, in order."""
        pages = self.pages(
            series_id,
            period_start=period_start,
            period_end=period_end,
            available_as_of=available_as_of,
            page_size=page_size,
        )
        return _revisions(pages)


class _Changes(_Resource):
    """A dataset's change stream."""

    def read(
        self, dataset_id: str, *, after: int, limit: int | None = None
    ) -> ChangePage:
        """Read once: the visible revisions after `after`, at most `limit`."""
        return self._client._transport.call(
            _params.changes(dataset_id, after, limit), decode_change_page
        )

    def pages(
        self, dataset_id: str, *, after: int, limit: int | None = None
    ) -> Generator[ChangePage, None, None]:
        """Return the stream's pages, from `after` until caught up.

        Each later page is read from the previous page's `next_position`.
        The iterator stops after the first page that is caught up, so it
        always returns at least one. A page that is not caught up must hold
        a revision and move past the position it read from; otherwise the
        iterator raises `UnexpectedResponseError`.
        """
        first = _params.changes(dataset_id, after, limit)
        return _change_pages(self._client, first, dataset_id, after, limit)


def _observation_pages(
    client: Client,
    first: Target,
    target: Callable[[str], Target],
    page_token: str | None,
) -> Generator[ObservationPage, None, None]:
    """Fetch a query's pages one at a time, as the caller asks for them."""
    sent: set[str] = set() if page_token is None else {page_token}
    position: int | None = None

    def check(page: ObservationPage) -> str | None:
        if position is not None and page.position != position:
            return (
                f"the page's position, {page.position}, differs from the query's "
                f"snapshot position, {position}, so the pages would mix two states"
            )
        if page.next_page_token in sent:
            return (
                "the page's next_page_token is one this iterator has already "
                "sent, so the pages would repeat"
            )
        return None

    request = first
    while True:
        page = client._transport.call(request, decode_observation_page, check)
        position = page.position
        yield page
        if page.next_page_token is None:
            return
        sent.add(page.next_page_token)
        request = target(page.next_page_token)


def _revisions(
    pages: Iterator[ObservationPage],
) -> Generator[Revision, None, None]:
    """Return the records of each page, fetching a page only when it is needed."""
    for page in pages:
        yield from page.data


def _change_pages(
    client: Client,
    first: Target,
    dataset_id: str,
    after: object,
    limit: object,
) -> Generator[ChangePage, None, None]:
    """Read the change stream one page at a time, until a page is caught up."""
    # The position each page is read from. `after` is an int that is not a
    # bool, or None, which is not sent and which the API refuses.
    position = int.__int__(after) if isinstance(after, int) else None

    def check(page: ChangePage) -> str | None:
        if page.caught_up:
            return None
        if not page.data:
            return (
                "the change page is not caught up but holds no revision, so "
                "reading on would make no progress"
            )
        if position is not None and page.next_position <= position:
            return (
                f"the change page is not caught up but its next_position, "
                f"{page.next_position}, is not after the position it read from, "
                f"{position}, so reading on would make no progress"
            )
        return None

    request = first
    while True:
        page = client._transport.call(request, decode_change_page, check)
        yield page
        if page.caught_up:
            return
        position = page.next_position
        request = _params.changes(dataset_id, position, limit)
