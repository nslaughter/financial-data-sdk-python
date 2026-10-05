# Conformance: financial data SDK for Python

**Status:** Draft 0.1.0, for the operator's review, with the
[client contract](client.md); not tagged. Scenarios that rest on one of the
contract's owner specifications name it.

The SDK passes two kinds of checks:

- **The shared checks.** These are the expected results of the pinned
  contract version, in `contract/expected/`, which every SDK and the API
  pass. The [SDK runner](#the-sdk-runner) executes them as the API's
  [conformance format][conformance-format] defines, sending each request
  through the SDK wherever the SDK has a method for it.
- **The SDK scenarios.** These cover behavior the shared files cannot show:
  retries, throttling, broken connections, deadlines, response validation,
  resuming, credentials in errors and logs, the caller's HTTP client, the
  dataframe conversion, and the examples. They run through the
  [fault proxy](#the-fault-proxy) against the pinned API.

The scenarios were written from the client contract and the API's
documents, not by running an SDK. An SDK that disagrees with one needs
investigation, not a changed expectation. If the investigation finds an
error in a scenario, correct it in a new version of this document.

## The API under test

Both kinds of checks run against the API image that
`contract/CONTRACT.json` names (D5), started with `TEST_CONTROL=enabled` and
the default `CLOCK_START`, `2026-10-01T00:00:00Z`. Both reset the API
through test control before every check or scenario, so they run one at a
time against one server. The credentials are those in
`contract/fixtures/credentials.json`: `cred_research` (`demo-research-key`)
unless a check names another, `cred_unentitled`, and the test-control key.

## The SDK runner

### What it runs

The runner takes the API's base URL and a stage. At stage 1 it runs every
file the [stage table][stage-table] requires of an SDK at stage 1:
`august-2026-at-cutoffs`, `full-history`, `missing-value`,
`withdrawal-and-rerelease`, `out-of-order-arrival`, `provider-correction`,
`late-source-release`, `change-stream`, `simulated-clock`, `pagination`,
`access-control`, and `request-errors`. It honors each scenario's `stages`
member.

It follows the API's conformance format exactly: independence, the default
credential, paging, status, matching, references, and failure reports. The
rules below add only how a step reaches the API and how an SDK result is
compared.

### Clients

- For each customer credential, the runner builds one SDK client:
  `Client(api_key=<the credential's key>, base_url=<the API>, timeout=30.0, retry=RetryPolicy(max_attempts=1), http_client=<the recording client>)`.
  Retries are off, so each SDK call sends exactly one request.
- The recording client is an `httpx.Client` whose request event hook
  records each request's method, path, and query parameters. Passing it
  also exercises D1.
- Test actions (`set_clock`, `set_credential`, `reset`, and the reset before
  each check) and requests sent over HTTP use the runner's own
  `httpx.Client`, never the SDK.

### Which steps go through the SDK

A request step goes through the SDK when all of the following hold, after
its references are resolved. Otherwise the runner sends it over HTTP,
exactly as the API runner does.

1. Its method is `GET`, and its path matches a row of the table below
   exactly: path parameters are neither empty nor `.` or `..`, and there is
   no trailing slash.
2. It has no `authorization` member, and its `credential` is absent or
   names a customer credential.
3. Every member of its `query` is an argument of the row's method, every
   required argument is present, and no member is an array.
4. Every integer argument (`page_size`, `limit`, or `after`) is a string
   `s` that `int(s)` accepts and for which `str(int(s)) == s`, so the SDK
   sends the same text. A value such as `010`, `+5`, `1.5`, or `ten` goes
   over HTTP; `0` and `-1` go through the SDK.
5. Its `expect` has none of `headers`, `body_sha256`, or `body_lines`.

| Path | SDK call |
| --- | --- |
| `/v1/meta` | `client.meta()` |
| `/v1/datasets` | `client.datasets.list()` |
| `/v1/datasets/{dataset_id}` | `client.datasets.get(dataset_id)` |
| `/v1/series` | `client.series.list()` |
| `/v1/series/{series_id}` | `client.series.get(series_id)` |
| `/v1/observations` | `client.observations.page(**query)`, with `series_id`, `period_start`, `period_end`, `available_as_of`, `page_size`, and `page_token` |
| `/v1/datasets/{dataset_id}/changes` | `client.changes.read(dataset_id, after=int(after))`, adding `limit=int(limit)` when the step has `limit` |

String arguments are passed as strings, so the SDK sends them exactly as
written ([decision 3](client.md#decisions)). A `null` query member is left
out of the call. Integer arguments are converted with `int()` and passed
even when zero, so `limit` `0` reaches the API and is refused there.

In the stage 1 files, these steps go over HTTP: test actions; requests
without a key, with a raw `Authorization` header, or with the test-control
key; `/test` paths; unknown, repeated, or missing parameters; integers not
in canonical form; and routing errors (other versions, unknown paths, and
other methods). They check the API, not the SDK. The runner sends them so
that the scenarios around them run as written.

### Checking an SDK step

1. The recording client must have recorded one request for the step, with
   the step's method and path and exactly the step's query parameters and
   values: none missing, none added.
2. If the SDK returned, the step's expected status must be `200`. The
   runner converts the result to JSON ([below](#converting-a-result)) and
   matches it against `expect.body`.
3. If the SDK raised, the exception must be an `APIError`. Its class must
   be exactly the one the client contract's
   [exceptions table](client.md#exceptions) gives for the expected status
   and, when the step names one, the expected code. Its `status` must equal
   the expected status, its `code` the expected `code`, and its `problem`
   must match `expect.body`. Any other exception fails the step.
4. A later reference to the step resolves against the converted result, or
   against the exception's `problem`.

### Converting a result

| SDK value | JSON |
| --- | --- |
| A record | An object with one member per dataclass field, by the same name. Properties such as `caught_up` are not fields and are left out. |
| A tuple returned by `list()` | `{"data": [...]}` |
| Any other tuple | An array |
| `str`, including a `StrEnum` member | A string; a member converts to its value |
| `int`, `bool` | A number, a boolean |
| `None` | `null` |
| `Decimal` | A string, `format(value, "f")` |
| `date` | A string, `YYYY-MM-DD` |
| `datetime` | A string, `YYYY-MM-DDTHH:MM:SSZ` |

The conversion is the round trip the client contract promises. An expected
`"102.0"` matches only if the SDK kept it as `102.0`.

### Query checks

For each check, the runner calls `client.observations.pages(**query)` and
concatenates every page's `data`, which `expected` must match. It also
calls `client.observations.iterate(**query)`, which must return the same
records in the same order. For `pages_with_page_size_10`, it calls
`pages(**query, page_size=10)` and compares each page's count and its first
and last `observation_id`. `contrast_available_as_of` belongs to
`published-as-of`, a stage 2 file.

### Change-stream checks

- **`position_checks`:** after the reset at `at`, the runner calls
  `client.datasets.get("core-indicators")`. Its `head_position` must equal
  `expected_position`.
- **`read_checks`:** the runner calls
  `client.changes.read("core-indicators", after=after_position, limit=limit)`.
  `expected` must match `data`, and `next_position` and `head_position`
  must equal `expected_next_position` and `expected_head_position`.
- **`apply_checks`:** the runner keeps its own local copy (D2): a map from
  `observation_id` to the current revision. It reads
  `client.changes.pages("core-indicators", after=0)` and applies, in
  order, each revision with `sequence` at or below `from_position`, then
  each with `sequence` above `from_position` and at or below
  `through_position`. A revision replaces its observation's current
  revision only when its `revision_number` is higher. The current revision
  of `observation_id` must be `expected_current_revision_id`. The runner
  compares sequences here because it is checking the rule, not resuming a
  stream.

## The fault proxy

D3 places throttling and transient failures in a proxy in this repository,
because the API defines neither. The proxy is a local HTTP/1.1 server on
`127.0.0.1` that runs in its own thread, under `tests/faults/`. It forwards
each request to the API unchanged and returns the API's response
unchanged, unless a rule matches the request.

A rule matches by method and exact path, and then by which matching
requests it applies to: request _n_ only, request _n_ and every later one,
or every request. Requests are counted per rule from when the rule was
added. Its action is one of these:

| Action | Effect |
| --- | --- |
| `respond` | Answers with the given status, headers, and body, without forwarding. |
| `problem` | Answers with the given status as `application/problem+json`, with the body `{"status": <status>, "code": <code>, "title": "Injected fault", "detail": "Injected by the fault proxy.", "parameter": <parameter or null>}`, and any given headers. |
| `drop` | Reads the request, then closes the connection without a response. |
| `stall` | Reads the request and sends nothing, until the client closes the connection or the scenario ends. |
| `rewrite` | Forwards the request, then answers with the API's response, its body parsed, changed by the given function, and serialized again. |

The proxy records every request it receives (method, path, query, headers,
and arrival time on a monotonic clock) and every response it returns
(status and headers). Scenarios check those records. A scenario starts by
clearing the rules and the records and resetting the API directly, not
through the proxy.

For an HTTP date in `Retry-After`, the proxy writes its own UTC time plus
the given seconds, truncated to a whole second, in IMF-fixdate form. The
wait that date asks for is therefore more than _n_ − 1 and at most _n_
seconds.

## How a scenario runs

### Profile

Every scenario starts from this client, unless it says otherwise. The
values are short so that scenarios run in seconds; they are not
recommended defaults.

| Setting | Value |
| --- | --- |
| `api_key` | `demo-research-key` |
| `base_url` | The fault proxy |
| `timeout` | `5.0` |
| `retry` | `RetryPolicy(max_attempts=3, max_retry_wait=3.0, base_delay=0.1, max_delay=0.4, jitter=False)` |

Without jitter, the waits before the second and third attempts are 0.1 s
and 0.2 s.

### Writing a scenario

Each scenario below has a heading with its name, a paragraph saying what it
shows, and then:

- **Clock:** the clock the API is reset to; the default clock when absent.
- **Proxy:** the rules in force from the start. A step may add or clear
  rules.
- **Steps:** numbered. Each step does one thing and says what must happen.
  "Requests" counts the requests the proxy received during that step, and
  "elapsed" is measured on a monotonic clock around the step's call. Time
  windows include an allowance for scheduling on a CI runner.

The test for scenario `name` is `test_<name>`, with hyphens replaced by
underscores, in `tests/scenarios/`. A scenario is turned on by listing its
name in `tests/scenarios/enabled.txt`. The harness fails if a listed name
has no heading here or no test, and from plan step 11 on, it fails if a
heading here is not listed. Nothing else skips or relaxes a
scenario ([decision 1](#decisions)).

## Scenarios

### Throttling and transient failures

#### `retry-after-seconds`

A throttled request waits as long as `Retry-After` asks, then succeeds.

- **Proxy:** `GET /v1/series/activity-index`, request 1: `problem` 429
  `rate_limited`, with `Retry-After: 1`.

1. `client.series.get("activity-index")` returns the `Series` whose
   `series_id` is `activity-index`. Requests: 2, the second arriving 1.0 to
   1.4 s after the first. One `INFO` record from `financial_data` names
   status 429 and a wait of 1.0 s.

#### `retry-after-http-date`

`Retry-After` in its date form is honored as well.

- **Proxy:** `GET /v1/series/activity-index`, request 1: `problem` 429
  `rate_limited`, with `Retry-After` an HTTP date 2 s ahead.

1. `client.series.get("activity-index")` returns the series. Requests: 2,
   the second arriving 1.0 to 2.4 s after the first.

#### `retry-after-invalid`

An unreadable `Retry-After` is ignored, and backoff applies.

- **Proxy:** `GET /v1/series/activity-index`, request 1: `problem` 429
  `rate_limited`, with `Retry-After: soon`.

1. `client.series.get("activity-index")` returns the series. Requests: 2,
   the second arriving 0.1 to 0.4 s after the first.

#### `retry-after-beyond-budget`

A wait longer than the retry budget is not slept: the caller learns at once
how long the API asked for.

- **Proxy:** `GET /v1/series/activity-index`, every request: `problem` 429
  `rate_limited`, with `Retry-After: 30`.

1. `client.series.get("activity-index")` raises `RateLimitError` with
   `status` 429, `code` `rate_limited`, `retry_after` 30.0, and `attempts`
   1. Requests: 1. Elapsed: under 0.5 s.

#### `retry-after-beyond-deadline`

A wait that would outlast the caller's deadline is not slept either.

- **Proxy:** `GET /v1/series/activity-index`, every request: `problem` 429
  `rate_limited`, with `Retry-After: 2`.

1. `client.with_options(timeout=1.0).series.get("activity-index")` raises
   `RateLimitError` with `retry_after` 2.0 and `attempts` 1. Requests: 1.
   Elapsed: under 0.5 s.

#### `throttled-on-every-attempt`

Throttling beyond the retry policy ends in `RateLimitError` within the
policy's bounds, as the README's demonstration requires.

- **Proxy:** `GET /v1/series/activity-index`, every request: `problem` 429
  `rate_limited`, without `Retry-After`.

1. `client.series.get("activity-index")` raises `RateLimitError` with
   `attempts` 3 and `retry_after` `None`. Requests: 3. Elapsed: 0.3 to
   0.7 s. Two `INFO` records, with waits of 0.1 and 0.2 s.

#### `transient-errors-then-success`

Gateway errors without a problem body are retried, and the call succeeds
when the API answers.

- **Proxy:** `GET /v1/datasets`, request 1: `respond` 503, `text/plain`,
  `Service Unavailable`; request 2: `respond` 502, `text/html`,
  `<html><body>Bad Gateway</body></html>`.

1. `client.datasets.list()` returns one `Dataset`, `core-indicators`, with
   `entitled` true and `head_position` 37. Requests: 3. Elapsed: 0.3 to
   0.7 s.

#### `server-error-exhausts-attempts`

The API's own `500` is retried within bounds, then raised with its code.

- **Proxy:** `GET /v1/datasets`, every request: `problem` 500 `internal`.

1. `client.datasets.list()` raises `ServerError` with `status` 500, `code`
   `internal`, and `attempts` 3. Requests: 3. Elapsed: 0.3 to 0.7 s.

#### `connection-dropped-then-success`

A connection closed without a response is retried.

- **Proxy:** `GET /v1/datasets`, request 1: `drop`.

1. `client.datasets.list()` returns the dataset. Requests: 2. Elapsed: 0.1
   to 0.5 s.

#### `connection-dropped-every-attempt`

When no response ever arrives, the caller gets a `TransportError`, not an
API error.

- **Proxy:** `GET /v1/datasets`, every request: `drop`.

1. `client.datasets.list()` raises `TransportError` with `attempts` 3,
   whose `__cause__` is an `httpx.TransportError`. Requests: 3.

#### `deadline-during-request`

The deadline bounds a request that never answers, and nothing is retried
after it.

- **Proxy:** `GET /v1/datasets`, every request: `stall`.

1. `client.with_options(timeout=1.0).datasets.list()` raises
   `DeadlineExceededError` with `attempts` 1. Requests: 1. Elapsed: 1.0 to
   1.5 s.

#### `deadline-bounds-backoff`

The deadline also bounds the waits between attempts: the SDK does not
start a wait that would end after it.

- **Proxy:** `GET /v1/datasets`, every request: `respond` 503,
  `text/plain`, `Service Unavailable`.

1. `client.with_options(timeout=0.25).datasets.list()` raises `ServerError`
   with `status` 503, `code` `None`, and `attempts` 2. Requests: 2.
   Elapsed: 0.1 to 0.25 s.

#### `refusals-are-not-retried`

A refusal will repeat, so each is raised after one request, as its own
exception.

1. A client with `cred_unentitled`'s key:
   `client.observations.page("activity-index")` raises `NotEntitledError`
   with `status` 403 and `code` `not_entitled`. Requests: 1.
2. `client.series.get("no-such-series")` raises `NotFoundError`. Requests:
   1.
3. `client.observations.page("activity-index", available_as_of="yesterday")`
   raises `InvalidRequestError` with `code` `invalid_parameter` and
   `parameter` `available_as_of`. Requests: 1.
4. `client.observations.page("activity-index", available_as_of="2026-10-01T00:00:01Z")`
   raises `InvalidRequestError` with `code` `cutoff_in_future`. Requests:
   1.
5. A client with the key `not-a-demo-key`: `client.series.list()` raises
   `AuthenticationError` with `code` `unauthenticated`. Requests: 1.
6. `client.changes.read("core-indicators", after=38)` raises
   `PositionAheadError` with `parameter` `after`. Requests: 1.

### Responses

#### `error-bodies-the-api-did-not-write`

Errors from something in front of the API, or from a later API version,
raise an exception that claims no more than the response says.

- **Proxy:** `GET /v1/series/activity-index`, request 1: `respond` 404,
  `text/html`, `<html><body>Not Found</body></html>`; request 2: `problem`
  400 `something_new`, with `parameter` `series_id`; request 3: `respond`
  403, `application/json`, the body of a `not_entitled` problem; request
  4: `respond` 200, `text/html`, `<html></html>`; request 5: `respond` 302,
  with `Location: http://example.invalid/`.

1. `client.series.get("activity-index")` raises an `APIError` whose class
   is exactly `APIError`, with `status` 404, and `code` and `problem`
   `None`. Requests: 1.
2. The same call raises exactly `APIError`, with `status` 400, `code`
   `something_new`, and `parameter` `series_id`. Requests: 1.
3. The same call raises exactly `APIError`, with `status` 403 and `code`
   `None`, because the response is not a problem response. Requests: 1.
4. The same call raises `UnexpectedResponseError` with `status` 200.
   Requests: 1.
5. The same call raises `UnexpectedResponseError` with `status` 302.
   Requests: 1.

#### `responses-are-validated`

A successful response that breaks its documented form is refused, naming
the member, and members the SDK does not know are ignored.

Each step adds one rule, `rewrite` on request 1 of `GET /v1/observations`,
and calls
`client.observations.page("activity-index", period_start="2026-08-01", period_end="2026-09-01")`,
whose unchanged answer is `rev_aug26_2`. Each step sends 1 request.

1. Add `"quality": "final"` to `data[0]` and to the page. The call returns
   a page whose record equals the unchanged one.
2. Remove `data[0].available_at`. The call raises
   `UnexpectedResponseError` naming `available_at` and index 0.
3. Set `data[0].value` to the JSON number `102.1`. The call raises
   `UnexpectedResponseError` naming `value`.
4. Set `data[0].available_at` to `"2026-09-10T12:30:40+00:00"`. The call
   raises `UnexpectedResponseError` naming `available_at`.
5. Set `data[0].sequence` to the JSON number `37.0`. The call raises
   `UnexpectedResponseError` naming `sequence`.
6. Set `data[0].change_type` to `null`. The call raises
   `UnexpectedResponseError` naming `change_type`.
7. Set `data[0].change_type` to `"future_type"`. The call returns the
   record with `change_type == "future_type"`, a `str` that is not a
   `ChangeType`.

#### `pages-must-share-a-snapshot`

`pages()` refuses a page that would mix two snapshots or lead back to a page
it has already read, instead of returning it.

1. Add `rewrite` on request 2 of `GET /v1/observations`, setting
   `position` to 38. `client.observations.pages("activity-index", page_size=10)`
   returns its first page, then raises `UnexpectedResponseError`.
   Requests: 2.
2. Clear the rules, and add `rewrite` on request 2 of
   `GET /v1/observations`, setting `next_page_token` to the `page_token`
   the request sent. The same iteration returns its first page, then
   raises `UnexpectedResponseError` instead of returning the second.
   Requests: 2.
3. Clear the rules, and add `rewrite` on request 3 of
   `GET /v1/observations`, setting `next_page_token` to the `page_token`
   request 2 sent, which leads back to the second page. The same iteration
   returns its first two pages, then raises `UnexpectedResponseError`
   instead of returning the third. Requests: 3.
4. Clear the rules, and add `rewrite` on request 1 of
   `GET /v1/datasets/core-indicators/changes`, setting `data` to `[]` and
   `next_position` to 0, and leaving `head_position` 37. The API's answer
   holds all 37 revisions with `next_position` 37, so the rewritten page is
   not caught up and makes no progress.
   `client.changes.pages("core-indicators", after=0)` raises
   `UnexpectedResponseError`. Requests: 1.

### Pagination and resuming

#### `iteration-is-lazy`

Iterators fetch a page only when the caller needs it, and the client
releases its connections when the block ends.

1. `it = client.observations.iterate("activity-index", page_size=10)`.
   Requests: 0.
2. Take 10 records from `it`. Requests: 1.
3. Take one more record. Requests: 1 more.
4. Call `it.close()`. Requests: none.
5. Leave the `with` block. Then `client.series.list()` raises
   `ClientClosedError`. Requests: none.

#### `snapshot-kept-across-a-revision`

`pages()` keeps one snapshot while a revision arrives, as the README's
demonstration requires.

- **Clock:** `2026-09-10T12:30:20Z`.

1. `pages = client.observations.pages("activity-index", page_size=10)`.
   Take three pages, each with `position` 36.
2. Set the clock to `2026-09-10T12:30:40Z`, when `rev_aug26_2` becomes
   available.
3. The next page has `position` 36 and two records, `rev_jul26_1` and
   `rev_aug26_1`, the latter with value `Decimal("102.4")`, and
   `next_page_token` `None`. The iterator then ends. Requests in the
   scenario: 4.
4. `client.observations.page("activity-index", period_start="2026-08-01", period_end="2026-09-01")`
   returns `rev_aug26_2` with `position` 37.

#### `resume-after-interrupted-download`

A download that fails on its fourth page resumes from its saved token with
no record missing or repeated, even though a revision arrived in between.
This is the README's completion criterion for resuming.

- **Clock:** `2026-09-10T12:30:20Z`.
- **Proxy:** `GET /v1/observations`, request 4 and every later one: `drop`.

1. Iterate `client.observations.pages("activity-index", page_size=10)`,
   saving each page's records and `next_page_token`. Three pages, 30
   records, are saved; then the iterator raises `TransportError` with
   `attempts` 3. Requests: 6.
2. Clear the rules. Set the clock to `2026-09-10T12:30:40Z`.
3. `client.observations.pages("activity-index", page_size=10, page_token=<the third page's token>)`
   returns one page with `position` 36, the records `rev_jul26_1` and
   `rev_aug26_1` (value `102.4`), and `next_page_token` `None`.
4. The 32 saved records have 32 distinct `observation_id` values, and,
   converted to JSON, they match the `expected` list of the `full-history`
   check at `2026-09-04T00:00:00Z`, in order. No revision became available
   between that cutoff and the scenario's clock.
5. `client.observations.pages("activity-index", page_token=<the same token>)`,
   without `page_size`, raises `PageTokenError` with `code`
   `page_token_mismatch` on its first page. Requests: 1.

#### `resume-after-snapshot-expiry`

An expired snapshot is reported as needing a restart, and the SDK does not
restart on its own (decision 7).

1. `first = client.observations.page("activity-index", page_size=10)`.
2. Set the clock to `2026-10-01T01:00:00Z`, the page's
   `snapshot_expires_at`.
3. `client.observations.pages("activity-index", page_size=10, page_token=first.next_page_token)`
   raises `PageTokenExpiredError` with `status` 410, `code`
   `page_token_expired`, `parameter` `page_token`, and `attempts` 1, on its
   first page. Requests: 1.
4. `client.observations.pages("activity-index", page_size=10)` returns four
   pages with `position` 37.

#### `access-revoked-during-iteration`

Revoking access mid-download ends the iterator with a refusal that differs
from an empty result. Entitlements are per dataset, so the scenario removes
the dataset ([open questions](client.md#open-questions)).

1. `client.observations.page("activity-index", period_start="2030-01-01", period_end="2030-02-01")`
   returns a page with empty `data` and `next_page_token` `None`, without
   raising. `iterate()` with the same arguments returns no records.
2. `it = client.observations.iterate("activity-index", page_size=10)`. Take
   10 records.
3. Set `cred_research`'s datasets to `[]`.
4. Taking the next record raises `NotEntitledError` with `status` 403,
   `code` `not_entitled`, and `attempts` 1, whose `path` includes
   `page_token=`.
5. Set `cred_research`'s datasets to `["core-indicators"]` and `active` to
   `false`.
6. Taking the first record of a new
   `client.observations.iterate("activity-index")` raises
   `AuthenticationError` with `code` `unauthenticated`.

#### `change-stream-until-caught-up`

`changes.pages()` reads until it catches up, and returns the positions the
API sends.

- **Clock:** `2025-07-03T12:31:10Z`.

1. `client.changes.pages("core-indicators", after=16, limit=2)` returns two
   pages: sequences 17 and 18, with `next_position` 18, `head_position` 20,
   and `caught_up` false; then sequences 19 and 20, with `next_position` 20
   and `caught_up` true. Requests: 2.
2. `client.changes.pages("core-indicators", after=20)` returns one page,
   with empty `data`, `next_position` 20, and `caught_up` true. Requests:
   1.

#### `change-stream-position-expired`

A position past retention is reported as needing a fresh load.

- **Clock:** `2027-02-02T12:31:10Z`.

1. `client.changes.pages("core-indicators", after=0)` raises
   `PositionExpiredError` with `status` 410, `code` `position_expired`,
   `parameter` `after`, and `attempts` 1, on its first page. Requests: 1.
2. `client.changes.read("core-indicators", after=1, limit=1)` returns
   sequence 2.

### Credentials and the HTTP client

#### `key-absent-from-errors-and-logs`

The key appears in no error, log record, or representation, while every
request still carries it.

- Logging: every record from `financial_data` at `DEBUG` and above is
  captured.

1. Set `cred_research`'s `active` to `false`. `client.series.list()` raises
   `AuthenticationError`. Reset the API.
2. Add `problem` 429 `rate_limited` with `Retry-After: 30` on every
   `GET /v1/series`. `client.series.list()` raises `RateLimitError`. Clear
   the rules.
3. Add `drop` on every `GET /v1/series`. `client.series.list()` raises
   `TransportError`. Clear the rules.
4. Add `stall` on every `GET /v1/series`.
   `client.with_options(timeout=0.5).series.list()` raises
   `DeadlineExceededError`. Clear the rules.
5. Add `respond` 200 `text/html` on every `GET /v1/series`.
   `client.series.list()` raises `UnexpectedResponseError`. Clear the
   rules.
6. Add `respond` 401 on every `GET /v1/series`, with
   `Content-Type: application/problem+json`,
   `Request-Id: demo-research-key`, and the body
   `{"status": 401, "code": "unauthenticated", "title": "Unauthenticated", "detail": "Bearer demo-research-key is not valid.", "parameter": null, "echo": {"headers": ["Authorization: Bearer demo-research-key"]}}`.
   `client.series.list()` raises `AuthenticationError` with `detail`
   `Bearer [redacted] is not valid.` and `request_id` `[redacted]`. Clear
   the rules.
7. For each exception raised above, `demo-research-key` does not occur in
   its `str`, its `repr`, the `repr` of its `args` and of each attribute
   the client contract lists, or `traceback.format_exception` of it.
   Following its `__cause__` and `__context__` to the end of the chain,
   every httpx exception that holds a request has
   `Authorization: Bearer [redacted]` on it. The chains of the
   `TransportError`, which follows three dropped connections, and of the
   `DeadlineExceededError` hold at least one such request.
8. `demo-research-key` occurs in no captured record's message, arguments,
   or attributes, and not in `repr(client)`. The captured records include
   two `INFO` retry records from step 3.
9. Every request the proxy received carried
   `Authorization: Bearer demo-research-key`.

#### `custom-http-client`

A caller-supplied `httpx.Client` carries the SDK's requests with the
caller's settings, and the caller keeps ownership of it (D1).

1. Create
   `http = httpx.Client(headers={"X-Customer": "acme"}, auth=("user", "pass"), event_hooks={"request": [hook]})`,
   where `hook` counts requests. In a `with Client(http_client=http, ...)`
   block, `client.series.list()` returns the series. The proxy received
   one request carrying `X-Customer: acme`,
   `Authorization: Bearer demo-research-key`, and a `User-Agent` beginning
   `financial-data-sdk-python/`. `hook` counted 1.
2. After the block, `http.is_closed` is false, and `http` can still send a
   request.
3. Close `http`. A new `Client(http_client=http, ...)` raises
   `ClientClosedError` from `client.series.list()`. Requests: none.

#### `request-id-from-any-response`

D7: the SDK reports a `Request-Id` from whatever answered, including a
proxy in front of the API.

- **Proxy:** `GET /v1/series/activity-index`, request 1: `respond` 503,
  `text/plain`, with `Request-Id: req_first`; request 2: `problem` 429
  `rate_limited`, with `Retry-After: 30` and `Request-Id: req_second`.

1. `client.series.get("activity-index")` raises `RateLimitError` with
   `request_id` `req_second` and `attempts` 2, and `str` of it contains
   `req_second`. The one `INFO` retry record names `req_first`.

#### `request-id-from-the-api`

D7, against contract 0.4.0 or later: errors carry the API's own request ID.

1. `client.series.get("no-such-series")` raises `NotFoundError`, whose
   `request_id` is non-empty and equals the `Request-Id` header of the
   response the proxy returned.

### Dataframes

#### `dataframe-keeps-revision-information`

The pandas conversion keeps every revision, every field, and exact values
(D6).

1. `revisions = [r for p in client.changes.pages("core-indicators", after=0) for r in p.data]`,
   which holds 37 revisions.
2. `df = financial_data.pandas.to_dataframe(revisions)` has 37 rows and the
   14 columns in the contract's order.
3. The `value` column has `object` dtype. Every non-null element is a
   `Decimal` equal to its record's value, and the column holds no `float`
   and no `NaN`.
4. The row for `rev_oct24_1` has `value` `None` and `missing_reason`
   `not_collected`. The row for `rev_may25_2` has `value` `None`,
   `missing_reason` `None`, and `change_type` `withdrawal`.
5. `available_at` is timezone-aware in UTC, and `sequence` is `int64`.
6. `to_dataframe([])` has 0 rows and the same 14 columns.

### Examples

These scenarios run the examples with the Python of a clean virtual
environment into which only the built wheel, with its `pandas` extra, is
installed, from a directory outside the source tree.
`FINANCIAL_DATA_BASE_URL` points at the proxy, and `FINANCIAL_DATA_API_KEY`
is `demo-research-key`. Each `sync` and `query` is a separate process, and
`--db` names a file in a fresh temporary directory.

#### `research-example`

The README's research workflow runs from the installed distribution.

1. `python examples/research.py` exits with status 0. Its output shows
   `rev_aug26_1` with `102.4` for the September 4 cutoff, `rev_aug26_2`
   with `102.1` without a cutoff, and a DataFrame of shape `(32, 14)`.

#### `scheduled-job-resumes`

The scheduled job checkpoints each page, resumes after a failure on its
fourth page without a missing or repeated record, follows the revision
that arrived meanwhile, and still reproduces the September 4 analysis
offline. This is the README's demonstration, steps 3 and 4.

- **Clock:** `2026-09-10T12:30:20Z`.
- **Proxy:** `GET /v1/observations`, request 4 and every later one: `drop`.

1. `sync --page-size 10` exits with status 1. Its output says 3 pages and
   30 records were saved and that the next run resumes from a page token,
   and names `TransportError`. The database holds 30 revisions, and
   `status` shows the stage `loading`.
2. Clear the rules. Set the clock to `2026-09-10T12:30:40Z`.
3. `sync --page-size 10` exits with status 0. The database holds 33
   revisions: the 32 the load selected, which are those of the
   `full-history` check at `2026-09-04T00:00:00Z`, and `rev_aug26_2`.
   `status` shows the stage `following`, position 37, `complete_from`
   `2026-09-03T12:31:10Z`, and `synced_at` `2026-09-10T12:30:40Z`.
4. `query --as-of 2026-09-04T00:00:00Z --period-start 2026-08-01 --period-end 2026-09-01`
   prints `rev_aug26_1` with `102.4`.
5. `query --as-of 2026-09-10T12:30:40Z --period-start 2026-08-01 --period-end 2026-09-01`
   prints `rev_aug26_2` with `102.1`.
6. `query --as-of 2026-09-04T00:00:00Z` prints 32 lines whose revision IDs
   and values match the `full-history` check at that cutoff, in order.
7. `query --as-of 2026-09-03T00:00:00Z` exits with a non-zero status and
   names the range it can answer.

#### `scheduled-job-follows-updates`

The job follows a withdrawal and a re-release through the change stream
and keeps every version, so an analysis made between them can be
reproduced. This is the README's demonstration, step 4.

- **Clock:** `2025-06-10T00:00:00Z`, after the May 2025 release and before
  its withdrawal.

1. `sync` exits with status 0 at position 17, with `complete_from`
   `2025-06-03T12:31:10Z`.
2. Set the clock to `2025-07-03T12:31:10Z`.
3. `sync` exits with status 0 at position 20, having saved `rev_may25_2`,
   `rev_jun25_1`, and `rev_may25_3`.
4. With `--period-start 2025-05-01 --period-end 2025-06-01`, `query`
   prints `rev_may25_1` with `100.6` at `--as-of 2025-06-10T00:00:00Z`,
   `rev_may25_2` as a `withdrawal` without a value at
   `--as-of 2025-06-21T00:00:00Z`, and `rev_may25_3` with `100.1` at
   `--as-of 2025-07-03T12:31:10Z`.

#### `scheduled-job-starts-empty`

A job first run before the series has any revision loads nothing. Every
revision then comes from the change stream, so the copy answers every
cutoff up to its last sync.

- **Clock:** `2024-01-01T00:00:00Z`, before the first revision.

1. `sync` exits with status 0. The database holds no revisions, and
   `status` shows the stage `following`, position 0, no `complete_from`,
   and `synced_at` `2024-01-01T00:00:00Z`.
2. Set the clock to `2024-02-03T12:31:10Z`, when `rev_jan24_1` becomes
   available.
3. `sync` exits with status 0 at position 1, having saved `rev_jan24_1`.
   `status` still shows no `complete_from`, and `synced_at`
   `2024-02-03T12:31:10Z`.
4. `query --as-of 2024-01-15T00:00:00Z` exits with status 0 and prints no
   lines.
5. `query --as-of 2024-02-03T12:31:10Z` prints `rev_jan24_1` with `97.1`.
6. `query --as-of 2024-03-01T00:00:00Z` exits with a non-zero status and
   names the range it can answer.

## Coverage

### The README's completion criteria

| Criterion | Checks and scenarios |
| --- | --- |
| The built package installs in a clean environment, and the documented workflow runs against the pinned demo API and passes the shared contract's checks | The stage 1 shared checks, run from the installed wheel by plan step 12's release workflow; `research-example`, `scheduled-job-resumes`, `scheduled-job-follows-updates`, `scheduled-job-starts-empty` |
| Authentication and rate-limit errors are reported distinctly | `access-control` (shared), `refusals-are-not-retried`, `access-revoked-during-iteration`, `throttled-on-every-attempt`, `retry-after-beyond-budget` |
| Retries stop within their configured bounds | `throttled-on-every-attempt`, `server-error-exhausts-attempts`, `connection-dropped-every-attempt`, `retry-after-beyond-budget`, `retry-after-beyond-deadline`, `deadline-during-request`, `deadline-bounds-backoff`; jitter in plan step 3's unit tests, since one run cannot show a random wait |
| An interrupted paginated download resumes with no missing or duplicate records | `resume-after-interrupted-download`, `scheduled-job-resumes` |
| The September 4 query still returns 102.4 after the revision is loaded | `august-2026-at-cutoffs` and `pagination` (shared), `snapshot-kept-across-a-revision`, `research-example`, `scheduled-job-resumes` |

### The README's demonstration and design choices

| Claim | Scenarios |
| --- | --- |
| Observation and revision identities, periods, availability times, decimal values, units, and missing values survive the client | Every query check (shared), through the [conversion](#converting-a-result); `responses-are-validated`, `dataframe-keeps-revision-information` |
| Denied access, invalid credentials, invalid input, throttling, and transient errors are distinguishable from one another and from an empty result | `refusals-are-not-retried`, `access-revoked-during-iteration`, `error-bodies-the-api-did-not-write`, `throttled-on-every-attempt`, `transient-errors-then-success` |
| Revoking access, forcing throttling beyond the retry budget, and a revision during a download (demonstration step 5) | `access-revoked-during-iteration`, `throttled-on-every-attempt`, `snapshot-kept-across-a-revision` |
| The caller controls concurrency and cleanup | `iteration-is-lazy` |
| Pages and tokens are available alongside a record iterator, and a job saves both in one transaction | `resume-after-interrupted-download`, `scheduled-job-resumes`; that a failure between a page's revisions and its checkpoint saves neither, in plan step 10's tests, since these scenarios fail only between transactions |
| Pagination stays on one snapshot; an expired snapshot is reported as requiring a restart | `snapshot-kept-across-a-revision`, `pages-must-share-a-snapshot`, `resume-after-snapshot-expiry` |
| `Retry-After` in both forms; the caller's deadline covers requests and waits; only safe reads are retried | `retry-after-seconds`, `retry-after-http-date`, `retry-after-invalid`, `deadline-during-request`, `deadline-bounds-backoff`; only `GET` exists at stage 1 |
| Errors carry the provider's request ID and omit the credential | `request-id-from-any-response`, `request-id-from-the-api`, `key-absent-from-errors-and-logs` |
| Customers can supply their own transport | `custom-http-client`; the SDK runner's recording client |
| Logging without global handlers | Plan step 3's unit tests; `key-absent-from-errors-and-logs` |
| Dataframe support is optional and keeps revision information | `dataframe-keeps-revision-information`; plan step 5's test without pandas |

## Decisions

These are proposed with this draft and take effect when the operator
approves it.

1. **The SDK scenarios are prose matched to tests by name, not a notation
   the harness parses.** There are 32, each a few calls against a real
   API. A parsed notation, like the Polymarket client's, would cost more
   than the drift it prevents. A reviewer compares each test with its
   scenario, and the harness checks that names and headings agree.
2. **The SDK runner turns retries off.** Each step then sends exactly one
   request, as the shared files assume. Retries are the scenarios' subject.
3. **The SDK runner records every request the SDK sends.** A step passes
   only if the SDK sent exactly the step's parameters, which shows that the
   SDK adds none and drops none (client decision 5).
4. **Steps the SDK has no faithful call for go over HTTP, and still run.**
   The shared files then reach the same verdict as the API runner, and the
   runner's report says how many steps went through the SDK.

## Failure reports

The runner and the scenario harness report, for each failure: the file or
scenario, the check name or step number, the SDK call made or the HTTP
request sent, and the expected and actual values at the first difference.
For an SDK exception they report its class and its `status`, `code`,
`parameter`, and `attempts`. No report contains the key.

[conformance-format]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/conformance.md
[stage-table]: https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#implementations-and-conformance
