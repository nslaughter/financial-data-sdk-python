# Client contract: financial data SDK for Python

**Status:** Draft 0.1.0, for the operator's review; not tagged. Its eight
design decisions, D1 to D8, are [owner specifications](#owner-specifications):
the operator decided each on 2026-10-05, and an implementation follows them
as written. The other [decisions](#decisions) are proposed by this draft and
take effect when the operator approves it. None of the SDK exists yet.

This document governs the SDK's code: its public interface, the records it
returns, pagination and resuming, errors, retries and deadlines, credentials
and logging, the dataframe conversion, and the two examples that act as
release checks. The [README](../README.md) describes the project for people;
it is not a specification. The [conformance document](conformance.md) says
how the shared expected results run through the SDK and gives the scenarios
only an SDK can show, and the
[implementation plan](../docs/implementation-plan.md) orders the work. If
this document and a check or scenario disagree, report the disagreement
instead of choosing one.

## The API contract

The SDK is a customer of the demo API in
[financial-data-api](https://github.com/nslaughter/financial-data-api). It
is built against contract version 0.3.0, tagged `contract-v0.3.0` (commit
`ecd6f685b0acb69f5eba1a03041441497a039eed`):

| Document | Governs |
| --- | --- |
| [Data contract][data-contract] | What the records mean: identities, time fields, values and missing data, selection at a cutoff, and the change stream. |
| [API specification][api] | How the API behaves: parameters, errors and their order, authentication, the simulated clock, pagination, positions, and retention. |
| [Conformance format][conformance-format] | How the expected results are executed and compared. |

This repository keeps a byte-for-byte copy of the contract's fixtures and
expected results in `contract/`, pinned to the tag (D5). Where this document
restates an API rule, it cites the section. The API's documents govern the
API, and this one governs only what the client does with it. A disagreement
between them is reported, not resolved here.

The SDK implements stage 1 of the API, the demo API:

| API | SDK |
| --- | --- |
| `GET /v1/meta` | `client.meta()` |
| `GET /v1/datasets` | `client.datasets.list()` |
| `GET /v1/datasets/{dataset_id}` | `client.datasets.get(dataset_id)` |
| `GET /v1/series` | `client.series.list()` |
| `GET /v1/series/{series_id}` | `client.series.get(series_id)` |
| `GET /v1/observations` | `client.observations.page()`, `.pages()`, and `.iterate()` |
| `GET /v1/datasets/{dataset_id}/changes` | `client.changes.read()` and `.pages()` |
| `/test/*` | None ([decision 14](#decisions)) |

Stage 2 adds `published_as_of`, the revision history, the release calendar,
and exports. Each needs a later version of this document. Until then the SDK
has no method for them, and they do not change its records or errors.

D7 adds a `Request-Id` response header to the API in contract version
0.4.0, which is not yet written. The SDK reads the header from any response
that carries one, so it works unchanged against 0.3.0, where the value is
always `None`. This document assumes 0.4.0 names the header `Request-Id`; if
it names another, this document changes first.

## Scope

In scope:

- the stage 1 customer endpoints, called synchronously over HTTP;
- typed records that keep the contract's identities, time fields, decimal
  values, units, and missing values;
- pages, page tokens, and change-stream positions, alongside record
  iterators;
- errors that tell refusal, invalid input, throttling, transient failure,
  and an empty result apart;
- bounded retries under the caller's deadline;
- keeping the credential out of errors and logs, and logging through the
  standard library;
- a caller-supplied HTTP client (D1);
- conversion to a pandas DataFrame, as an optional extra (D6);
- a research example, and a scheduled-job example that checkpoints and
  resumes (D2).

Out of scope: stage 2 features; an asynchronous client; test control, which
the conformance runner and the scenarios call over HTTP themselves; local
storage, which belongs to the examples (D2); key issuance, rotation, or any
authentication other than the static bearer key; the stage 4 migration,
whose breaking change is still open ([Open questions](#open-questions));
performance claims.

## Concepts

The data contract and the API specification define these. The SDK uses
them unchanged.

- A **revision record** is one version of an observation: 14 fields, the
  same on every endpoint ([revision records][dc-records]). A `Revision`
  holds one.
- A **cutoff** (`available_as_of`) selects, for each observation, the
  highest-numbered revision available at that instant
  ([selection][dc-selection]). Without one, the API's clock is the cutoff.
- A **snapshot position** is the dataset's head position when a query's
  first page is served. Every page of the query is computed at it
  ([positions][api-positions]).
- A **page token** continues a query. It is opaque, bound to its endpoint,
  parameters, and credential, and it expires with its snapshot 3,600
  seconds of the API's time after the first page
  ([pagination][api-pagination]).
- A **position** in a dataset's change stream means every revision with
  `sequence` at or below it has been applied. Clients save the positions
  the API returns and never compute one ([change stream][dc-stream]).
- The **local copy** is the customer's own store of revisions. The SDK
  keeps none (D2); the scheduled-job example shows one.

## Public interface

All public names are importable from `financial_data` (D4), except the
dataframe conversion, which is in `financial_data.pandas` (D6).

```python
import os

from financial_data import Client

with Client(api_key=os.environ["FINANCIAL_DATA_API_KEY"]) as client:
    revisions = client.observations.iterate(
        series_id="activity-index",
        period_start="2026-08-01",
        period_end="2026-09-01",
        available_as_of="2026-09-04T00:00:00Z",
    )
    for revision in revisions:
        print(revision.period_start, revision.value, revision.revision_id)
```

At the API's default clock this prints `2026-08-01 102.4 rev_aug26_1`.

| Member | Behavior |
| --- | --- |
| `Client(*, api_key=None, base_url=None, timeout=60.0, retry=None, http_client=None)` | Validates its arguments ([Configuration](#configuration)) and does no I/O. Raises `ConfigError` for an invalid value or a missing key. |
| `with client`, `client.close()` | Leaving the block, by any path, or calling `close()` closes the HTTP client the SDK created. A client the caller supplied stays open (D1). Closing twice does nothing. |
| `client.with_options(*, timeout=..., retry=...)` | A client with the same key, base URL, and connection pool, and the given settings changed. An omitted argument keeps its value, and `timeout=None` removes the deadline. The derived client does not own the pool: closing it closes nothing, and closing the original closes both. |
| `client.meta()` | Returns `Meta`. |
| `client.datasets.list()` | Returns `tuple[Dataset, ...]`, in the API's order. |
| `client.datasets.get(dataset_id)` | Returns `Dataset`. |
| `client.series.list()` | Returns `tuple[Series, ...]`, in the API's order. |
| `client.series.get(series_id)` | Returns `Series`. |
| `client.observations.page(series_id, *, period_start=None, period_end=None, available_as_of=None, page_size=None, page_token=None)` | Sends one request and returns an `ObservationPage`. |
| `client.observations.pages(series_id, *, period_start=None, period_end=None, available_as_of=None, page_size=None, page_token=None)` | Returns an iterator of `ObservationPage`, from the first page, or from `page_token` if given, to the last. |
| `client.observations.iterate(series_id, *, period_start=None, period_end=None, available_as_of=None, page_size=None)` | Returns an iterator of `Revision` over every page. |
| `client.changes.read(dataset_id, *, after, limit=None)` | Sends one request and returns a `ChangePage`. |
| `client.changes.pages(dataset_id, *, after, limit=None)` | Returns an iterator of `ChangePage`, from `after` until caught up. |

Any call on a closed client, or on a client derived from a closed one,
raises `ClientClosedError`, as does a call through a caller-supplied HTTP
client that the caller has closed. `repr(client)` shows the base URL and
never the key.

### Arguments

Each argument becomes one path segment or query parameter. An argument left
`None` is not sent. The SDK adds no parameter the caller did not give, so
the API's defaults apply ([decision 5](#decisions)).

| Argument | Accepted | Sent as |
| --- | --- | --- |
| `series_id`, `dataset_id` | A non-empty `str` | In a path, percent-encoded with no safe characters, so `/` is encoded too. In a query, exactly as given. |
| `period_start`, `period_end` | A `datetime.date` that is not a `datetime`, or a `str` | `YYYY-MM-DD` for a `date`. A `str` exactly as given. |
| `available_as_of` | A timezone-aware `datetime`, or a `str` | For a `datetime`: converted to UTC, truncated to whole seconds, and written `YYYY-MM-DDTHH:MM:SSZ`. A `str` exactly as given. |
| `page_size`, `limit`, `after` | An `int` that is not a `bool` | Decimal digits, with `-` if negative. |
| `page_token` | A `str` | Exactly as given. |

- Before sending anything, the SDK checks only types and the two inputs it
  cannot send faithfully. A wrong type raises `TypeError`. A naive
  `datetime` raises `ValueError`, because its instant is unknown. An empty
  `series_id` or `dataset_id` raises `ValueError`, because it would address
  a different path.
- Everything else is the API's to judge, including a malformed date string,
  a page size out of range, a reversed period, and a cutoff after the API's
  clock. The SDK sends the request and raises the API's error
  ([decision 3](#decisions)). The API's rules then have one implementation,
  and the error names the parameter as the API does.
- Truncating a cutoff to whole seconds never changes the result. Every
  `available_at` is a whole second ([invariant 14][dc-invariants]), so
  `available_at ≤ T` holds exactly when `available_at ≤ ⌊T⌋`
  ([decision 4](#decisions)). A cutoff less than a second after the API's
  clock is therefore accepted, with the answer at the clock, which is the
  same.
- A keyword argument that a method does not define raises Python's own
  `TypeError`. A misspelled cutoff therefore never reaches the API, just as
  the API refuses an unknown parameter ([decision 19][dc-decisions]).

### Iterators

- `pages()` and `iterate()` send nothing until the caller asks for the
  first item. They fetch the next page only when the caller needs an item
  from it. Nothing is prefetched, and no request runs in the background.
- Each page is read in full before any of it is returned, so no response
  stays open between items. A loop that stops early, by `break` or by an
  exception, leaves nothing open except the connection pool, which leaving
  the `with` block closes.
- An error while fetching a page ends the iterator. Items already returned
  stay valid, and the error names the request that failed
  ([Errors](#errors)).
- One thread uses an iterator. A client may be shared between threads, as
  its `httpx.Client` may.

## Records

Every record is a frozen dataclass with slots (D1). Field names are the
API's JSON member names, in the contract's order, so a record converts to
the wire form by name.

### `Revision`

| Field | Type |
| --- | --- |
| `sequence` | `int` |
| `series_id` | `str` |
| `observation_id` | `str` |
| `revision_id` | `str` |
| `revision_number` | `int` |
| `change_type` | `str`; a `ChangeType` member when known |
| `period_start` | `date` |
| `period_end` | `date`; exclusive |
| `value` | `Decimal \| None` |
| `missing_reason` | `str \| None`; a `MissingReason` member when known |
| `unit` | `str` |
| `published_at` | `datetime`, UTC |
| `received_at` | `datetime`, UTC |
| `available_at` | `datetime`, UTC |

The fields mean what the [revision records][dc-records] table says. A query
returns the selected revision of each observation, and the change stream
returns every revision in `sequence` order. Both use `Revision`
([decision 2](#decisions)).

### Catalog records

| Record | Fields |
| --- | --- |
| `Meta` | `api_version: str`; `supported_api_versions: tuple[str, ...]`; `contract_version: str`; `server_time: datetime` |
| `Dataset` | `dataset_id`, `name`, `description: str`; `entitled: bool`; `head_position: int \| None`, which is `None` when the key is not entitled |
| `Series` | `series_id`, `dataset_id`, `name`, `description`, `frequency`, `unit`, `base_period`, `seasonal_adjustment`, `source`, `release_schedule: str`; `entitled: bool` |

### Pages

| Record | Fields |
| --- | --- |
| `ObservationPage` | `data: tuple[Revision, ...]`; `position: int`, the query's snapshot position; `snapshot_expires_at: datetime`; `next_page_token: str \| None` |
| `ChangePage` | `data: tuple[Revision, ...]`; `next_position: int`; `head_position: int`; and the property `caught_up`, which is `next_position == head_position` |

### Values and times

- `value` is a `Decimal` built from the API's string, never through
  `float`. A `Decimal` keeps the digits and the exponent, so
  `format(value, "f")` returns the API's text, `"102.0"` as `"102.0"`, for
  every value without leading zeros ([Open questions](#open-questions)).
  `str(value)` does not: it writes `0.0000001` as `1E-7`.
- A null value stays `None`, distinct from zero. `missing_reason` says why a
  released observation has no value. A withdrawal has `change_type`
  `withdrawal`, a null value, and a null reason ([values][dc-values]).
- Dates are `datetime.date`. `period_end` is exclusive, as in the contract.
- Timestamps are timezone-aware `datetime` values in UTC, with whole
  seconds.
- `change_type` and `missing_reason` are strings. A value the SDK knows is a
  member of `ChangeType` or `MissingReason`. Both are `StrEnum`s, so a
  member compares equal to its string. A value the SDK does not know is kept
  as a plain `str` ([decision 9](#decisions)); the contract requires clients
  to accept missing reasons they do not recognize ([values][dc-values]).

### Validating responses

The SDK checks every successful response against the API's documented form
before it returns anything from it:

- the `Content-Type` is `application/json`, ignoring parameters, and the
  body is a JSON object;
- every member the API documents for the response is present, with its JSON
  type. A nullable member may be `null`; no other member may
  ([responses][api-responses]);
- integers are JSON integers, not booleans or fractions. `value` is `null`
  or a JSON string matching `^-?[0-9]+(\.[0-9]+)?$`. Dates match
  `YYYY-MM-DD` and are valid dates. Timestamps match
  `YYYY-MM-DDTHH:MM:SSZ` and are valid instants;
- members the SDK does not know are ignored, as the API's versioning
  requires ([versioning][api-versioning]).

A response that fails raises `UnexpectedResponseError`, which names the
member and, for a record, its index in `data`. It is not retried. The SDK
does not check the contract's invariants across records, such as
precedence or order; the API enforces those ([invariants][dc-invariants]).
The only exceptions are the two guarantees pagination depends on
([Pagination](#pagination), [decision 10](#decisions)).

## Pagination

### Observation queries

- `page()` sends exactly the arguments it is given, including `page_token`,
  and returns the page.
- `pages()` sends its arguments for the first request. It then sends the
  same arguments with each `next_page_token` until a page's token is
  `null`. The API refuses a resumed page whose other parameters differ
  ([pagination][api-pagination]), and `pages()` never sends one.
- `iterate()` returns the records of `pages()`, in order.
- Every page of one query has the same snapshot `position`. If a later page
  reports a different position, or returns as its `next_page_token` any
  token the iterator has already sent, including the `page_token` it
  started from, `pages()` raises `UnexpectedResponseError`. The iterator
  keeps the tokens it has sent for this check, so it never mixes two states
  or loops forever, even through a cycle of several pages
  ([decision 10](#decisions)).

### Resuming a query

A scheduled job saves each page's records and its `next_page_token` in one
local transaction. To resume, it calls `pages()` with the same arguments and
`page_token` set to the saved token. The resumed iterator continues on the
original snapshot, so a revision that arrived in between does not appear,
and nothing is missed or repeated. The contract's
[pagination expected results][exp-pagination] check this.

- A snapshot lives for 3,600 seconds of the API's time from its first page.
  A token used after that raises `PageTokenExpiredError`. The SDK never
  restarts a query on its own: a restarted query has a new snapshot
  position, and only the caller knows whether the pages it saved can be
  combined with it ([decision 7](#decisions)).
- A token issued before a reset of the API raises `PageTokenError` with code
  `invalid_page_token`. A resumed request with different arguments, or with
  a different key, raises `PageTokenError` with code `page_token_mismatch`.
- A token never carries access. If a key is deactivated or an entitlement
  removed after the first page, the next page fails like any other request
  ([authentication][api-auth]).

### The change stream

- `read()` reads once: the visible revisions after `after`, at most
  `limit`.
- `pages()` reads from `after`, then from each page's `next_position`, and
  stops after the first page that is caught up. It always returns at least
  one page, so a caller already at the head still receives `next_position`
  and `head_position`.
- The caller saves each page's revisions with its `next_position` in one
  transaction. The SDK returns the positions the API sends and never
  computes one from a `sequence`, because sequences can have gaps
  ([change stream][dc-stream]).
- A page that is not caught up must hold at least one revision and have a
  `next_position` greater than the position it read from; otherwise
  `pages()` raises `UnexpectedResponseError`, so it cannot loop forever. A
  caught-up page needs neither: read at the head, it is empty and its
  `next_position` is the position it read from.
- `after` beyond the head raises `PositionAheadError`. A position whose next
  event is past retention raises `PositionExpiredError`; the caller loads
  the data again with a query and continues from that query's position
  ([retention][api-retention]).

## Errors

### Exceptions

| Exception | Raised when |
| --- | --- |
| `FinancialDataError` | Base class of every exception below. |
| `ConfigError(FinancialDataError, ValueError)` | An argument of `Client`, `with_options`, or `RetryPolicy` is invalid, or no key is given or set in the environment. |
| `ClientClosedError(FinancialDataError, RuntimeError)` | A call is made on a closed client, or through a closed HTTP client. |
| `APIError(FinancialDataError)` | The API, or something in front of it, answered with a `4xx` or `5xx` status. A subclass below says which; `APIError` itself is raised when no subclass applies. |
| `InvalidRequestError(APIError)` | `400` with `unknown_parameter`, `missing_parameter`, `invalid_parameter`, `conflicting_cutoffs`, or `cutoff_in_future`. `parameter` names the argument. |
| `PageTokenError(APIError)` | `400` with `invalid_page_token` or `page_token_mismatch`. |
| `PageTokenExpiredError(PageTokenError)` | `410 page_token_expired`. Restart the query. |
| `PositionAheadError(APIError)` | `400 position_ahead`. |
| `PositionExpiredError(APIError)` | `410 position_expired`. Load the data again with a query. |
| `AuthenticationError(APIError)` | `401`, with any code. |
| `NotEntitledError(APIError)` | `403 not_entitled`. |
| `NotFoundError(APIError)` | `404 not_found`. |
| `UnsupportedAPIVersionError(APIError)` | `404 unsupported_api_version`. |
| `RateLimitError(APIError)` | `429`, with any code, once retries stop. |
| `ServerError(APIError)` | Any `5xx`, with any code, once retries stop. |
| `TransportError(FinancialDataError)` | No response arrived because connecting failed or the connection broke, once retries stop. `__cause__` is httpx's exception. |
| `DeadlineExceededError(FinancialDataError, TimeoutError)` | The call's deadline passed while a request was in flight. |
| `UnexpectedResponseError(FinancialDataError)` | A response the API's documents do not allow: a successful response that fails [validation](#validating-responses), a `1xx` or `3xx` status, or a page that breaks a pagination guarantee. |

### Choosing the exception

The API's `code` is stable, and it is what clients act on
([errors][api-errors]). A response is a **problem response** when its
`Content-Type` is `application/problem+json` and its body is a JSON object.
The SDK chooses the exception by code first:

1. If the response is a problem response, and its `code` and status
   together match a row of the table above, raise that row's exception.
2. Otherwise choose by status: `401` raises `AuthenticationError`, `429`
   `RateLimitError`, any `5xx` `ServerError`, and any other `4xx`
   `APIError`.

A body the API did not write, such as an HTML page from a proxy, or a code
from a later API version, still raises an exception a caller can act on.
Neither raises one that claims more than the response says: a `404` without
a problem body is an `APIError`, not a `NotFoundError`, because it may come
from a wrong base URL rather than a missing series
([decision 8](#decisions)).

### Attributes

| Attribute | On | Meaning |
| --- | --- | --- |
| `status` | `APIError`, `UnexpectedResponseError` | The HTTP status, or `None` for an `UnexpectedResponseError` raised by a pagination check. |
| `code`, `title`, `detail`, `parameter` | `APIError` | The problem response's members, or `None` when the response is not one or lacks the member. `parameter` names the parameter at fault. |
| `problem` | `APIError` | The whole problem body as a read-only mapping, or `None`. |
| `retry_after` | `APIError` | The seconds the response's `Retry-After` asked for, or `None` ([Waiting](#waiting)). |
| `request_id` | `APIError`, `UnexpectedResponseError` | The response's `Request-Id` header, or `None` (D7). |
| `method`, `path` | `APIError`, `TransportError`, `DeadlineExceededError`, `UnexpectedResponseError` | The request's method, and its path with the query string, as sent. |
| `attempts` | `APIError`, `TransportError`, `DeadlineExceededError`, `UnexpectedResponseError` | How many requests the call sent, retries included. |

`str(error)` reads like
`403 not_entitled: <detail> (GET /v1/observations?series_id=activity-index; request_id req_1)`.
It never contains the key ([Credentials and logging](#credentials-and-logging)).

### What is not an error

- A query that matches nothing returns an empty page, and its iterator ends
  at once. A refusal is always an exception. A synchronization job can
  therefore tell "nothing new" from "not allowed"
  ([access control][exp-access]).
- A withdrawn observation, and a released observation without a value, are
  records, not errors.

## Retries and deadlines

### What is retried

Every stage 1 endpoint is a `GET`, which is safe to repeat. A call is
retried when an attempt ends with one of these outcomes
([decision 11](#decisions)):

| Outcome | Retried |
| --- | --- |
| `429` | Yes |
| `500`, `502`, `503`, `504` | Yes |
| Any other status | No |
| Connecting failed or timed out | Yes |
| The connection broke before the response was complete, or a timeout set on a caller-supplied client expired | Yes |
| The call's deadline passed | No |
| A successful response failed validation | No |

A later version that adds a request with effects, such as creating an
export, must say whether it is retried. Until it does, such a request is not
retried.

### Waiting

Before retry _k_ (_k_ = 1 before the second attempt), the SDK waits:

- the delay in the response's `Retry-After`, if it has a valid one. The
  value is either a non-negative integer of seconds or an HTTP date. A date
  is compared with the local clock in UTC, and a date in the past means no
  wait. The delay is used as given, without jitter and without the backoff
  cap. An invalid value is ignored;
- otherwise, `min(max_delay, base_delay × 2^(k−1))` seconds. With `jitter`,
  the wait is drawn uniformly between 0 and that value.

### Bounds

A call stops retrying, and raises the last attempt's exception, as soon as
one of these holds:

- it has made `max_attempts` attempts;
- the next wait would bring its total waiting above `max_retry_wait`;
- the next wait would end at or after its deadline.

A `Retry-After` longer than the remaining budget or deadline therefore
raises the exception of the attempt that carried it at once, with
`retry_after` set, instead of sleeping and then failing: `RateLimitError`
for a `429`, and `ServerError` for a `5xx` such as `503`. A caller with a
longer horizon, such as a scheduled job, can wait that long itself and try
again ([decision 12](#decisions)).

### Deadline

`timeout` is the deadline for one call, in seconds, covering every attempt
and every wait; `None` sets none. In `pages()` and `iterate()`, each page's
request is one call with its own deadline, because the time between pages
belongs to the caller ([decision 15](#decisions)). Each attempt runs with an
httpx timeout equal to the time remaining. When the deadline passes during
an attempt, the SDK raises `DeadlineExceededError` and does not retry.

httpx applies that timeout to each connect, write, and read separately, not
to the attempt as a whole, so a server that sends its headers or its body a
few bytes at a time can keep an attempt running past the deadline. How the
SDK bounds such an attempt is an [open question](#open-questions), and plan
step 3 waits for the answer.

## Requests

- A request's URL is `base_url`, without a trailing `/`, followed by the
  path. A `base_url` may include a path prefix, for an API behind a
  gateway.
- Every request carries `Authorization: Bearer <key>`,
  `Accept: application/json`, and
  `User-Agent: financial-data-sdk-python/<SDK version> python/<Python version>`.
- Every request is a `GET` without a body.
- The HTTP client the SDK creates does not follow redirects, so a `3xx`
  raises `UnexpectedResponseError`. A caller-supplied client follows its
  own settings.

## Credentials and logging

- The key comes from `api_key` or, when that is `None`, from
  `FINANCIAL_DATA_API_KEY`. A variable set to an empty value counts as
  unset. The key must be a non-empty string without whitespace or control
  characters, so it can only appear in the `Authorization` header;
  otherwise the constructor raises `ConfigError`, whose message does not
  repeat the value.
- The key appears only in that header. It is not in any exception's
  message, arguments, or attributes, in `repr(client)`, or in any log
  record. A `TransportError` or `DeadlineExceededError` chains httpx's
  exception as its `__cause__`, and that exception holds the request, so
  the SDK replaces the request's `Authorization` value with
  `Bearer [redacted]` before raising ([decision 13](#decisions)).
- Logging uses `logging.getLogger("financial_data")`. The SDK adds no
  handler, sets no level, and never calls `logging.basicConfig`. It logs
  each attempt at `DEBUG`, with the method, path, attempt number, status or
  exception class, elapsed seconds, and request ID. It logs each retry at
  `INFO`, with the same fields, the reason, and the wait. It logs nothing at
  `WARNING` or above, so a program that has not configured logging prints
  nothing.

## Supplying an HTTP client (D1)

`http_client` takes an `httpx.Client`. Given one, the SDK:

- sends every request through it, with absolute URLs, so its `base_url` is
  not used;
- sets its own `Authorization`, `Accept`, and `User-Agent` on each request.
  Other headers the client sends by default are sent too, and the SDK's
  credential replaces any `auth` the client has;
- passes a timeout on each attempt only when a deadline applies; otherwise
  the client's own timeouts apply;
- never closes it, because the caller owns it.

A customer uses this to add proxies, TLS settings, or event hooks, and
tests use it to substitute `httpx.MockTransport`. Without one, the SDK
creates its own client with no timeouts of its own (the deadline sets
them), no redirects, and the default connection pool.

## Dataframes (D6)

`financial_data.pandas.to_dataframe(revisions)` takes an iterable of
`Revision` and returns a `pandas.DataFrame`:

- one row per revision, in the order given. No row is dropped, combined, or
  reordered, and no index is set;
- the 14 fields as columns, with the same names, in the contract's order;
- `value` is an `object` column of `Decimal` or `None`, never `float` or
  `NaN`;
- `series_id`, `observation_id`, `revision_id`, `change_type`,
  `missing_reason`, and `unit` are `object` columns of `str`, with `None`
  kept where `missing_reason` is null. `change_type` and `missing_reason`
  hold plain strings, not enum members;
- `published_at`, `received_at`, and `available_at` are timezone-aware
  `datetime64` columns in UTC;
- `period_start` and `period_end` are `datetime64` columns without a
  timezone, at midnight. `period_end` stays exclusive;
- `sequence` and `revision_number` are `int64` columns;
- an empty input gives an empty DataFrame with the same 14 columns.

The conversion never pivots, deduplicates, or selects a current revision.
Any conversion that drops revision information is left to the customer's
analysis. Importing `financial_data.pandas` without pandas installed raises
`ImportError`, naming the extra `financial-data-sdk[pandas]`; importing
`financial_data` never imports pandas. The extra requires pandas 2.2 or
later.

## Examples (D2)

The examples are the customer's workflow. CI runs them from the installed
distribution against the pinned API ([conformance](conformance.md#examples)).
They keep the local copy; the SDK does not.

### Research example

`examples/research.py` reads `FINANCIAL_DATA_API_KEY` and
`FINANCIAL_DATA_BASE_URL`, then:

1. queries the August 2026 observation of `activity-index` at
   `available_as_of=2026-09-04T00:00:00Z` and without a cutoff, and prints
   each result's `revision_id`, `value`, `published_at`, and
   `available_at`;
2. converts every observation at the September 4 cutoff to a DataFrame,
   which needs the `pandas` extra, and prints its shape and its August row.

At the API's default clock, it prints 102.4 (`rev_aug26_1`) for September 4
and 102.1 (`rev_aug26_2`) without a cutoff.

### Scheduled-job example

`examples/scheduled_job.py` keeps a local copy of one series in SQLite,
using the standard library's `sqlite3`. It loads the copy with a paged
query, follows the change stream, and answers cutoff queries offline.

| Command | Effect |
| --- | --- |
| `sync --db PATH [--series SERIES_ID] [--page-size N]` | Loads, resumes, or updates the local copy, then exits. The series defaults to `activity-index` and the page size to 100. |
| `query --db PATH --as-of TIMESTAMP [--period-start DATE] [--period-end DATE]` | Answers a cutoff query from the local copy, without the API. |
| `status --db PATH` | Prints the checkpoint. |

#### Storage

- `revisions` holds one row per revision, keyed by `revision_id`, with all
  14 fields. `value` is stored as the API's text in a `TEXT` column, never
  as `REAL`. Rows are inserted with `INSERT OR IGNORE`, so a page received
  twice changes nothing.
- `checkpoint` holds one row: the series and its dataset, the query's
  arguments, the stage (`loading` or `following`), the saved page token,
  the position, `complete_from`, and `synced_at`.

#### `sync`

1. Reads `client.meta()` and keeps its `server_time`.
2. **Loading.** Without a saved page token, it starts the query, without a
   cutoff, from its first page; with one, it resumes from the token. For
   each page, one transaction saves the page's revisions, its
   `next_page_token`, the snapshot position, and the latest `available_at`
   among the revisions this load has saved. After the last page, the stage
   becomes `following`, at the query's position. A query without a cutoff
   matches the state at its position, so the stream continues from there
   ([positions][api-positions]).
   If the saved token has expired (`PageTokenExpiredError`), the load
   restarts from the first page. Rows already saved stay, because a
   revision never changes, but `complete_from` is computed again from the
   new load.
3. **Following.** Reads `client.changes.pages(dataset_id, after=position)`.
   For each page, one transaction saves its revisions of the series and its
   `next_position`. A `PositionExpiredError` returns the job to loading.
4. When caught up, saves the `server_time` from step 1 as `synced_at` and
   exits with status 0.
5. On any `FinancialDataError`, prints what this run saved (pages and
   records), the token or position the next run resumes from, and the
   error's class, code, and request ID, then exits with status 1. Running
   `sync` again resumes.

#### `query`

`query` applies the contract's [selection rule][dc-selection] to the local
copy. For each observation within the period range, it takes the revision
with the highest `revision_number` among those with `available_at` at or
before `--as-of`. It prints one line per observation, ordered by
`period_start`: the period start, the value as stored, the revision ID, and
the change type.

#### What the local copy can reproduce

A query returns each observation's selected revision at the snapshot, not
the revisions it superseded, so the local copy lacks those. It still
answers a cutoff exactly from `complete_from`, the latest `available_at`
among the records the load saved. From that instant, every loaded record is
available, and each outranks every revision the load left out of its
observation, while revisions after the snapshot position come from the
stream. It answers exactly up to `synced_at`, the API's time before the
last catch-up began. `query` refuses a cutoff outside that range, names the
range, and suggests asking the API.

In the fixture, a load that runs after the August 2026 release became
available, at 12:31:10 on September 3, and before its revision did, at
12:30:40 on September 10, has `complete_from` `2026-09-03T12:31:10Z`. Such a
copy still answers the September 4 cutoff with 102.4 after it has followed
the revision to 102.1. A copy first loaded after the revision has
`complete_from` on September 10, and the September 4 cutoff needs the API.

## Packaging (D4, D8)

- The distribution is `financial-data-sdk`, imported as `financial_data`.
  It supports CPython 3.11 and later, and CI tests every version from 3.11
  to the newest stable release. It uses a `src/` layout and ships
  `py.typed`.
- `httpx` is the only runtime dependency (D1). The `pandas` extra adds
  pandas (D6).
- `financial_data.__version__` is the distribution's version.
- Releases are tagged `v<version>` ([decision 18](#decisions)). Each is a
  GitHub release with the wheel and the source distribution attached, and
  nothing is published to PyPI (D8).
- The public API is every name in `financial_data.__all__`, and
  `financial_data.pandas.to_dataframe`. Modules and names beginning with `_`
  are private.

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `api_key` | `FINANCIAL_DATA_API_KEY` | The customer key ([Credentials and logging](#credentials-and-logging)). |
| `base_url` | `FINANCIAL_DATA_BASE_URL`, else `http://localhost:8080` ([decision 16](#decisions)) | An `http` or `https` URL with a host and an optional path, without a query or fragment. A variable set to an empty value counts as unset. |
| `timeout` | `60.0` ([decision 15](#decisions)) | The deadline for one call, in seconds, or `None` for none. |
| `retry` | `RetryPolicy()` | The retry policy, below. |
| `http_client` | `None` | A caller-supplied `httpx.Client` (D1). |

`RetryPolicy` is a frozen dataclass ([decision 12](#decisions)):

| Field | Default | Meaning |
| --- | --- | --- |
| `max_attempts` | `4` | Attempts per call, including the first. `1` turns retries off. |
| `max_retry_wait` | `30.0` | Total seconds a call may spend waiting between attempts. |
| `base_delay` | `0.5` | Seconds of backoff before the first retry. |
| `max_delay` | `8.0` | The backoff cap in seconds. It does not limit `Retry-After`. |
| `jitter` | `True` | Draw each backoff uniformly between 0 and its value. |

`timeout` must be positive and finite, or `None`. `max_attempts` must be an
integer of at least 1. The other durations must be finite and not negative,
with `base_delay` at most `max_delay`. Otherwise the constructor raises
`ConfigError`.

## Decisions

These are proposed by this draft and take effect when the operator approves
it. Each can be revisited in a later version.

1. **The client is synchronous only.** The README chooses this: the caller
   controls concurrency. An asynchronous client would double the surface
   the Go and TypeScript SDKs must match, and it can come in a later
   version.
2. **One record type, `Revision`, serves queries and the change stream.**
   The API returns the same 14-field record on every path
   ([responses][api-responses]), so a customer can store both in one table.
   The README's example names its loop variable `observation`; each item is
   the selected revision of one observation.
3. **Strings are sent as given, and the API validates them.** The SDK
   formats `date` and `datetime` values itself, so they are always well
   formed. A string passes through, so the API's rules have one
   implementation, the SDK cannot drift from them, and the error names the
   parameter as the API does. The cost is a request for an input the SDK
   could have refused locally.
4. **Cutoffs are truncated to whole seconds.** The truncation is exact,
   because every `available_at` is a whole second. Refusing fractional
   seconds would make `datetime.now(UTC)` unusable as a cutoff, for no
   gain.
5. **The SDK adds no parameter the caller did not give.** The API's
   defaults apply, and a resumed page repeats exactly what the first page
   sent.
6. **Iterators fetch lazily and hold one page.** The README requires it:
   the caller controls when requests happen and what stays open.
7. **An expired page token is raised, never answered by restarting.** A
   restart changes the snapshot position, and whether pages from two
   snapshots can be combined depends on the caller's storage. The
   scheduled-job example shows one safe way.
8. **An exception is chosen by code, then by status, and never claims more
   than the response says.** The code is what the API promises to keep
   stable. Falling back to the status keeps responses from proxies and
   later API versions actionable.
9. **Unknown `change_type` and `missing_reason` values are kept as
   strings.** The contract requires accepting unknown reasons, and treating
   change types the same way keeps a later contract version from breaking
   decoding. Applying revisions by precedence needs only `revision_number`.
10. **The SDK checks the response form and the two pagination guarantees it
    relies on, not the contract's invariants.** A record that does not
    match its documented form would be stored wrong. The invariants are the
    API's to enforce ([invariants][dc-invariants]), and checking them would
    need whole datasets.
11. **Only `GET` is retried, on 429, 500, 502, 503, 504, and connection
    failures.** These are the transient outcomes, and every other status
    would repeat. The API's `500 internal` describes an unexpected failure,
    and a `GET` is safe to repeat.
12. **Retry defaults: 4 attempts, 30 s of waiting, backoff from 0.5 s
    doubling to 8 s with full jitter, and `Retry-After` used as given.** A
    call that meets a brief outage succeeds within a few seconds and fails
    within about half a minute, and full jitter spreads out clients that
    failed together. A `Retry-After` beyond the budget raises at once, so
    the caller decides instead of the SDK sleeping past its bounds. No
    measurement supports these numbers, because the API defines no
    throttling.
13. **The key is redacted from httpx's chained exceptions, which are
    kept.** The chain keeps the transport detail a support engineer needs.
    The request it holds carries the header, so the SDK redacts the header
    rather than dropping the cause.
14. **The SDK has no test-control methods.** Test control belongs to the
    demo API, not to customers. The conformance runner and the scenarios
    call `/test` over HTTP.
15. **The deadline defaults to 60 s per call, and each page of an iterator
    gets its own.** That covers every wait the default policy allows plus
    several slow attempts. The time between pages belongs to the caller.
16. **`base_url` defaults to `http://localhost:8080`.** There is no hosted
    deployment, and the demo API listens there by default.
    `FINANCIAL_DATA_BASE_URL` overrides it. A variable set to an empty value
    counts as unset, as in the API ([decision 31][dc-decisions]).
17. **The SDK makes no request at construction and checks no version
    against `/v1/meta`.** Refusing an unsupported API is part of the
    migration stage's design, which is still open.
18. **Release tags are `v<version>`.** This repository has no contract tags
    for them to collide with.

## Owner specifications

The operator decided D1 to D8 on 2026-10-05, each by choosing the
recommended option among those set out below. They are owner
specifications: an implementation follows them as written, and changing one
needs the operator and a new version of this document. The options stay
with each as the record of the choice.

### D1. HTTP stack and dependencies

**Owner specification:** the recommended option, chosen by the operator on
2026-10-05.

| Option | For | Against |
| --- | --- | --- |
| httpx, exposed as `http_client=httpx.Client` | The README's caller-supplied transport with no extra code; customers pass proxies, TLS settings, or `httpx.MockTransport`; the pattern is common in Python SDKs | The public API depends on httpx's major version |
| An SDK-defined transport protocol, with httpx as the default | httpx types stay out of the public API | More code to write and document, and customers adapt their own clients to it |
| The standard library only | No runtime dependencies | Weaker connection pooling and timeout control, and a transport protocol is still needed |

**Specification: httpx, exposed.** `Client(http_client=...)` takes an
`httpx.Client`, as [Supplying an HTTP client](#supplying-an-http-client-d1)
describes. httpx is the only runtime dependency. Records are frozen
dataclasses decoded by the SDK's own code, without Pydantic. Because the
public API depends on httpx's major version, `pyproject.toml` keeps httpx
below its next major version, and moving to a new major is a new SDK
version.

### D2. Who owns the local copy

**Owner specification:** the recommended option, chosen by the operator on
2026-10-05.

| Option | For | Against |
| --- | --- | --- |
| The examples | The SDK stays free of storage choices; the example shows the transaction and precedence rules where a customer can copy them | The Go and TypeScript SDKs repeat the example's logic |
| A pure precedence helper or in-memory local copy in the SDK | One implementation of the precedence rule for the runner and the examples | More public surface for a rule a customer applies in their own store |
| A SQLite-backed store in the SDK | A complete answer for small customers | Much more surface for the other two SDKs to match, and a storage choice made for every customer |

**Specification: the examples.** The SDK returns pages, page tokens,
positions, and records. The scheduled-job example keeps the SQLite store,
the one-transaction checkpoints, and the precedence rule
([Examples](#examples-d2)). The conformance runner keeps its own small local
copy for the contract's `apply_checks` ([conformance](conformance.md)).

### D3. Where throttling and transient failures come from

**Owner specification:** the recommended option, chosen by the operator on
2026-10-05.

| Option | For | Against |
| --- | --- | --- |
| A fault proxy in this repository | No change to the API's contract; it can also produce broken connections, which the API cannot | The Go and TypeScript SDKs need their own, or a shared one |
| Fault injection in the API's test control | All three SDKs share one mechanism | The API's specification changes first, in a new contract version, and gains problem codes for throttling |
| Leaving throttling out of stage 1 | No work | The README's retry and throttling claims go unshown |

**Specification: a fault proxy in this repository.** The API's contract is
unchanged, and rate limits stay outside its scope
([decision 15][dc-decisions]). In tests and in the examples' scenarios, the
proxy sits between the SDK and the pinned API
([fault proxy](conformance.md#the-fault-proxy)).

### D4. Names and Python versions

**Owner specification:** the recommended option, chosen by the operator on
2026-10-05.

| Option | For | Against |
| --- | --- | --- |
| `financial-data-sdk`, imported as `financial_data` | Short; drops the repository's `-python`, as a distribution name usually does | The import name differs from the distribution name |
| `financial-data-sdk`, imported as `financial_data_sdk` | The names match | A longer import |
| `financial-data-client`, imported as `financial_data` | Matches the Polymarket client's naming | Differs from the repository and the README |

| Option | For | Against |
| --- | --- | --- |
| CPython 3.11 and later | Every version still receiving fixes, which is what a provider's customers run | |
| CPython 3.12 and later | Matches the Polymarket client; newer typing syntax | Excludes research environments still on 3.11 |
| CPython 3.10 and later | The widest reach | 3.10 reaches end of life in October 2026 |

**Specification:** the distribution `financial-data-sdk`, imported as
`financial_data`, for CPython 3.11 and later, with CI testing every version
from 3.11 to the newest stable release.

### D5. How CI gets the contract and the API

**Owner specification:** the recommended option, chosen by the operator on
2026-10-05.

| Option | For | Against |
| --- | --- | --- |
| A vendored copy of the contract, and the API from its image only | Tests read reviewable files; the SDK is tested against the API customers run | The steps that need the API wait for its image |
| A pinned checkout of the API's source, then its image | Integration can start once the API serves stage 1, before the image exists | CI builds Go code, and it switches sources later |
| A git submodule for the contract, and the image | Nothing is copied | Submodules complicate checkouts and releases |

**Specification: a vendored contract, and the image only.** `contract/`
holds `fixtures/` and `expected/` from `contract-v0.3.0`, byte for byte.
`contract/CONTRACT.json` names the repository, the tag, the commit, and the
API image the integration tests run, and `scripts/check_contract.py`
compares the files with the tag. Integration tests run only that image. The
image's name and tag scheme are the API's plan step 7, which waits on an
operator decision, so the steps here that need the API wait for it
([implementation plan](../docs/implementation-plan.md)).

### D6. Dataframes in the first release

**Owner specification:** the recommended option, chosen by the operator on
2026-10-05.

| Option | For | Against |
| --- | --- | --- |
| A `pandas` extra | Research workflows use pandas; the README promises dataframe support as an optional dependency | One more dependency to test on each Python version |
| `pandas` and `polars` extras | Covers both libraries | Twice the conversions and tests |
| None in the first release | A smaller release | The README's promise is deferred |

**Specification: a `pandas` extra.** `financial-data-sdk[pandas]` provides
`financial_data.pandas.to_dataframe`, as [Dataframes](#dataframes-d6)
describes, and the research example uses it.

### D7. Request IDs

**Owner specification:** the recommended option, chosen by the operator on
2026-10-05.

| Option | For | Against |
| --- | --- | --- |
| Add a request ID header to the API | The README's promise holds; the Go and TypeScript SDKs and the monitor gain it too; the API's step 4, in progress, is when it costs least | The API's contract changes, in version 0.4.0 |
| The SDK sends its own ID | No API change | The provider never logs it, so it traces nothing on the provider's side |
| Report an ID only when a response has one | No API change | Always `None` against the demo API |

**Specification: add it to the API.** Contract 0.4.0 adds a `Request-Id`
header to every response, errors included. The SDK reports it as
`request_id` on `APIError` and `UnexpectedResponseError` and in its log
records. It reads the header from any response that has one, so an ID from
a proxy or gateway in front of the API is reported too. The README's
promise that errors include the provider's request ID holds from contract
0.4.0. The API change belongs to financial-data-api and is not made in this
repository.

### D8. Where the release is published

**Owner specification:** the recommended option, chosen by the operator on
2026-10-05.

| Option | For | Against |
| --- | --- | --- |
| A GitHub release only | No public package name to claim or maintain for a demonstration | Customers install from a URL |
| PyPI | `pip install financial-data-sdk` works | A demonstration package on the public index; the name must be free |
| Decide later | No commitment now | The release step waits |

**Specification: a GitHub release only.** Each release tag gets a GitHub
release with the wheel and the source distribution attached, installable by
URL.

## Open questions

- **The stage 4 breaking change.** API v1 already uses the explicit time
  fields ([decision 13][dc-decisions]), and the contract leaves the stage's
  change open. Stage 1 does not depend on it; the README's stage 4 section
  names the change once it is chosen.
- **The request ID header's name** in contract 0.4.0, assumed here to be
  `Request-Id` (D7).
- **The API image's name and tag scheme**, which are the API's plan step 7.
  D5 waits on them.
- **Sharing the fault proxy.** If the Go and TypeScript SDKs share it (D3),
  it moves to a repository all three can pin.
- **Leading zeros in values.** The contract's value pattern admits `007.5`.
  A `Decimal` keeps the sign of `-0` but drops leading zeros, so
  `format(value, "f")` would differ from such text. No fixture has one.
  Whether the pattern should forbid leading zeros is the contract's
  question.
- **The retry defaults** rest on no measurement, because the API defines no
  throttling ([decision 12](#decisions)).
- **Bounding a slow response by the deadline.** httpx's timeouts bound each
  socket operation, not a whole request: with httpx 0.28.1, a 1-second
  timeout let a body sent one byte every 0.4 seconds run 2.5 seconds, and
  headers sent the same way run past it too ([Deadline](#deadline)). The
  options include a watchdog that closes the connection at the deadline,
  which needs a thread and reaches the socket only through httpcore's
  extensions; a deadline-aware network backend for the SDK's own client,
  which imports httpcore beside httpx (D1) and does not cover a caller's
  client; and promising less: each socket operation lasts at most the time
  remaining when the attempt began, and the deadline is also checked
  between chunks of the body.
  Plan step 3 waits for the choice, and scenarios for slow headers and a
  slow body come with it.
- **Stage 2.** When `published_as_of`, the revision history, the release
  calendar, and exports enter the SDK, and whether they change its record
  types.
- **Restarting on expiry.** Whether `pages()` should offer an opt-in
  restart, for callers whose storage tolerates combining snapshots, as the
  scheduled-job example's does.

[data-contract]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md
[api]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md
[conformance-format]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/conformance.md
[dc-records]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#revision-records
[dc-invariants]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#invariants
[dc-values]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#values-and-missing-data
[dc-selection]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#selecting-the-revision-available-at-a-cutoff
[dc-stream]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#change-stream
[dc-decisions]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#decisions
[api-versioning]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#versioning
[api-responses]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#responses
[api-errors]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#errors
[api-auth]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#authentication-and-entitlements
[api-positions]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#positions-and-snapshots
[api-pagination]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#pagination
[api-retention]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#retention
[exp-pagination]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/expected/pagination.json
[exp-access]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/expected/access-control.json
