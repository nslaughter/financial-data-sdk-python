"""The client's methods, its iterators, and closing it.

Every test here uses `httpx.MockTransport`, with the fake API built from the
vendored fixtures in `tests/support/fake_api.py`. The tests of closing check
the `httpx.Client` the SDK creates through a subclass that replaces
`httpx.Client` and records its instances.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import islice
from typing import Any, TypeVar

import httpx
import pytest

from financial_data import (
    ChangePage,
    Client,
    ClientClosedError,
    Dataset,
    DeadlineExceededError,
    Meta,
    NotEntitledError,
    NotFoundError,
    ObservationPage,
    PageTokenError,
    PageTokenExpiredError,
    PositionAheadError,
    PositionExpiredError,
    RetryPolicy,
    Revision,
    Series,
    ServerError,
    UnexpectedResponseError,
)
from financial_data._transport import Transport
from tests.support.convert import to_json
from tests.support.fake_api import (
    DATASETS,
    HEAD_POSITION,
    REVISIONS,
    SERIES,
    SNAPSHOT_EXPIRES_AT,
    Document,
    FakeAPI,
    problem_response,
    select,
)

KEY = "demo-research-key"
BASE_URL = "http://api.test"
SERIES_ID = "activity-index"
DATASET_ID = "core-indicators"
NO_RETRY = RetryPolicy(max_attempts=1)


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def http(api: FakeAPI) -> Iterator[httpx.Client]:
    with httpx.Client(transport=httpx.MockTransport(api)) as http:
        yield http


@pytest.fixture
def client(http: httpx.Client) -> Client:
    return Client(api_key=KEY, base_url=BASE_URL, retry=NO_RETRY, http_client=http)


class Recording(httpx.Client):
    """The `httpx.Client` the SDK creates, sending to the fake API."""

    api: FakeAPI
    instances: list[Recording]

    def __init__(self, **settings: Any) -> None:
        self.settings = settings
        super().__init__(transport=httpx.MockTransport(self.api), **settings)
        self.instances.append(self)


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch, api: FakeAPI) -> list[Recording]:
    """Replace `httpx.Client`, and return every instance the SDK creates."""
    instances: list[Recording] = []
    monkeypatch.setattr(Recording, "api", api, raising=False)
    monkeypatch.setattr(Recording, "instances", instances, raising=False)
    monkeypatch.setattr(httpx, "Client", Recording)
    return instances


def own_client(**options: Any) -> Client:
    """A client that creates its own `httpx.Client`."""
    return Client(api_key=KEY, base_url=BASE_URL, retry=NO_RETRY, **options)


def sent(api: FakeAPI) -> list[str]:
    """The path and query of each request, exactly as sent."""
    return [request.url.raw_path.decode("ascii") for request in api.sent]


def tokens_sent(api: FakeAPI) -> list[str | None]:
    return [request.url.params.get("page_token") for request in api.sent]


def records(pages: list[ObservationPage] | list[ChangePage]) -> list[Any]:
    return [to_json(revision) for page in pages for revision in page.data]


_T = TypeVar("_T")


def drain(iterator: Iterator[_T], most: int = 100) -> list[_T]:
    """Return every item, failing rather than hanging if there are more than `most`.

    A regression can make an iterator endless, which `list()` would wait on
    forever.
    """
    items = list(islice(iterator, most + 1))
    assert len(items) <= most, f"the iterator returned more than {most} items"
    return items


# The catalog


def test_meta_describes_the_server(client: Client, api: FakeAPI) -> None:
    assert client.meta() == Meta(
        api_version="v1",
        supported_api_versions=("v1",),
        contract_version="0.3.0",
        server_time=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert sent(api) == ["/v1/meta"]


def test_datasets_list_returns_every_dataset_in_the_apis_order(
    client: Client, api: FakeAPI
) -> None:
    other = {**DATASETS[0], "dataset_id": "a-dataset", "entitled": False}
    api.rewrite(
        1, lambda body: {"data": [DATASETS[0], {**other, "head_position": None}]}
    )
    datasets = client.datasets.list()
    assert isinstance(datasets, tuple)
    assert all(isinstance(dataset, Dataset) for dataset in datasets)
    assert [dataset.dataset_id for dataset in datasets] == [DATASET_ID, "a-dataset"]
    assert datasets[1].head_position is None
    assert sent(api) == ["/v1/datasets"]


def test_datasets_get_returns_one_dataset(client: Client, api: FakeAPI) -> None:
    dataset = client.datasets.get(DATASET_ID)
    assert isinstance(dataset, Dataset)
    assert to_json(dataset) == DATASETS[0]
    assert sent(api) == [f"/v1/datasets/{DATASET_ID}"]


def test_series_list_returns_every_series_in_the_apis_order(
    client: Client, api: FakeAPI
) -> None:
    other = {**SERIES[0], "series_id": "a-series"}
    api.rewrite(1, lambda body: {"data": [SERIES[0], other]})
    series = client.series.list()
    assert isinstance(series, tuple)
    assert all(isinstance(each, Series) for each in series)
    assert to_json(series) == {"data": [SERIES[0], other]}
    assert sent(api) == ["/v1/series"]


def test_series_get_returns_one_series(client: Client, api: FakeAPI) -> None:
    series = client.series.get(SERIES_ID)
    assert isinstance(series, Series)
    assert to_json(series) == SERIES[0]
    assert sent(api) == [f"/v1/series/{SERIES_ID}"]


def test_an_unknown_id_raises_the_apis_not_found(client: Client, api: FakeAPI) -> None:
    with pytest.raises(NotFoundError) as caught:
        client.series.get("no-such-series")
    assert caught.value.code == "not_found"
    assert caught.value.path == "/v1/series/no-such-series"


def test_path_ids_are_percent_encoded_with_no_safe_characters(
    client: Client, api: FakeAPI
) -> None:
    for call in (
        lambda: client.series.get("a/b c"),
        lambda: client.datasets.get("a/b c"),
        lambda: client.changes.read("a/b c", after=0),
    ):
        with pytest.raises(NotFoundError):
            call()
    assert sent(api) == [
        "/v1/series/a%2Fb%20c",
        "/v1/datasets/a%2Fb%20c",
        "/v1/datasets/a%2Fb%20c/changes?after=0",
    ]


# Arguments


def path_calls(client: Client) -> list[Callable[[str], object]]:
    return [
        client.series.get,
        client.datasets.get,
        lambda dataset_id: client.changes.read(dataset_id, after=0),
        lambda dataset_id: client.changes.pages(dataset_id, after=0),
    ]


@pytest.mark.parametrize("segment", ["", ".", ".."])
@pytest.mark.parametrize("index", range(4))
def test_a_path_id_that_would_address_another_resource_is_refused(
    client: Client, api: FakeAPI, segment: str, index: int
) -> None:
    # pages() checks its arguments when it is called, before it returns.
    with pytest.raises(ValueError, match="must not be empty"):
        path_calls(client)[index](segment)
    assert api.sent == []


def test_a_query_series_id_is_sent_as_given(client: Client, api: FakeAPI) -> None:
    client.observations.page("")
    client.observations.page("..")
    assert sent(api) == ["/v1/observations?series_id=", "/v1/observations?series_id=.."]


def invalid_calls(client: Client) -> list[Callable[[], object]]:
    naive = datetime(2026, 9, 4)
    return [
        lambda: client.series.get(1),  # type: ignore[arg-type]
        lambda: client.observations.page(SERIES_ID, page_size="10"),  # type: ignore[arg-type]
        lambda: client.observations.pages(SERIES_ID, period_start=naive),
        lambda: client.observations.pages(SERIES_ID, available_as_of=naive),
        lambda: client.observations.iterate(SERIES_ID, page_size=True),
        lambda: client.observations.iterate(b"activity-index"),  # type: ignore[arg-type]
        lambda: client.changes.read(DATASET_ID, after="0"),  # type: ignore[arg-type]
        lambda: client.changes.pages(DATASET_ID, after=0, limit=1.0),  # type: ignore[arg-type]
    ]


@pytest.mark.parametrize("index", range(8))
def test_arguments_are_checked_when_the_method_is_called(
    client: Client, api: FakeAPI, index: int
) -> None:
    with pytest.raises((TypeError, ValueError)):
        invalid_calls(client)[index]()
    assert api.sent == []


def test_a_keyword_argument_a_method_does_not_define_is_refused(
    client: Client, api: FakeAPI
) -> None:
    with pytest.raises(TypeError):
        client.observations.iterate(SERIES_ID, page_token="token-1")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        client.observations.page(SERIES_ID, available_as_off="2026-09-04T00:00:00Z")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        client.changes.pages(DATASET_ID, after=0, page_size=10)  # type: ignore[call-arg]
    assert api.sent == []


# Observation queries


def test_page_sends_only_the_arguments_it_is_given(
    client: Client, api: FakeAPI
) -> None:
    page = client.observations.page(SERIES_ID)
    assert sent(api) == [f"/v1/observations?series_id={SERIES_ID}"]
    assert records([page]) == select()
    assert page.position == HEAD_POSITION
    assert page.next_page_token is None


def test_page_sends_every_argument_it_is_given(client: Client, api: FakeAPI) -> None:
    page = client.observations.page(
        SERIES_ID,
        period_start=date(2026, 8, 1),
        period_end="2026-09-01",
        available_as_of=datetime(
            2026, 9, 4, 2, 0, 0, 999_999, tzinfo=timezone(timedelta(hours=2))
        ),
        page_size=10,
    )
    assert sent(api) == [
        f"/v1/observations?series_id={SERIES_ID}&period_start=2026-08-01"
        "&period_end=2026-09-01&available_as_of=2026-09-04T00%3A00%3A00Z&page_size=10"
    ]
    assert to_json(page) == {
        "data": select("2026-09-04T00:00:00Z", "2026-08-01", "2026-09-01"),
        "position": HEAD_POSITION,
        "snapshot_expires_at": SNAPSHOT_EXPIRES_AT,
        "next_page_token": None,
    }
    [revision] = page.data
    assert (revision.revision_id, revision.value) == ("rev_aug26_1", Decimal("102.4"))


def test_page_sends_the_page_token_it_is_given(client: Client, api: FakeAPI) -> None:
    first = client.observations.page(SERIES_ID, page_size=10)
    assert first.next_page_token is not None
    second = client.observations.page(
        SERIES_ID, page_size=10, page_token=first.next_page_token
    )
    assert tokens_sent(api) == [None, first.next_page_token]
    assert records([first, second]) == select()[:20]


def test_the_contracts_example_prints_the_august_release(client: Client) -> None:
    revisions = client.observations.iterate(
        series_id=SERIES_ID,
        period_start="2026-08-01",
        period_end="2026-09-01",
        available_as_of="2026-09-04T00:00:00Z",
    )
    shown = [(r.period_start, r.value, r.revision_id) for r in revisions]
    assert shown == [(date(2026, 8, 1), Decimal("102.4"), "rev_aug26_1")]


def test_pages_follows_each_token_until_the_last_page(
    client: Client, api: FakeAPI
) -> None:
    pages = drain(client.observations.pages(SERIES_ID, page_size=10))
    assert [len(page.data) for page in pages] == [10, 10, 10, 2]
    assert {page.position for page in pages} == {HEAD_POSITION}
    assert pages[-1].next_page_token is None
    assert records(pages) == select()
    assert tokens_sent(api) == [None] + [page.next_page_token for page in pages[:-1]]
    for query in api.queries:
        assert query[:2] == [("series_id", SERIES_ID), ("page_size", "10")]


def test_pages_of_a_one_page_result_sends_one_request(
    client: Client, api: FakeAPI
) -> None:
    pages = drain(client.observations.pages(SERIES_ID))
    assert len(pages) == 1
    assert records(pages) == select()
    assert sent(api) == [f"/v1/observations?series_id={SERIES_ID}"]


def test_iterate_returns_the_records_of_every_page_in_order(
    client: Client, api: FakeAPI
) -> None:
    revisions = drain(client.observations.iterate(SERIES_ID, page_size=10))
    assert all(isinstance(revision, Revision) for revision in revisions)
    assert [to_json(revision) for revision in revisions] == select()
    assert len(api.sent) == 4


def test_iterators_send_nothing_until_the_first_item(
    client: Client, api: FakeAPI
) -> None:
    pages = client.observations.pages(SERIES_ID, page_size=10)
    revisions = client.observations.iterate(SERIES_ID, page_size=10)
    changes = client.changes.pages(DATASET_ID, after=0, limit=10)
    assert api.sent == []
    next(revisions)
    next(pages)
    next(changes)
    assert len(api.sent) == 3


def test_iterate_fetches_a_page_only_when_an_item_from_it_is_needed(
    client: Client, api: FakeAPI
) -> None:
    revisions = client.observations.iterate(SERIES_ID, page_size=10)
    assert len(api.sent) == 0
    assert len(list(islice(revisions, 10))) == 10
    assert len(api.sent) == 1
    next(revisions)
    assert len(api.sent) == 2
    assert len(list(islice(revisions, 9))) == 9
    assert len(api.sent) == 2
    next(revisions)
    assert len(api.sent) == 3


def test_pages_fetches_a_page_only_when_it_is_needed(
    client: Client, api: FakeAPI
) -> None:
    pages = client.observations.pages(SERIES_ID, page_size=10)
    for count in range(1, 5):
        next(pages)
        assert len(api.sent) == count
    with pytest.raises(StopIteration):
        next(pages)
    assert len(api.sent) == 4


def test_an_iterator_stopped_early_sends_nothing_more(
    client: Client, api: FakeAPI
) -> None:
    revisions = client.observations.iterate(SERIES_ID, page_size=10)
    for index, _ in enumerate(revisions):
        if index == 10:
            break
    assert len(api.sent) == 2
    revisions.close()
    with pytest.raises(StopIteration):
        next(revisions)
    pages = client.observations.pages(SERIES_ID, page_size=10)
    next(pages)
    pages.close()
    assert len(api.sent) == 3


def test_resuming_with_a_page_token_repeats_every_argument(
    client: Client, api: FakeAPI
) -> None:
    arguments: dict[str, Any] = {
        "period_start": date(2024, 3, 1),
        "period_end": "2026-09-01",
        "available_as_of": datetime(2026, 9, 4, tzinfo=UTC),
        "page_size": 7,
    }
    first = client.observations.page(SERIES_ID, **arguments)
    resumed = drain(
        client.observations.pages(
            SERIES_ID, **arguments, page_token=first.next_page_token
        )
    )
    [query, *later] = api.queries
    assert query == [
        ("series_id", SERIES_ID),
        ("period_start", "2024-03-01"),
        ("period_end", "2026-09-01"),
        ("available_as_of", "2026-09-04T00:00:00Z"),
        ("page_size", "7"),
    ]
    assert len(later) == len(resumed) > 1
    for each in later:
        assert each[:-1] == query
    assert tokens_sent(api)[1:] == [first.next_page_token] + [
        page.next_page_token for page in resumed[:-1]
    ]
    # Nothing missing and nothing repeated.
    assert records([first, *resumed]) == select(
        "2026-09-04T00:00:00Z", "2024-03-01", "2026-09-01"
    )


def test_resuming_with_other_arguments_raises_the_apis_refusal(
    client: Client, api: FakeAPI
) -> None:
    first = client.observations.page(SERIES_ID, page_size=10)
    pages = client.observations.pages(SERIES_ID, page_token=first.next_page_token)
    with pytest.raises(PageTokenError) as caught:
        next(pages)
    assert caught.value.code == "page_token_mismatch"
    assert len(api.sent) == 2


def test_an_expired_page_token_raises_and_the_query_is_not_restarted(
    client: Client, api: FakeAPI
) -> None:
    api.replace(1, problem_response(410, "page_token_expired", "page_token"))
    pages = client.observations.pages(SERIES_ID, page_size=10, page_token="token-1")
    with pytest.raises(PageTokenExpiredError) as caught:
        next(pages)
    assert (caught.value.status, caught.value.parameter) == (410, "page_token")
    with pytest.raises(StopIteration):
        next(pages)
    assert len(api.sent) == 1


def test_an_empty_result_raises_nothing(client: Client, api: FakeAPI) -> None:
    period: dict[str, Any] = {"period_start": "2030-01-01", "period_end": "2030-02-01"}
    page = client.observations.page(SERIES_ID, **period)
    assert (page.data, page.next_page_token) == ((), None)
    assert drain(client.observations.iterate(SERIES_ID, **period)) == []
    [only] = drain(client.observations.pages(SERIES_ID, **period))
    assert only.data == ()
    assert len(api.sent) == 3


def test_an_error_while_fetching_a_page_ends_the_iterator(
    client: Client, api: FakeAPI
) -> None:
    api.replace(2, problem_response(403, "not_entitled"))
    revisions = client.observations.iterate(SERIES_ID, page_size=10)
    taken = list(islice(revisions, 10))
    with pytest.raises(NotEntitledError) as caught:
        next(revisions)
    assert caught.value.attempts == 1
    assert "page_token=" in caught.value.path
    with pytest.raises(StopIteration):
        next(revisions)
    # The records already returned stay valid.
    assert [to_json(revision) for revision in taken] == select()[:10]
    assert len(api.sent) == 2


def test_a_page_is_retried_within_its_own_call(
    api: FakeAPI, http: httpx.Client
) -> None:
    retry = RetryPolicy(max_attempts=2, base_delay=0.0, max_delay=0.0)
    client = Client(api_key=KEY, base_url=BASE_URL, retry=retry, http_client=http)
    api.replace(2, problem_response(503, "unavailable"))
    pages = drain(client.observations.pages(SERIES_ID, page_size=10))
    assert records(pages) == select()
    assert tokens_sent(api)[1] == tokens_sent(api)[2] == pages[0].next_page_token
    assert len(api.sent) == 5


# Pagination guarantees


def query_path(token: str | None) -> str:
    return f"/v1/observations?series_id={SERIES_ID}&page_size=10&page_token={token}"


def assert_broken_guarantee(
    error: UnexpectedResponseError, path: str, request_id: str | None = None
) -> None:
    assert error.status is None
    assert (error.method, error.path, error.attempts) == ("GET", path, 1)
    assert error.request_id == request_id
    assert error.__cause__ is None
    assert error.__context__ is None


def test_a_page_at_another_snapshot_position_raises(
    client: Client, api: FakeAPI
) -> None:
    api.headers["Request-Id"] = "req_page"
    api.rewrite(2, lambda body: {**body, "position": HEAD_POSITION + 1})
    pages = client.observations.pages(SERIES_ID, page_size=10)
    first = next(pages)
    with pytest.raises(UnexpectedResponseError, match="position") as caught:
        next(pages)
    assert_broken_guarantee(caught.value, query_path(first.next_page_token), "req_page")
    assert str(caught.value) == (
        "the page's position, 38, differs from the query's snapshot position, 37, "
        "so the pages would mix two states "
        f"(GET {query_path(first.next_page_token)}; request_id req_page)"
    )
    with pytest.raises(StopIteration):
        next(pages)
    assert len(api.sent) == 2


def sent_token(api: FakeAPI, number: int) -> Callable[[Document], Document]:
    """Set `next_page_token` to the token request `number` sent."""

    def change(body: Document) -> Document:
        return {
            **body,
            "next_page_token": api.sent[number - 1].url.params["page_token"],
        }

    return change


def test_a_page_that_returns_the_token_its_request_sent_raises(
    client: Client, api: FakeAPI
) -> None:
    api.rewrite(2, sent_token(api, 2))
    pages = client.observations.pages(SERIES_ID, page_size=10)
    first = next(pages)
    with pytest.raises(UnexpectedResponseError, match="already sent") as caught:
        next(pages)
    assert_broken_guarantee(caught.value, query_path(first.next_page_token))
    assert len(api.sent) == 2


def test_a_page_that_leads_back_to_an_earlier_page_raises(
    client: Client, api: FakeAPI
) -> None:
    api.rewrite(3, sent_token(api, 2))
    pages = client.observations.pages(SERIES_ID, page_size=10)
    taken = [next(pages), next(pages)]
    with pytest.raises(UnexpectedResponseError, match="already sent") as caught:
        next(pages)
    assert_broken_guarantee(caught.value, query_path(taken[1].next_page_token))
    assert len(api.sent) == 3


class Unhashable(str):
    """A str that a set cannot hold."""

    __hash__ = None  # type: ignore[assignment]


class OwnHash(str):
    """A str that hashes unlike its text."""

    def __hash__(self) -> int:
        return 0


class NeverEqual(str):
    """A str equal to nothing, not even its text."""

    def __eq__(self, other: object) -> bool:
        return False

    __hash__ = str.__hash__


TOKEN_TYPES = [str, Unhashable, OwnHash, NeverEqual]


@pytest.mark.parametrize("token_type", TOKEN_TYPES)
def test_resuming_from_a_str_subclass_sends_its_text(
    client: Client, api: FakeAPI, token_type: type[str]
) -> None:
    first = client.observations.page(SERIES_ID, page_size=10)
    assert first.next_page_token is not None
    resumed = drain(
        client.observations.pages(
            SERIES_ID, page_size=10, page_token=token_type(first.next_page_token)
        )
    )
    assert tokens_sent(api)[1] == first.next_page_token
    assert records([first, *resumed]) == select()


@pytest.mark.parametrize("token_type", TOKEN_TYPES)
@pytest.mark.parametrize("number", [2, 3])
def test_a_page_that_returns_the_token_the_iterator_started_from_raises(
    client: Client, api: FakeAPI, number: int, token_type: type[str]
) -> None:
    first = client.observations.page(SERIES_ID, page_size=10)
    assert first.next_page_token is not None
    api.rewrite(number, lambda body: {**body, "next_page_token": first.next_page_token})
    pages = client.observations.pages(
        SERIES_ID, page_size=10, page_token=token_type(first.next_page_token)
    )
    returned = list(islice(pages, number - 2))
    with pytest.raises(UnexpectedResponseError, match="already sent"):
        next(pages)
    assert len(returned) == number - 2
    assert len(api.sent) == number


def test_a_broken_guarantee_is_not_retried(api: FakeAPI, http: httpx.Client) -> None:
    retry = RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0)
    client = Client(api_key=KEY, base_url=BASE_URL, retry=retry, http_client=http)
    api.rewrite(2, lambda body: {**body, "position": 1})
    pages = client.observations.pages(SERIES_ID, page_size=10)
    next(pages)
    with pytest.raises(UnexpectedResponseError) as caught:
        next(pages)
    assert caught.value.attempts == 1
    assert len(api.sent) == 2


def test_a_broken_guarantee_names_the_request_without_the_key(
    client: Client, api: FakeAPI
) -> None:
    api.headers["Request-Id"] = f"req-{KEY}"
    api.rewrite(2, lambda body: {**body, "position": 1})
    pages = client.observations.pages(f"series-{KEY}", page_size=10)
    next(pages)
    with pytest.raises(UnexpectedResponseError) as caught:
        next(pages)
    error = caught.value
    assert error.path.startswith("/v1/observations?series_id=[redacted]&page_size=10")
    assert error.request_id == "req-[redacted]"
    for text in (str(error), repr(error), repr(error.args)):
        assert KEY not in text


def test_a_page_that_keeps_the_guarantees_returns_after_a_rewrite(
    client: Client, api: FakeAPI
) -> None:
    # Members the SDK does not know are ignored, and so is a page's position
    # when it is the query's.
    api.rewrite(2, lambda body: {**body, "quality": "final"})
    assert (
        records(drain(client.observations.pages(SERIES_ID, page_size=10))) == select()
    )


# The change stream


def test_read_sends_exactly_the_arguments_it_is_given(
    client: Client, api: FakeAPI
) -> None:
    page = client.changes.read(DATASET_ID, after=1, limit=1)
    assert [revision.sequence for revision in page.data] == [2]
    assert (page.next_position, page.head_position, page.caught_up) == (2, 37, False)
    whole = client.changes.read(DATASET_ID, after=0)
    assert records([whole]) == REVISIONS
    assert whole.caught_up
    assert sent(api) == [
        f"/v1/datasets/{DATASET_ID}/changes?after=1&limit=1",
        f"/v1/datasets/{DATASET_ID}/changes?after=0",
    ]


def test_change_pages_read_from_each_next_position_until_caught_up(
    client: Client, api: FakeAPI
) -> None:
    pages = drain(client.changes.pages(DATASET_ID, after=16, limit=5))
    assert [page.caught_up for page in pages] == [False] * 4 + [True]
    assert [page.next_position for page in pages] == [21, 26, 31, 36, 37]
    assert records(pages) == REVISIONS[16:]
    assert sent(api) == [
        f"/v1/datasets/{DATASET_ID}/changes?after={after}&limit=5"
        for after in (16, 21, 26, 31, 36)
    ]


def test_change_pages_without_a_limit_send_none(client: Client, api: FakeAPI) -> None:
    [page] = drain(client.changes.pages(DATASET_ID, after=0))
    assert page.caught_up
    assert sent(api) == [f"/v1/datasets/{DATASET_ID}/changes?after=0"]


def test_change_pages_at_the_head_return_one_empty_caught_up_page(
    client: Client, api: FakeAPI
) -> None:
    [page] = drain(client.changes.pages(DATASET_ID, after=HEAD_POSITION))
    assert page.data == ()
    assert page.next_position == page.head_position == HEAD_POSITION
    assert page.caught_up
    assert len(api.sent) == 1


def test_change_pages_fetch_a_page_only_when_it_is_needed(
    client: Client, api: FakeAPI
) -> None:
    pages = client.changes.pages(DATASET_ID, after=0, limit=10)
    for count in range(1, 5):
        next(pages)
        assert len(api.sent) == count
    with pytest.raises(StopIteration):
        next(pages)
    assert len(api.sent) == 4


def changes_path(after: int, limit: int | None = None) -> str:
    query = f"after={after}" if limit is None else f"after={after}&limit={limit}"
    return f"/v1/datasets/{DATASET_ID}/changes?{query}"


def test_a_change_page_not_caught_up_without_a_revision_raises(
    client: Client, api: FakeAPI
) -> None:
    api.rewrite(1, lambda body: {**body, "data": [], "next_position": 0})
    pages = client.changes.pages(DATASET_ID, after=0)
    with pytest.raises(UnexpectedResponseError, match="holds no revision") as caught:
        next(pages)
    assert_broken_guarantee(caught.value, changes_path(0))
    assert len(api.sent) == 1


@pytest.mark.parametrize("next_position", [16, 15, 0])
def test_a_change_page_not_caught_up_that_does_not_move_past_its_position_raises(
    client: Client, api: FakeAPI, next_position: int
) -> None:
    api.rewrite(1, lambda body: {**body, "next_position": next_position})
    pages = client.changes.pages(DATASET_ID, after=16, limit=2)
    with pytest.raises(UnexpectedResponseError, match="not after") as caught:
        next(pages)
    assert_broken_guarantee(caught.value, changes_path(16, 2))
    assert len(api.sent) == 1


def test_a_later_change_page_that_does_not_move_past_its_position_raises(
    client: Client, api: FakeAPI
) -> None:
    api.rewrite(2, lambda body: {**body, "next_position": 18})
    pages = client.changes.pages(DATASET_ID, after=16, limit=2)
    assert next(pages).next_position == 18
    with pytest.raises(UnexpectedResponseError, match="not after") as caught:
        next(pages)
    assert_broken_guarantee(caught.value, changes_path(18, 2))
    assert len(api.sent) == 2


def test_a_caught_up_change_page_needs_no_revision_or_progress(
    client: Client, api: FakeAPI
) -> None:
    # As after a reset to an earlier clock: the head is behind the position.
    api.rewrite(1, lambda body: {"data": [], "next_position": 5, "head_position": 5})
    [page] = drain(client.changes.pages(DATASET_ID, after=20))
    assert (page.data, page.caught_up) == ((), True)
    assert len(api.sent) == 1


def test_change_position_errors_raise_on_the_first_page(
    client: Client, api: FakeAPI
) -> None:
    api.replace(1, problem_response(410, "position_expired", "after"))
    with pytest.raises(PositionExpiredError):
        next(client.changes.pages(DATASET_ID, after=0))
    with pytest.raises(PositionAheadError):
        next(client.changes.pages(DATASET_ID, after=HEAD_POSITION + 1))
    assert len(api.sent) == 2


# Deadlines


class Clock:
    """A monotonic clock that only the fake API and the caller advance."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def clocked_client(
    api: FakeAPI,
    clock: Clock,
    seconds: float = 0.0,
    *,
    timeout: float = 5.0,
    retry: RetryPolicy = NO_RETRY,
    client_timeout: float = 5.0,
) -> Client:
    """A client on the fake clock, whose every request takes `seconds` on it.

    `client_timeout` is the supplied HTTP client's own timeout.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        clock.now += seconds
        return api(request)

    http = httpx.Client(transport=httpx.MockTransport(handler), timeout=client_timeout)
    client = Client(
        api_key=KEY, base_url=BASE_URL, timeout=timeout, retry=retry, http_client=http
    )
    client._transport = Transport(
        api_key=KEY,
        base_url=BASE_URL,
        timeout=timeout,
        retry=retry,
        pool=client._pool,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    return client


def test_each_page_of_an_iterator_has_its_own_deadline(api: FakeAPI) -> None:
    clock = Clock()
    client = clocked_client(api, clock, 3.0)
    pages = client.observations.pages(SERIES_ID, page_size=10)
    for _ in range(4):
        next(pages)
        # The time between pages belongs to the caller.
        clock.now += 60.0
    assert [request.extensions["timeout"]["read"] for request in api.sent] == [5.0] * 4


def test_a_page_whose_request_passes_its_deadline_ends_the_iterator(
    api: FakeAPI,
) -> None:
    clock = Clock()
    client = clocked_client(api, clock, 3.0)
    changes = client.changes.pages(DATASET_ID, after=0, limit=10)
    next(changes)
    client._transport = client._transport.derive(
        timeout=2.0, retry=NO_RETRY, pool=client._pool
    )
    with pytest.raises(DeadlineExceededError) as caught:
        next(changes)
    assert caught.value.path == changes_path(10, 10)
    with pytest.raises(StopIteration):
        next(changes)


def test_a_derived_client_keeps_the_clocks_and_changes_the_deadline(
    api: FakeAPI,
) -> None:
    clock = Clock()
    client = clocked_client(api, clock, 3.0)
    assert client.meta().api_version == "v1"
    with pytest.raises(DeadlineExceededError):
        client.with_options(timeout=2.0).meta()
    assert client.with_options(timeout=None).meta().api_version == "v1"


# Deriving a client


def test_a_derived_client_sends_with_its_own_settings(api: FakeAPI) -> None:
    client = clocked_client(api, Clock(), timeout=60.0, client_timeout=7.0)
    retry = RetryPolicy(max_attempts=2, base_delay=0.0, max_delay=0.0)
    derived = client.with_options(timeout=None, retry=retry)
    client.meta()
    api.replace(2, problem_response(503, "unavailable"))
    derived.meta()
    api.replace(4, problem_response(503, "unavailable"))
    with pytest.raises(ServerError):
        client.meta()
    # Without a deadline, the HTTP client's own timeouts apply, and the
    # derived client's policy retries once.
    timeouts = [request.extensions["timeout"]["read"] for request in api.sent]
    assert timeouts == [60.0, 7.0, 7.0, 60.0]


def test_a_derived_client_shares_the_pool_without_owning_it(
    created: list[Recording], api: FakeAPI
) -> None:
    client = own_client()
    derived = client.with_options(timeout=2.0)
    again = derived.with_options(retry=None)
    assert created == []
    derived.meta()
    client.meta()
    again.series.list()
    assert len(created) == 1
    assert len(api.sent) == 3
    derived.close()
    again.close()
    assert not created[0].is_closed
    client.datasets.list()
    client.close()
    assert created[0].is_closed


def test_a_client_derived_many_times_over_checks_every_client_before_it(
    client: Client, api: FakeAPI
) -> None:
    # More derivations than the recursion limit allows nested calls.
    chain = [client]
    for _ in range(2 * sys.getrecursionlimit()):
        chain.append(chain[-1].with_options(timeout=5.0))
    assert chain[-1].meta().api_version == "v1"
    middle = len(chain) // 2
    chain[middle].close()
    with pytest.raises(ClientClosedError):
        chain[-1].meta()
    assert chain[middle - 1].meta().api_version == "v1"
    client.close()
    with pytest.raises(ClientClosedError):
        chain[1].meta()
    assert len(api.sent) == 2


def test_leaving_a_derived_clients_with_block_leaves_the_pool_open(
    created: list[Recording],
) -> None:
    with own_client() as client:
        with client.with_options(timeout=1.0) as derived:
            derived.meta()
        assert not created[0].is_closed
        client.meta()
        with pytest.raises(ClientClosedError):
            derived.meta()
    assert created[0].is_closed


# Closing


def test_the_sdk_creates_its_http_client_at_the_first_request(
    created: list[Recording], api: FakeAPI
) -> None:
    client = own_client()
    revisions = client.observations.iterate(SERIES_ID)
    client.with_options(timeout=None)
    assert created == []
    next(revisions)
    [http] = created
    # No timeouts of its own, because the deadline sets them, and no redirects.
    assert http.settings == {"timeout": None, "follow_redirects": False}
    client.close()


def test_close_closes_the_http_client_the_sdk_created(
    created: list[Recording], api: FakeAPI
) -> None:
    client = own_client()
    client.meta()
    client.close()
    assert created[0].is_closed
    client.close()
    with pytest.raises(ClientClosedError):
        client.meta()
    assert len(api.sent) == 1


def test_leaving_the_with_block_closes_the_http_client_the_sdk_created(
    created: list[Recording],
) -> None:
    with own_client() as client:
        client.meta()
        assert not created[0].is_closed
    assert created[0].is_closed


def test_leaving_the_with_block_by_an_exception_closes_the_http_client(
    created: list[Recording],
) -> None:
    raised = RuntimeError("the caller's")
    with pytest.raises(RuntimeError) as caught, own_client() as client:
        client.meta()
        raise raised
    assert caught.value is raised
    assert created[0].is_closed


def test_leaving_the_with_block_after_an_iterator_stopped_early_closes_it(
    created: list[Recording], api: FakeAPI
) -> None:
    with own_client() as client:
        revisions = client.observations.iterate(SERIES_ID, page_size=10)
        for _ in revisions:
            break
        assert not created[0].is_closed
    assert created[0].is_closed
    # The iterator kept past the block raises instead of sending.
    with pytest.raises(ClientClosedError):
        list(islice(revisions, 11))
    assert len(api.sent) == 1


def test_leaving_the_with_block_after_an_error_in_the_loop_closes_it(
    created: list[Recording],
) -> None:
    with pytest.raises(KeyError), own_client() as client:
        for _ in client.changes.pages(DATASET_ID, after=0, limit=1):
            raise KeyError("the caller's")
    assert created[0].is_closed


def test_closing_a_client_that_sent_nothing_creates_nothing(
    created: list[Recording], api: FakeAPI
) -> None:
    client = own_client()
    client.close()
    with pytest.raises(ClientClosedError):
        client.meta()
    assert created == []
    assert api.sent == []


def every_call(client: Client) -> list[Callable[[], object]]:
    return [
        client.meta,
        client.datasets.list,
        lambda: client.datasets.get(DATASET_ID),
        client.series.list,
        lambda: client.series.get(SERIES_ID),
        lambda: client.observations.page(SERIES_ID),
        lambda: next(client.observations.pages(SERIES_ID)),
        lambda: next(client.observations.iterate(SERIES_ID)),
        lambda: client.changes.read(DATASET_ID, after=0),
        lambda: next(client.changes.pages(DATASET_ID, after=0)),
    ]


@pytest.mark.parametrize("index", range(10))
@pytest.mark.parametrize("closing", ["client", "source", "derived", "http_client"])
def test_every_call_on_a_closed_client_raises(
    api: FakeAPI, http: httpx.Client, index: int, closing: str
) -> None:
    source = Client(api_key=KEY, base_url=BASE_URL, retry=NO_RETRY, http_client=http)
    client = source.with_options() if closing in ("source", "derived") else source
    if closing == "http_client":
        http.close()
    elif closing == "source":
        source.close()
    else:
        client.close()
    with pytest.raises(ClientClosedError):
        every_call(client)[index]()
    assert api.sent == []


def test_a_client_derived_from_a_closed_client_is_closed(
    client: Client, api: FakeAPI
) -> None:
    derived = client.with_options(timeout=1.0)
    derived.close()
    with pytest.raises(ClientClosedError):
        derived.with_options(timeout=2.0).meta()
    client.close()
    with pytest.raises(ClientClosedError):
        client.with_options().meta()
    assert api.sent == []


def test_an_iterator_raises_once_its_client_is_closed(
    client: Client, api: FakeAPI
) -> None:
    revisions = client.observations.iterate(SERIES_ID, page_size=10)
    list(islice(revisions, 10))
    client.close()
    with pytest.raises(ClientClosedError):
        next(revisions)
    assert len(api.sent) == 1


def test_an_iterator_raises_once_the_supplied_client_is_closed(
    client: Client, api: FakeAPI, http: httpx.Client
) -> None:
    pages = client.changes.pages(DATASET_ID, after=0, limit=10)
    next(pages)
    http.close()
    with pytest.raises(ClientClosedError, match="http_client"):
        next(pages)
    assert len(api.sent) == 1


def test_a_supplied_client_is_never_closed(api: FakeAPI, http: httpx.Client) -> None:
    with Client(api_key=KEY, base_url=BASE_URL, http_client=http) as client:
        client.meta()
        with client.with_options(timeout=None) as derived:
            derived.meta()
        for _ in client.observations.iterate(SERIES_ID, page_size=10):
            break
    assert not http.is_closed
    client.close()
    assert not http.is_closed
    assert http.get(f"{BASE_URL}/v1/meta").status_code == 200
