"""A fake of the API's stage 1 endpoints, built from the vendored fixtures.

It answers as the API does at its default clock, `2026-10-01T00:00:00Z`,
when every fixture revision is visible and the head position is 37. It
selects revisions at a cutoff, pages a query at its snapshot, binds each
page token to the parameters it was issued with, and reads the change
stream, enough for tests of the SDK's methods and iterators over
`httpx.MockTransport`. It does not reproduce the API's validation or its
errors, except the few the tests need.

A test changes a response by request number, counted from 1 over every
request the fake receives: `rewrite` changes the JSON body the fake would
send, and `replace` sends another response instead.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

import httpx

CONTRACT: Final = Path(__file__).resolve().parents[2] / "contract"
HEAD_POSITION: Final = 37
SNAPSHOT_EXPIRES_AT: Final = "2026-10-01T01:00:00Z"
DEFAULT_PAGE_SIZE: Final = 100
META: Final = {
    "api_version": "v1",
    "supported_api_versions": ["v1"],
    "contract_version": "0.3.0",
    "server_time": "2026-10-01T00:00:00Z",
}

Document = dict[str, Any]


def fixture(name: str) -> Document:
    """Read a file of `contract/fixtures/`."""
    document: Document = json.loads((CONTRACT / "fixtures" / name).read_text())
    return document


REVISIONS: Final[list[Document]] = fixture("revisions.json")["revisions"]
DATASETS: Final[list[Document]] = [
    {**dataset, "entitled": True, "head_position": HEAD_POSITION}
    for dataset in fixture("datasets.json")["datasets"]
]
SERIES: Final[list[Document]] = [
    {**series, "entitled": True} for series in fixture("series.json")["series"]
]


def select(
    cutoff: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
) -> list[Document]:
    """Select each observation's revision at a cutoff, ordered by period.

    The selected revision is the one with the highest `revision_number`
    among those with `available_at` at or before the cutoff, or among all
    of them without one. Timestamps and dates compare as text, because the
    fixture writes them in one fixed form.
    """
    chosen: dict[str, Document] = {}
    for revision in REVISIONS:
        if cutoff is not None and revision["available_at"] > cutoff:
            continue
        current = chosen.get(revision["observation_id"])
        if current is None or revision["revision_number"] > current["revision_number"]:
            chosen[revision["observation_id"]] = revision
    return sorted(
        (
            revision
            for revision in chosen.values()
            if (period_start is None or revision["period_start"] >= period_start)
            and (period_end is None or revision["period_end"] <= period_end)
        ),
        key=lambda revision: revision["period_start"],
    )


def json_response(
    document: Document, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    media_type = "application/json" if status < 400 else "application/problem+json"
    return httpx.Response(
        status,
        headers={"Content-Type": media_type, **(headers or {})},
        content=json.dumps(document).encode(),
    )


def problem(status: int, code: str, parameter: str | None = None) -> Document:
    """Return a problem body, as the API writes one."""
    return {
        "status": status,
        "code": code,
        "title": code.replace("_", " ").capitalize(),
        "detail": f"The fake API answered {code}.",
        "parameter": parameter,
    }


def problem_response(
    status: int, code: str, parameter: str | None = None
) -> httpx.Response:
    return json_response(problem(status, code, parameter), status)


class FakeAPI:
    """The fake API: an `httpx.MockTransport` handler that keeps what it received."""

    def __init__(self) -> None:
        self.sent: list[httpx.Request] = []
        self.rewrites: dict[int, Callable[[Document], Document]] = {}
        self.replacements: dict[int, httpx.Response] = {}
        self.headers: dict[str, str] = {}
        """Headers added to every response, such as `Request-Id`."""
        self._tokens: dict[str, tuple[list[tuple[str, str]], int]] = {}

    def rewrite(self, number: int, change: Callable[[Document], Document]) -> None:
        """Change the JSON body of the response to request `number`."""
        self.rewrites[number] = change

    def replace(self, number: int, response: httpx.Response) -> None:
        """Send `response` to request `number` instead of the fake's own."""
        self.replacements[number] = response

    @property
    def queries(self) -> list[list[tuple[str, str]]]:
        """The query parameters of each request received, in order."""
        return [list(request.url.params.multi_items()) for request in self.sent]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        number = len(self.sent)
        if number in self.replacements:
            return self.replacements[number]
        status, document = self._answer(request)
        if number in self.rewrites:
            document = self.rewrites[number](document)
        return json_response(document, status, self.headers)

    def _answer(self, request: httpx.Request) -> tuple[int, Document]:
        path = request.url.raw_path.partition(b"?")[0].decode("ascii")
        params = list(request.url.params.multi_items())
        if path == "/v1/meta":
            return 200, META
        if path == "/v1/datasets":
            return 200, {"data": DATASETS}
        if path == "/v1/series":
            return 200, {"data": SERIES}
        if path == "/v1/observations":
            return self._observations(params)
        for dataset in DATASETS:
            if path == f"/v1/datasets/{quote(dataset['dataset_id'], safe='')}/changes":
                return self._changes(dict(params))
        for prefix, records, key in (
            ("/v1/datasets/", DATASETS, "dataset_id"),
            ("/v1/series/", SERIES, "series_id"),
        ):
            for record in records:
                if path == prefix + quote(record[key], safe=""):
                    return 200, record
        return 404, problem(404, "not_found")

    def _observations(self, params: list[tuple[str, str]]) -> tuple[int, Document]:
        query = [(name, value) for name, value in params if name != "page_token"]
        tokens = [value for name, value in params if name == "page_token"]
        offset = 0
        if tokens:
            if tokens[0] not in self._tokens:
                return 400, problem(400, "invalid_page_token", "page_token")
            bound, offset = self._tokens[tokens[0]]
            if bound != query:
                return 400, problem(400, "page_token_mismatch", "page_token")
        given = dict(query)
        size = int(given.get("page_size", DEFAULT_PAGE_SIZE))
        rows = select(
            given.get("available_as_of"),
            given.get("period_start"),
            given.get("period_end"),
        )
        end = offset + size
        token = None
        if end < len(rows):
            token = f"token-{len(self._tokens) + 1}"
            self._tokens[token] = (query, end)
        return 200, {
            "data": rows[offset:end],
            "position": HEAD_POSITION,
            "snapshot_expires_at": SNAPSHOT_EXPIRES_AT,
            "next_page_token": token,
        }

    def _changes(self, params: dict[str, str]) -> tuple[int, Document]:
        after = int(params["after"])
        if after > HEAD_POSITION:
            return 400, problem(400, "position_ahead", "after")
        limit = int(params.get("limit", DEFAULT_PAGE_SIZE))
        rows = sorted(
            (revision for revision in REVISIONS if revision["sequence"] > after),
            key=lambda revision: revision["sequence"],
        )[:limit]
        return 200, {
            "data": rows,
            "next_position": rows[-1]["sequence"] if rows else after,
            "head_position": HEAD_POSITION,
        }
