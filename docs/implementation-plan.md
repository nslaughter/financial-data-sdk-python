# Implementation plan

This plan divides the SDK's implementation into pull requests that can be
reviewed one at a time. Each names what it builds, what it leaves out, and
the checks that prove it done. The specifications are
[`spec/client.md`](../spec/client.md) and
[`spec/conformance.md`](../spec/conformance.md), at draft 0.1.0, built on
the API's contract version 0.3.0. Read [`AGENTS.md`](../AGENTS.md) before
starting any of them.

Steps 1 to 7 build the SDK, its test harnesses, and its type documentation
without the API: they test against `httpx.MockTransport`, a local stub, and
the vendored contract. Steps 8 to 11 run the SDK against the pinned API
image and wait for financial-data-api to publish it. Step 12 releases.

## Progress

Each step's pull request changes its own row: it sets **Status** to `Done`,
and after the pull request is opened, a follow-up commit on the same branch
fills in **Pull request**. A row reads `Done` on `main` only once its pull
request is merged.

A step whose status is `Needs operator decision` cannot start until the
operator records the decision here. A step whose status is
`Waiting on the API` cannot start until what it waits for exists and the
operator changes its status to `Not started`. The contract's design
decisions, D1 to D8, are all
[owner specifications](../spec/client.md#owner-specifications), decided on
2026-10-05; the **Owner specifications** column names those each step
follows.

| Step | Owner specifications | Status | Pull request |
| --- | --- | --- | --- |
| 1. Create the package, records, errors, and configuration | D1, D4, D5 | Not started | |
| 2. Decode responses and format arguments | D1 | Not started | |
| 3. Send requests with retries and deadlines | D1, D7 | Needs operator decision | |
| 4. Query the catalog, observations, and the change stream | | Not started | |
| 5. Convert records to pandas | D6 | Not started | |
| 6. Build the SDK runner and the fault proxy | D3, D5 | Not started | |
| 7. Document the types against the contract | | Not started | |
| 8. Pass the stage 1 shared checks against the API image | D5 | Waiting on the API | |
| 9. Run the SDK scenarios | D3 | Waiting on the API | |
| 10. Add the research and scheduled-job examples | D2, D6 | Waiting on the API | |
| 11. Check request IDs against contract 0.4.0 | D7 | Waiting on the API | |
| 12. Release a tagged distribution | D8 | Not started | |

What each step that cannot start yet needs:

- **Step 3** needs the operator's choice of how the deadline bounds a
  response that arrives slowly, an
  [open question](../spec/client.md#open-questions) in the client contract.
- **Steps 8 to 10** need the stage 1 image, which is the API's plan
  [step 7](https://github.com/nslaughter/financial-data-api/blob/main/docs/implementation-plan.md#7-publish-the-demo-api-image).
  That step itself waits on the operator's choice of image name and tag
  scheme.
- **Step 11** needs contract version 0.4.0 with the `Request-Id` header
  (D7), tagged in financial-data-api, and an image that implements it.

If the operator changes an owner specification, the scenarios that name it
may need to change. That change is a new version of the specification, made
before the step that depends on it, never inside an implementation pull
request.

## How checks are turned on

- **Shared checks.** `tests/conformance` runs every file the stage it is
  given requires, from `contract/expected/`. Step 8 runs it at stage 1, and
  every later step keeps it passing.
- **SDK scenarios.** The harness runs the scenarios listed in
  `tests/scenarios/enabled.txt`, one name per line. Each step adds the
  names it must pass. A scenario that is not listed has not been turned on
  yet; no other way of skipping or relaxing one is allowed. From step 11
  on, every scenario in `spec/conformance.md` is listed, and the harness
  checks that.

## Package layout

A pull request may refine this layout if it explains why.

| Path | Contents |
| --- | --- |
| `src/financial_data/__init__.py` | The public names, `__all__`, and `__version__`, and nothing else. |
| `_records.py` | Record dataclasses and the enums. |
| `_errors.py` | The exceptions, and choosing one from a status, headers, and body. Pure. |
| `_config.py` | `RetryPolicy`, constructor validation, and the environment variables. |
| `_params.py` | Checking arguments and formatting them into paths and query parameters. Pure. |
| `_decode.py` | Validating responses and decoding them into records. Pure. |
| `_retry.py` | Retry decisions, backoff, and `Retry-After`. Pure, with the clock and randomness passed in. |
| `_transport.py` | One call over httpx: attempts, waits, the deadline, redaction, request IDs, and logging. |
| `_client.py` | `Client`, `with_options`, the resources, and the iterators. |
| `pandas.py` | `to_dataframe` (step 5). |
| `tests/unit/` | Unit tests: the default `pytest` run. No network. |
| `tests/support/` | Helpers shared by the test suites, including the conversion of SDK results to JSON. |
| `tests/conformance/` | The SDK runner (step 6). |
| `tests/faults/` | The fault proxy (step 6). |
| `tests/scenarios/` | The scenario tests and `enabled.txt` (steps 6 and 9). |
| `contract/` | The vendored contract: `fixtures/`, `expected/`, and `CONTRACT.json` (D5). |
| `scripts/check_contract.py` | Compares `contract/` with its tag. |
| `examples/` | The research and scheduled-job examples (step 10). |
| `docs/types.md` | The type documentation (step 7). |

## Steps

### 1. Create the package, records, errors, and configuration

Follows D4: the distribution `financial-data-sdk`, imported as
`financial_data`, for CPython 3.11 and later. Follows D1 for the dependency
and the types, and D5 for the contract.

- A `pyproject.toml` with a `src/` layout and `py.typed`. httpx is the only
  runtime dependency, from 0.27 and below its next major version; record
  the exact range. The `pandas` extra requires pandas 2.2 or later.
- Development tools: `ruff` for formatting and linting, `mypy` in strict
  mode, `pytest`, and `uv` with a committed lock file. Commands as in
  [`AGENTS.md`](../AGENTS.md#commands).
- A CI workflow that, on every Python version from 3.11 to the newest
  stable release:
  - checks formatting, lints, type-checks, and runs the unit tests;
  - builds the wheel and the source distribution;
  - installs the wheel in a clean virtual environment and imports the
    package there, from a directory outside the source tree.
  One job also runs the unit tests with the lowest httpx the range allows.
- Vendor the contract:
  - copy `fixtures/` and `expected/` from `contract-v0.3.0` of
    financial-data-api into `contract/`, byte for byte;
  - write `contract/CONTRACT.json` with the repository, the tag, the commit
    `ecd6f685b0acb69f5eba1a03041441497a039eed`, and `"api_image": null`;
  - write `scripts/check_contract.py`, which fetches the tag, checks that
    it points to the commit, and fails on any file that differs, is
    missing, or is extra. CI runs it.
- Every record, enum, exception, and `RetryPolicy` the contract names, with
  their fields, types, and base classes. Records are frozen dataclasses
  with slots.
- `Client`'s constructor and `with_options`, validating every setting in
  the contract's [Configuration](../spec/client.md#configuration) table,
  reading the two environment variables (an empty value counts as unset),
  and giving a `repr` without the key.
- Tests:
  - each invalid setting raises `ConfigError`, whose message does not
    contain a key it was given, including keys with a space, a control
    character, and a non-ASCII character such as `“demo-research-key”`;
  - records are frozen, and `Revision`'s fields are in the order of the
    keys of the first record in `contract/fixtures/revisions.json`;
  - every public name is importable from the top level and listed in
    `__all__`.

Out of scope: any request. `Client`'s other members may be stubs that
raise `NotImplementedError`.

### 2. Decode responses and format arguments

- In `_decode.py`, validate and decode every stage 1 response, as
  [Validating responses](../spec/client.md#validating-responses) says,
  including unknown members, unknown `change_type` and `missing_reason`
  values, and an `UnexpectedResponseError` that names the member and the
  index.
- In `_params.py`, check and format arguments, as
  [Arguments](../spec/client.md#arguments) says: the `TypeError` and
  `ValueError` cases, conversion to UTC, truncation to whole seconds, a
  `date` that is not a `datetime`, `bool` refused, path segments
  percent-encoded with no safe characters, and `None` left out.
- In `_errors.py`, recognize a problem response, choose the exception by
  code and then by status, and fill its attributes and its `str`.
- In `tests/support/`, the conversion of SDK results to JSON that
  [the SDK runner](../spec/conformance.md#converting-a-result) uses.
- Tests:
  - every record in `contract/fixtures/revisions.json` decodes, and
    converts back to exactly its JSON;
  - each validation failure is refused and named: a missing member, a
    wrong JSON type, `null` where it is not allowed, a value that is not a
    string, a fractional or boolean integer, and dates and timestamps in
    every malformed form the API's `request-errors` file uses;
  - `""`, `"."`, and `".."` refused as a `series_id` or `dataset_id` with
    `ValueError`, and a path ID with `/` percent-encoded;
  - values such as `102.0`, `0.0000001`, `-0`, and
    `12345678901234567890.123` keep their text through
    `format(value, "f")`;
  - every row of the exception table, the fallback by status, and a
    response that is not a problem response.

Out of scope: HTTP.

### 3. Send requests with retries and deadlines

Follows D1 for the HTTP client and D7 for reading `Request-Id`. Before this
step starts, the operator decides how the deadline bounds a response whose
headers or body arrive slowly ([Deadline](../spec/client.md#deadline)),
records it in the client contract with the scenarios that show it, and
changes the step's status to `Not started`.

- In `_retry.py`:
  - the retry decision for every outcome in
    [What is retried](../spec/client.md#what-is-retried);
  - backoff, with and without full jitter;
  - `Retry-After` as delay-seconds and as an HTTP date in each of the
    three forms HTTP allows, with a date in the past counting as no wait
    and an invalid value ignored;
  - the three bounds.
- In `_transport.py`, one call:
  - each attempt runs with an httpx timeout equal to the time remaining;
  - `DeadlineExceededError` and `TransportError`, with `Authorization`
    redacted on the request of every httpx exception in the raised
    exception's `__cause__` and `__context__` chain;
  - `attempts` and `request_id` on every exception that has them;
  - the key replaced with `[redacted]` in any text taken from a response,
    before an exception or a log record holds it;
  - the three request headers;
  - no redirects on the SDK's own client;
  - a supplied client's settings, `auth` replaced, never closed, and
    `ClientClosedError` when it is closed;
  - logging, as
    [Credentials and logging](../spec/client.md#credentials-and-logging)
    says.
- Tests use `httpx.MockTransport`, with the clock, the sleep, and the
  random source passed in:
  - each row of the retry table, and each bound;
  - a `Retry-After` beyond the budget on a `429` and on a `503`, which
    raise `RateLimitError` and `ServerError`, each with `retry_after` set;
  - jitter stays within its bounds and uses the random source;
  - `Retry-After` in each form;
  - the deadline passing during a request, and before a wait;
  - a caller-supplied client's own timeout, with `timeout=None`, retried
    and then raised as `TransportError`;
  - the key absent from every exception, as rendered by `str`, `repr`, and
    `traceback.format_exception`, from the request of every exception in
    its `__cause__` and `__context__` chain, including those of earlier
    failed attempts, and from every log record;
  - a response that echoes the key, in a problem member at the top level
    and nested, and in `Request-Id`, leaves only `[redacted]` in the
    exception's attributes, its `str`, and the log records;
  - the logger has no handlers and logs nothing at `WARNING` or above;
  - a supplied client's headers and `auth`, and that it is left open.

Out of scope: the endpoints.

### 4. Query the catalog, observations, and the change stream

- In `_client.py`:
  - `Client`'s context manager and `close()`;
  - `with_options`, sharing the pool without owning it;
  - every method in the
    [Public interface](../spec/client.md#public-interface) table.
- Iterators fetch lazily and keep the pagination guarantees in
  [Pagination](../spec/client.md#pagination):
  - one snapshot position per query, and no token sent twice, whether a
    page returns its own token or a cycle passes through several pages;
  - the change stream's progress, stopping once caught up and returning at
    least one page.
- Tests use `httpx.MockTransport` with responses built from the vendored
  fixtures:
  - request counts as iterators advance, and after an early `break`;
  - resuming with `page_token` repeats every argument;
  - each pagination guarantee raises;
  - an empty result raises nothing, and neither does an empty caught-up
    change page, whose `next_position` is the position it read from;
  - `ClientClosedError` after `close()`, through a derived client, and
    through a supplied client the caller closed.

Out of scope: running against the API.

### 5. Convert records to pandas

Follows D6.

- `financial_data/pandas.py`, as
  [Dataframes](../spec/client.md#dataframes-d6) says.
- CI tests the extra on every supported Python version, with the lowest
  and the latest pandas that install there. A job without pandas checks
  that importing `financial_data.pandas` raises `ImportError` naming the
  extra, and, in a fresh interpreter, that `import financial_data` does
  not import pandas.
- Tests: every fixture revision converts, with the column order, dtypes,
  `Decimal` and `None` values, and empty input the contract gives.

### 6. Build the SDK runner and the fault proxy

Follows D3 for the proxy and D5 for reading the vendored contract.

- In `tests/conformance`, implement
  [The SDK runner](../spec/conformance.md#the-sdk-runner) completely:
  - reading `contract/expected/`, choosing files by stage, and honoring
    `stages`;
  - the reset before every check;
  - the rule for which steps go through the SDK, the recording client and
    its exact-parameters check, and the conversion;
  - matching and references;
  - query, position, read, and apply checks;
  - failure reports, and a count per file of the steps sent through the
    SDK and over HTTP.
  It takes `--base-url` and `--stage`, both required.
- In `tests/faults`, implement
  [The fault proxy](../spec/conformance.md#the-fault-proxy): rules, the
  five actions, the records, and the `Retry-After` date helper.
- In `tests/scenarios`, the harness: the profile; `enabled.txt`, checked
  against the headings under **Scenarios** in `spec/conformance.md` and
  against the tests; and the proxy started in front of `--base-url`.
- Tests, without the API:
  - the routing rule classifies every request step of the stage 1 files as
    the conformance document describes. For example, the malformed
    timestamps in `request-errors` go through the SDK, while `page_size`
    `010` goes over HTTP;
  - matching and references, against a fake API built on
    `httpx.MockTransport`;
  - each proxy action, against a local stub upstream.

Out of scope: running against the real API.

### 7. Document the types against the contract

- `docs/types.md` maps each record and field to its contract field and
  meaning, each exception to the API's status and codes, and positions and
  page tokens to the API's rules, linking the documents at the pinned tag.
  It says what the SDK checks and what it leaves to the API, and lists the
  limitations: stage 1 only; leading zeros in values; the range a local
  copy can reproduce; and retry defaults that rest on no measurement.
- A unit test fails if a name in `__all__` is missing from
  `docs/types.md`.

### 8. Pass the stage 1 shared checks against the API image

Before this step starts, financial-data-api publishes its stage 1 image
(its plan step 7), the operator records the image and tag here, and the
step's status changes to `Not started`.

- Set `api_image` in `contract/CONTRACT.json`. This step may change
  `contract/` only there.
- Add a CI job that, on every supported Python version:
  - starts the image with `TEST_CONTROL=enabled`;
  - installs the built wheel in a clean virtual environment;
  - runs `tests/conformance` with `--stage 1`, from outside the source
    tree.
- Done when every stage 1 file passes. The pull request reports the
  per-file counts of steps sent through the SDK and over HTTP.

### 9. Run the SDK scenarios

Waits for the same image as step 8.

- Add a CI job that starts the image and runs `tests/scenarios` from the
  installed wheel on every supported Python version.
- `enabled.txt` lists every scenario except those under **Examples** and
  `request-id-from-the-api`.
- Done when they pass.

### 10. Add the research and scheduled-job examples

Follows D2 and D6. Waits for the same image as step 8.

- `examples/research.py` and `examples/scheduled_job.py`, as
  [Examples](../spec/client.md#examples-d2) says. They use only public
  names.
- Tests without the API: load every fixture revision into the
  scheduled-job example's store, and run each query check in
  `contract/expected/` against its offline selection. Each result must
  match. Test `complete_from`, including the null one of a load that saved
  nothing, and the refusal of cutoffs outside the range.
- Turn on `research-example`, `scheduled-job-resumes`,
  `scheduled-job-follows-updates`, and `scheduled-job-starts-empty`. CI
  runs them from a clean virtual environment with the wheel and its
  `pandas` extra.

### 11. Check request IDs against contract 0.4.0

Follows D7. Before this step starts, financial-data-api tags
`contract-v0.4.0` with the `Request-Id` header and publishes an image that
implements it, and the operator records both here. If 0.4.0 names the
header differently, the client contract changes first, in a new version.

- Replace `contract/` with the files of `contract-v0.4.0`, and update
  `CONTRACT.json`, including the image. This step may change `contract/`.
- Run the stage 1 shared checks against the new image. If 0.4.0 changes
  anything beyond the header that the SDK depends on, stop and report it.
- Turn on `request-id-from-the-api`. Done when every shared check and every
  scenario passes, and `enabled.txt` lists them all.

### 12. Release a tagged distribution

Follows D8.

- A release workflow, triggered by a `v<version>` tag, that on every
  supported Python version builds the distribution, installs the wheel in
  a clean virtual environment, and runs the unit tests, the shared checks,
  the scenarios, and the examples against the pinned image. It then
  creates a GitHub release with the wheel and the source distribution
  attached. The repository owner pushes the tag; the pull request adds the
  workflow only.
- Update the README's status, add a quickstart that installs from the
  release URL and starts the pinned image, and link `docs/types.md`. The README's stage 4 section names the change only
  once the operator settles that open question.
- Done when the workflow passes on a pull request, without creating a
  release.

## After the first release

- Stage 2 features (`published_as_of`, the revision history, the release
  calendar, and exports) each need a new version of the client contract
  before any work starts. They also need the contract's stage 2 expected
  files, which the runner turns on with `--stage 2`.
- The migration stage's breaking change is an open question in the
  [data contract](https://github.com/nslaughter/financial-data-api/blob/contract-v0.3.0/spec/data-contract.md#open-questions).
  It needs a decision and new contract versions, in the API and here,
  before any work starts.
