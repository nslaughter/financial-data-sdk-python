# Types and the data contract

This document maps the SDK's public types to the API's contract, version
0.3.0, tagged `contract-v0.3.0`. It gives each record field the contract
field it holds and what that field means, each exception the API's statuses
and codes that raise it, and positions and page tokens the API's rules. It
then says what the SDK checks and what it leaves to the API, and lists the
SDK's limitations.

It describes the SDK for the people who use it; it is not a specification.
The [client contract](../spec/client.md) governs the SDK, and these
documents of the API, at the tag the SDK pins, govern the API:

| Document | Governs |
| --- | --- |
| [Data contract][data-contract] | What the records mean: identities, time fields, values and missing data, selection at a cutoff, and the change stream. |
| [API specification][api] | How the API behaves: parameters, errors, authentication, the simulated clock, pagination, positions, and retention. |

Where this document disagrees with either, they are right, and the
disagreement is a defect here.

The public API is every name in `financial_data.__all__`, each importable
from `financial_data`, and `to_dataframe`, from `financial_data.pandas`
([Packaging](../spec/client.md#packaging-d4-d8)). Modules and names that
begin with `_` are private.

## Methods and endpoints

`Client` is the entry point. Each of its methods calls one of the API's
stage 1 customer endpoints and returns the records below:

| Method | API endpoint | Returns | Needs an entitlement to |
| --- | --- | --- | --- |
| `client.meta()` | [`GET /v1/meta`][api-meta] | `Meta` | Nothing |
| `client.datasets.list()` | [`GET /v1/datasets`][api-datasets] | `tuple[Dataset, ...]` | Nothing |
| `client.datasets.get(dataset_id)` | `GET /v1/datasets/{dataset_id}` | `Dataset` | Nothing |
| `client.series.list()` | [`GET /v1/series`][api-series] | `tuple[Series, ...]` | Nothing |
| `client.series.get(series_id)` | `GET /v1/series/{series_id}` | `Series` | Nothing |
| `client.observations.page(series_id, ...)` | [`GET /v1/observations`][api-observations] | `ObservationPage` | The series' dataset |
| `client.observations.pages(series_id, ...)` | The same, page by page | An iterator of `ObservationPage` | The series' dataset |
| `client.observations.iterate(series_id, ...)` | The same, page by page | An iterator of `Revision` | The series' dataset |
| `client.changes.read(dataset_id, *, after, limit=None)` | [`GET /v1/datasets/{dataset_id}/changes`][api-changes] | `ChangePage` | The dataset |
| `client.changes.pages(dataset_id, *, after, limit=None)` | The same, until caught up | An iterator of `ChangePage` | The dataset |

Every valid customer key can read the catalog. The observations and the
change stream need the key to be entitled to the dataset
([authentication][api-auth]).

Each argument is the API parameter of the same name, and the SDK sends only
the arguments given, so the API's defaults apply
([Arguments](../spec/client.md#arguments)):

| Argument | Python type | API parameter |
| --- | --- | --- |
| `series_id`, `dataset_id` | `str` | An identity from the catalog: a path segment, or the query's `series_id`. |
| `period_start`, `period_end` | `date`, or a `str` sent as given | A [date][api-requests], `YYYY-MM-DD`. An observation is included when its whole period lies in the range: its `period_start` at or after `period_start`, and its `period_end` at or before `period_end`. |
| `available_as_of` | A timezone-aware `datetime`, or a `str` sent as given | The cutoff, a [timestamp][api-requests], `YYYY-MM-DDTHH:MM:SSZ`, at most the API's clock. A `datetime` is converted to UTC and truncated to whole seconds, which never changes the result, because every `available_at` is a whole second. Without one, the API's clock is the cutoff. |
| `page_size`, `limit` | `int` | 1 to 1000; the API's default is 100. |
| `page_token` | `str` | A token from `next_page_token` ([page tokens](#a-querys-snapshot-and-its-page-tokens)). |
| `after` | `int` | A [position](#change-stream-positions) in the change stream; 0 reads from the start. |

## Records

Every record is a frozen dataclass with slots. Every field has the name of
the contract field, or the API member, that it holds, and the fields are in
the contract's order, so a record converts to its JSON form by name.

### `Revision`

One revision record: one version of an observation, with the 14 fields of
the data contract's [revision records][dc-records], the same on every
endpoint ([responses][api-responses]). A query returns the selected revision
of each observation, and the change stream returns every revision in
`sequence` order; both return `Revision`.

| Field | Python type | Contract type | Meaning |
| --- | --- | --- | --- |
| `sequence` | `int` | integer | The revision's position in its dataset's change stream: unique in the dataset, and increasing in the order revisions became available. Sequences can have gaps. |
| `series_id` | `str` | string | The series the revision belongs to. |
| `observation_id` | `str` | string | The observation's stable identity, the same for every revision of one period. |
| `revision_id` | `str` | string | The identity of this revision, unique across the dataset. |
| `revision_number` | `int` | integer | Precedence among the observation's revisions, from 1. A higher number supersedes a lower one, whatever the order of arrival. |
| `change_type` | `ChangeType \| str` | string | What created the revision ([`ChangeType`](#changetype-and-missingreason)). |
| `period_start` | `date` | date | The first day of the period measured. |
| `period_end` | `date` | date | The day after the period ends: the end is exclusive. |
| `value` | `Decimal \| None` | decimal string or null | The measurement. `None` for a withdrawal, or for a released observation without a value. |
| `missing_reason` | `MissingReason \| str \| None` | string or null | Why a released observation has no value; `None` otherwise. |
| `unit` | `str` | string | The unit of `value`, the series' `unit`. |
| `published_at` | `datetime`, UTC | timestamp | When the source made this value public. A provider correction keeps the corrected value's. |
| `received_at` | `datetime`, UTC | timestamp | When the provider acquired it from the source. A provider correction keeps the corrected value's. |
| `available_at` | `datetime`, UTC | timestamp | When an entitled customer could first retrieve this revision through the API. A cutoff selects by it. |

Identifiers are opaque: do not parse them or infer order from them. Only
`sequence` and `revision_number` carry order. The three timestamps answer
different questions ([time fields][dc-time-fields]), and a cutoff selects,
for each observation, the highest `revision_number` among its revisions with
`available_at` at or before it ([selection][dc-selection]).

### `ChangeType` and `MissingReason`

`StrEnum`s of the values of `change_type` and `missing_reason` that the SDK
knows. A member compares equal to its string. A value the SDK does not know
is kept as a plain `str`, because the contract requires clients to accept
missing reasons they do not recognize, and a later contract version could
add a change type ([decision 9](../spec/client.md#decisions)).

| Member | Value | Contract meaning |
| --- | --- | --- |
| `ChangeType.INITIAL_RELEASE` | `initial_release` | The source first publishes the observation. `value` is a decimal, or `None` with a `missing_reason`. |
| `ChangeType.SOURCE_REVISION` | `source_revision` | The source publishes a new value for an observation it already released, including a re-release after a withdrawal. `value` is a decimal, or `None` with a `missing_reason`. |
| `ChangeType.PROVIDER_CORRECTION` | `provider_correction` | The provider fixes a value it served incorrectly, and the source's value is unchanged. `value` is a decimal. |
| `ChangeType.WITHDRAWAL` | `withdrawal` | The source withdraws the observation without a replacement value. `value` and `missing_reason` are `None`. |
| `MissingReason.NOT_COLLECTED` | `not_collected` | The source published the period without a value, because the data was not collected. |

The [change types][dc-change-types] and [values][dc-values] sections of the
data contract define them.

### `Meta`

`client.meta()` returns the body of [`GET /v1/meta`][api-meta].

| Field | Python type | API type | Meaning |
| --- | --- | --- | --- |
| `api_version` | `str` | string | The API version that answered: `v1`. |
| `supported_api_versions` | `tuple[str, ...]` | array of strings | The API versions the server serves. |
| `contract_version` | `str` | string | The contract version the server implements: `0.3.0` for the contract the SDK pins. |
| `server_time` | `datetime`, UTC | timestamp | The server's clock, the [simulated clock][api-clock] in the demo API. A query without a cutoff uses it. |

### `Dataset`

`client.datasets.list()` and `client.datasets.get()` return the datasets of
[`GET /v1/datasets`][api-datasets]. The first three fields are those of the
data contract's [dataset catalog][dc-datasets]; the API adds the last two
for the key that asks.

| Field | Python type | API type | Meaning |
| --- | --- | --- | --- |
| `dataset_id` | `str` | string | The dataset's stable identity. Entitlements name it. |
| `name` | `str` | string | The display name. |
| `description` | `str` | string | What the dataset contains. |
| `entitled` | `bool` | boolean | Whether the key may read the dataset's data. |
| `head_position` | `int \| None` | integer or null | The dataset's [head position](#change-stream-positions), or `None` when the key is not entitled. |

### `Series`

`client.series.list()` and `client.series.get()` return the series of
[`GET /v1/series`][api-series]: the fields of the data contract's
[series catalog][dc-series], and `entitled`.

| Field | Python type | API type | Meaning |
| --- | --- | --- | --- |
| `series_id` | `str` | string | The series' stable identity. Renaming the series does not change it. |
| `dataset_id` | `str` | string | The dataset the series belongs to. Entitlements are granted per dataset. |
| `name` | `str` | string | The display name. |
| `description` | `str` | string | What the series measures. |
| `frequency` | `str` | string | `monthly` for every series in contract 0.3.0. |
| `unit` | `str` | string | The unit of every value in the series. |
| `base_period` | `str` | string | The reference period of an index. |
| `seasonal_adjustment` | `str` | string | `seasonally_adjusted` or `not_seasonally_adjusted`. |
| `source` | `str` | string | The organization that publishes the measurement. |
| `release_schedule` | `str` | string | The source's stated schedule, for people. |
| `entitled` | `bool` | boolean | Whether the key may read the data of the series' dataset. |

### `ObservationPage`

`client.observations.page()` and `client.observations.pages()` return pages
of [`GET /v1/observations`][api-observations].

| Field | Python type | API type | Meaning |
| --- | --- | --- | --- |
| `data` | `tuple[Revision, ...]` | array of revision records | The selected revision of each observation on this page, ordered by `period_start`. A page is empty only when the whole result is. |
| `position` | `int` | integer | The query's [snapshot position](#a-querys-snapshot-and-its-page-tokens), the same on every page of the query. |
| `snapshot_expires_at` | `datetime`, UTC | timestamp | The instant the snapshot expires. A page token is accepted while the API's clock is before it. |
| `next_page_token` | `str \| None` | string or null | The token that continues the query, or `None` on the last page. |

### `ChangePage`

`client.changes.read()` and `client.changes.pages()` return reads of
[`GET /v1/datasets/{dataset_id}/changes`][api-changes].

| Field | Python type | API type | Meaning |
| --- | --- | --- | --- |
| `data` | `tuple[Revision, ...]` | array of revision records | The visible revisions with `sequence` greater than `after`, in `sequence` order, at most `limit` of them. |
| `next_position` | `int` | integer | The `sequence` of the last revision returned, or `after` when none is. Save it with the revisions, in one transaction. |
| `head_position` | `int` | integer | The dataset's head position. |
| `caught_up` | `bool`, a property | Not sent | Whether `next_position` equals `head_position`. |

## Values, dates, and timestamps

- `value` is a `Decimal` built from the API's string, never through `float`,
  so `format(value, "f")` returns the API's text, `"102.0"` as `"102.0"`,
  for every value without leading zeros ([limitations](#limitations)).
  `str(value)` does not: it writes `0.0000001` as `1E-7`.
- `None` in `value` is distinct from zero and from an absent observation.
  A released observation without a value is a record with a
  `missing_reason`, and a withdrawn one is a record with `change_type`
  `withdrawal` and neither. An observation with no revision available at the
  cutoff is not returned at all ([values][dc-values]).
- Dates are `datetime.date` values, without a time zone. `period_end` is
  exclusive: August 2026 runs from `2026-08-01` to `2026-09-01`.
- Timestamps are timezone-aware `datetime` values in UTC, with whole
  seconds, as the API writes them ([formats][api-requests]).

## Positions and page tokens

The SDK returns the positions and page tokens the API sends, and never
computes or changes one. Saving them, and deciding what to do when the API
refuses one, are the caller's.

### A query's snapshot and its page tokens

- A query is evaluated at a **snapshot position**: the dataset's head
  position when its first page is served. Every page of the query considers
  only the revisions with `sequence` at or below it, whatever arrives later,
  so pages never mix states. `ObservationPage.position` reports it. The
  result of a query without a cutoff is the state after applying every
  revision up to that position, so the change stream continues from it
  ([positions][api-positions], [one position][dc-one-position]).
- A **page token** is opaque: do not parse, construct, or alter one. It is
  bound to its endpoint, to every other parameter of the first request, and
  to the key. To resume a query, call `pages()`, on a client with the same
  key, with the same arguments and `page_token` set to the saved token;
  `pages()` sends them unchanged ([pagination][api-pagination],
  [Resuming a query](../spec/client.md#resuming-a-query)).
- A snapshot lives for 3,600 seconds of the API's clock from its first page
  ([retention][api-retention]).
- A token never carries access. The API checks the key and its entitlement
  on every page.

| When a page token is | The API answers | The SDK raises | The caller |
| --- | --- | --- | --- |
| Used at or after `snapshot_expires_at` | `410 page_token_expired` | `PageTokenExpiredError` | Restarts the query. The SDK never restarts it, because the new snapshot has a new position ([decision 7](../spec/client.md#decisions)). |
| Issued before the API's last reset or restart, or altered | `400 invalid_page_token` | `PageTokenError`, `code` `invalid_page_token` | Restarts the query. |
| Sent with different arguments | `400 page_token_mismatch` | `PageTokenError`, `code` `page_token_mismatch` | Sends the first request's arguments, as `pages()` does. |
| Sent to another endpoint, or with another key | `400 page_token_mismatch` | `PageTokenError`, `code` `page_token_mismatch` | Resumes on the endpoint the token was issued for, with the key it was issued to, or restarts the query. |
| Sent with a key since deactivated, or no longer entitled | `401 unauthenticated` or `403 not_entitled` | `AuthenticationError` or `NotEntitledError` | Needs the key's access restored. |

### Change-stream positions

- A position `P` in a dataset's change stream means every revision with
  `sequence` at or below `P` has been applied. Position 0 is before the first
  revision, and each dataset has its own positions
  ([change stream][dc-stream]).
- A dataset's **head position** is the highest `sequence` among its visible
  revisions, or 0 when none is visible ([positions][api-positions]).
  `Dataset.head_position` and `ChangePage.head_position` report it.
- Save the positions the API returns, such as `ChangePage.next_position` and
  the `position` of a query without a cutoff. Never compute one from a
  `sequence`, because sequences can have gaps.
- A revision at or below the saved position has already been applied. Update
  an observation's current value only when the incoming `revision_number` is
  higher than the one held.
- A position equal to the head never expires.

| When `after` is | The API answers | The SDK raises | The caller |
| --- | --- | --- | --- |
| Greater than the head position, as after a reset of the API to an earlier clock | `400 position_ahead` | `PositionAheadError` | Loads the data again with a query. |
| A position whose next event is past retention, 1,095 days after its `available_at` | `410 position_expired` | `PositionExpiredError` | Loads the data again with a query, and continues from that query's position ([retention][api-retention]). |

## Exceptions

The SDK chooses an exception by the API's `code` first, because the code is
stable and is what clients act on ([errors][api-errors]). When the response
is a problem response, with `Content-Type: application/problem+json` and a
JSON object as its body, and its `code` and status together match a row
below, it raises that row's exception. Otherwise it chooses by status alone:
`401` raises `AuthenticationError`, `429` `RateLimitError`, any `5xx`
`ServerError`, and any other `4xx` `APIError`. A response without a problem
body never raises more than its status says: a `404` from a proxy or a wrong
base URL is an `APIError`, not a `NotFoundError`
([Choosing the exception](../spec/client.md#choosing-the-exception)).

| Exception | Base classes | API status and code | Raised when |
| --- | --- | --- | --- |
| `FinancialDataError` | `Exception` | Those of its subclasses | The base class of every exception below. |
| `ConfigError` | `FinancialDataError`, `ValueError` | No request is sent | An argument of `Client`, `with_options`, or `RetryPolicy` is invalid, or no key is given or set in the environment. |
| `ClientClosedError` | `FinancialDataError`, `RuntimeError` | No request is sent | A call is made on a closed client, on a client derived from one, or through a caller-supplied HTTP client the caller has closed. |
| `APIError` | `FinancialDataError` | Any `4xx` that no row below names | The API, or something in front of it, refused the request, and no subclass applies. It is the base class of the exceptions below. |
| `InvalidRequestError` | `APIError` | `400` `unknown_parameter`, `missing_parameter`, `invalid_parameter`, `conflicting_cutoffs`, or `cutoff_in_future` | The API refused an argument. `parameter` names it as the API does, and names `period_end` for an empty or reversed period range. |
| `PageTokenError` | `APIError` | `400` `invalid_page_token` or `page_token_mismatch` | The API refused a page token ([page tokens](#a-querys-snapshot-and-its-page-tokens)). |
| `PageTokenExpiredError` | `PageTokenError` | `410 page_token_expired` | The token's snapshot expired. Restart the query. |
| `PositionAheadError` | `APIError` | `400 position_ahead` | `after` is beyond the dataset's head ([positions](#change-stream-positions)). |
| `PositionExpiredError` | `APIError` | `410 position_expired` | The events after `after` are past retention. Load the data again with a query. |
| `AuthenticationError` | `APIError` | `401`, with any code; the API's is `unauthenticated` | The key was refused. The API refuses a key that is unknown, inactive, or not a customer key. |
| `NotEntitledError` | `APIError` | `403 not_entitled` | The key is valid but not entitled to the dataset the request reads. |
| `NotFoundError` | `APIError` | `404 not_found` | No such series, dataset, or path. |
| `UnsupportedAPIVersionError` | `APIError` | `404 unsupported_api_version` | The path names an API version the server does not serve. |
| `RateLimitError` | `APIError` | `429`, with any code | A request was throttled, and retries stopped. The API defines no throttling, so this comes from something in front of it, such as a gateway. |
| `ServerError` | `APIError` | Any `5xx`, with any code; the API's is `500 internal` | The server failed, and retries stopped. |
| `TransportError` | `FinancialDataError` | No usable response | Connecting failed, the connection broke, the response could not be parsed, the request could not be sent, or a caller-supplied client's own timeout expired, and retries stopped. `__cause__` is httpx's exception, with the key removed. |
| `DeadlineExceededError` | `FinancialDataError`, `TimeoutError` | No complete response before the deadline | The call's deadline passed while a request was in flight. It is not retried. A response slow to send its headers can run past the deadline ([Deadline](../spec/client.md#deadline)). |
| `UnexpectedResponseError` | `FinancialDataError` | A status or body the API does not send: a `1xx` or `3xx`, a status outside 100 to 599, or a `2xx` that fails [validation](#in-every-successful-response) | The response is one the API's documents do not allow: one of those, or a body that does not decode as its `Content-Encoding` says. A page that breaks a [pagination guarantee](#across-pages) raises it too, with `status` `None`. It is not retried. |

The API's other codes raise `APIError`: `405 method_not_allowed`;
`409 clock_backwards`, from test control, which the SDK does not call; and
`410 export_expired`, from stage 2. An argument of the wrong type raises
`TypeError`, and a naive `datetime` or an unusable path segment raises
`ValueError`, before any request ([what the SDK checks](#what-the-sdk-checks)).

An empty result is not an error: a query that matches nothing returns an
empty page, and a refusal is always an exception, so a caller can tell
"nothing new" from "not allowed"
([What is not an error](../spec/client.md#what-is-not-an-error)).

### Attributes

| Attribute | On | Holds |
| --- | --- | --- |
| `status` | `APIError`, `UnexpectedResponseError` | The HTTP status, or `None` for an `UnexpectedResponseError` that a pagination check raised. |
| `code` | `APIError` | The problem response's `code`: stable, and what to act on. |
| `title` | `APIError` | The problem response's `title`, fixed for each code. |
| `detail` | `APIError` | The problem response's `detail`, which explains this occurrence and may change between versions. |
| `parameter` | `APIError` | The problem response's `parameter`: the query or path parameter at fault. |
| `problem` | `APIError` | The whole problem body, as a read-only mapping. |
| `retry_after` | `APIError` | The seconds the response's `Retry-After` asked for. |
| `request_id` | `APIError`, `UnexpectedResponseError` | The response's `Request-Id` header ([D7](../spec/client.md#d7-request-ids)). |
| `method`, `path` | `APIError`, `TransportError`, `DeadlineExceededError`, `UnexpectedResponseError` | The request's method, and its path with the query string, as sent. |
| `attempts` | `APIError`, `TransportError`, `DeadlineExceededError`, `UnexpectedResponseError` | How many requests the call sent, retries included. |

Each attribute taken from a response is `None` when the response lacks it,
and the problem members are `None` when the response is not a problem
response. The key never appears in an exception: the SDK replaces it with
`[redacted]` in any text it takes from a response, and in place of any
argument that holds it
([Credentials and logging](../spec/client.md#credentials-and-logging)).

## `Client`, `RetryPolicy`, and `__version__`

- `Client` takes the settings in
  [Configuration](../spec/client.md#configuration). `api_key`, or
  `FINANCIAL_DATA_API_KEY`, is a customer key from the
  API's credentials, sent as `Authorization: Bearer <key>`
  ([authentication][api-auth]). `base_url` defaults to
  `FINANCIAL_DATA_BASE_URL`, then `http://localhost:8080`, where the demo API
  listens by default. `timeout` is the deadline of one call, 60 seconds by
  default; each page of an iterator is one call.
- `RetryPolicy` sets the retries of one call: `max_attempts` (4),
  `max_retry_wait` (30.0 seconds), `base_delay` (0.5 seconds), `max_delay`
  (8.0 seconds), and `jitter` (`True`). Only these are retried: a `429`,
  `500`, `502`, `503`, or `504`; a connection that fails, or that breaks or
  closes before a complete response; a response httpx cannot parse; and,
  when the call has no deadline, the expiry of a caller-supplied client's
  own timeout ([What is retried](../spec/client.md#what-is-retried)). A
  `Retry-After` beyond the remaining budget or deadline raises at once, with
  `retry_after` set, so the caller can wait that long itself.
- `__version__` is the distribution's version, which the SDK sends in its
  `User-Agent`.

## DataFrames

`to_dataframe(revisions)`, in `financial_data.pandas`, needs the `pandas`
extra, `financial-data-sdk[pandas]`. It returns a `pandas.DataFrame` with
one row per revision, in the order given, and the 14 fields of `Revision`
as columns, with the same names, in the contract's order
([Dataframes](../spec/client.md#dataframes-d6)):

| Columns | dtype | Holds |
| --- | --- | --- |
| `sequence`, `revision_number` | `int64` | The integers. |
| `series_id`, `observation_id`, `revision_id`, `change_type`, `missing_reason`, `unit` | `object` | `str`, and `None` where `missing_reason` is null. `change_type` and `missing_reason` hold plain strings, not enum members. |
| `period_start`, `period_end` | `datetime64[us]` | Midnight on the date, without a time zone. `period_end` stays exclusive. |
| `value` | `object` | `Decimal` or `None`, never `float` or `NaN`. |
| `published_at`, `received_at`, `available_at` | `datetime64[us, UTC]` | The instants, in UTC. |

It never drops, combines, or reorders rows, and it never pivots,
deduplicates, or selects a current revision, so the revision information
stays for the caller's analysis. An empty input gives an empty DataFrame
with the same columns.

## What the SDK checks

The SDK checks only what it needs to send a request faithfully and to store
a response correctly. It leaves the API's rules about arguments and data to
the API, so those rules have one implementation, and each error names the
parameter as the API does ([decisions 3 and 10](../spec/client.md#decisions)).

### Before a request

- **Types.** A wrong type raises `TypeError`: a `bool` where an `int` is
  expected, a `datetime` as `period_start` or `period_end`, or a keyword
  argument the method does not define, such as a misspelled cutoff.
- **A naive `datetime`** as `available_as_of` raises `ValueError`, because
  its instant is unknown.
- **A path segment** that is empty, `.`, or `..` raises `ValueError`, because
  it would address another resource. As a query's `series_id`, the same text
  is sent, and the API refuses it.

### In every successful response

- The `Content-Type` is `application/json`, and the body is a JSON object.
- Every member the API documents for the response is present, with its JSON
  type. Only `value`, `missing_reason`, `head_position`, and
  `next_page_token` may be `null` ([responses][api-responses]).
- Integers are JSON integers, not booleans or fractions. `value` is `null`
  or a string matching `^-?[0-9]+(\.[0-9]+)?$`. Dates match `YYYY-MM-DD`
  and are valid dates, and timestamps match `YYYY-MM-DDTHH:MM:SSZ` and are
  valid instants.
- Members the SDK does not know are ignored, as the API's
  [versioning][api-versioning] requires.

A response that fails raises `UnexpectedResponseError`, which names the
member and, for a record, its index, such as `data[3].value`
([Validating responses](../spec/client.md#validating-responses)).

### Across pages

- Every page of one observation query has the same `position`, and no page
  returns as its `next_page_token` a token the iterator has already sent.
- A change page that is not caught up holds at least one revision, and its
  `next_position` is greater than the position it read from.

A page that breaks one raises `UnexpectedResponseError`, so an iterator
never mixes two snapshots or loops forever
([Pagination](../spec/client.md#pagination)).

### Left to the API

- **The form and range of each argument the SDK does not format:** a date or
  timestamp given as a `str`, an empty `series_id` in a query, `page_size`
  and `limit` from 1 to 1000, `after` not negative, a period range that is
  empty or reversed, and a cutoff after the API's clock
  ([requests][api-requests]).
- **Existence and access:** an unknown series or dataset, the key and its
  entitlements, page tokens, and the retention of positions.
- **The contract's [invariants][dc-invariants] across records:** unique
  identities, precedence, `sequence` order, `published_at` at or before
  `received_at` at or before `available_at`, whole monthly periods, the unit
  of the series, and the combinations of `change_type`, `value`, and
  `missing_reason` that the change types allow. The SDK decodes a record that
  breaks one as it is.
- **Ranges of the response's numbers:** the API keeps integers below 2^53.
  The SDK does not check that, or that a position or `sequence` is not
  negative.

## Limitations

- **Stage 1 only.** The SDK has no method for `published_as_of`, the
  revision history, the release calendar, or exports, the API's
  [stage 2][api-stages]. Each needs a later version of the client contract;
  until then they do not change the SDK's records or errors
  ([The API contract](../spec/client.md#the-api-contract)). A
  `published_as_of` argument raises Python's `TypeError`.
- **Leading zeros in values.** The contract's value pattern admits text such
  as `007.5`. A `Decimal` keeps the sign of `-0` but drops leading zeros, so
  `format(value, "f")` writes `7.5`. No fixture has one, and whether the
  pattern should forbid leading zeros is the contract's question
  ([Open questions](../spec/client.md#open-questions)).
- **The range a local copy can reproduce.** The SDK keeps no local copy
  ([D2](../spec/client.md#d2-who-owns-the-local-copy)). A query returns each
  observation's selected revision at its snapshot, not the revisions that
  revision superseded, so a copy loaded by a query lacks them. Such a copy,
  kept current from the change stream, answers a cutoff exactly only from
  the latest `available_at` among the records its load saved, or from the
  start if it saved none, up to the API's time before its last catch-up
  began. Every other cutoff needs the API. A copy first loaded after the
  August 2026 revision became available, for example, cannot answer the
  September 4, 2026 cutoff. The client contract's scheduled-job example
  keeps such a copy and refuses a cutoff outside that range
  ([What the local copy can reproduce](../spec/client.md#what-the-local-copy-can-reproduce)).
- **Retry defaults that rest on no measurement.** Four attempts, 30 seconds
  of waiting, and backoff from 0.5 seconds doubling to 8 seconds with full
  jitter are chosen so that a call that meets a brief outage succeeds within
  a few seconds and fails within about half a minute. The API defines no
  throttling ([out of scope][api-out-of-scope]), so nothing measured
  supports these numbers ([decision 12](../spec/client.md#decisions)).
  `RetryPolicy` changes them.
- **Request IDs.** The API at contract 0.3.0 sends no `Request-Id`, so
  `request_id` is `None` unless a proxy or gateway in front of the API sends
  one. Contract 0.4.0 is to add the header
  ([D7](../spec/client.md#d7-request-ids)).

[data-contract]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md
[api]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md
[dc-datasets]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#dataset-catalog
[dc-series]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#series-catalog
[dc-records]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#revision-records
[dc-invariants]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#invariants
[dc-time-fields]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#time-fields-answer-different-questions
[dc-values]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#values-and-missing-data
[dc-change-types]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#change-types
[dc-selection]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#selecting-the-revision-available-at-a-cutoff
[dc-one-position]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#a-query-is-evaluated-at-one-position
[dc-stream]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#change-stream
[api-stages]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#stages
[api-versioning]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#versioning
[api-requests]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#requests
[api-responses]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#responses
[api-errors]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#errors
[api-auth]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#authentication-and-entitlements
[api-clock]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#simulated-clock
[api-positions]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#positions-and-snapshots
[api-pagination]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#pagination
[api-retention]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#retention
[api-meta]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#get-v1meta
[api-datasets]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#get-v1datasets
[api-series]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#get-v1series
[api-observations]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#get-v1observations
[api-changes]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#get-v1datasetsdataset_idchanges
[api-out-of-scope]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/api.md#out-of-scope-for-v1
