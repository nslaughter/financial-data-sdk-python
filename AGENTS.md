# Working in this repository

This repository holds a Python SDK for the demo financial data API, the
specification it implements, and a pinned copy of the API's shared
contract. The specifications come first; the code implements them.

## Read these first

| Document | Governs |
| --- | --- |
| [`spec/client.md`](spec/client.md) | What the SDK does: its interface, arguments, records, response validation, pagination and resuming, errors, retries and deadlines, credentials and logging, the HTTP client, dataframes, the examples, packaging, decisions, and open questions. |
| [`spec/conformance.md`](spec/conformance.md) | How the shared expected results run through the SDK, the fault proxy, and the SDK scenarios with the results a correct SDK produces. |
| [`docs/implementation-plan.md`](docs/implementation-plan.md) | The order of pull requests and what each must deliver. |
| `contract/` and the API's documents at the tag in `contract/CONTRACT.json` | What the API does and what its records mean. The SDK's specification cites them and does not restate them. |

The README describes the project for people; it is not a specification.

## Rules

- **Do not change `spec/` in an implementation pull request.** If the code
  disagrees with the specification or a scenario, investigate the code
  first. If you conclude that a specification is wrong, ambiguous, or
  contradicts itself or the API's documents, stop and report it, quoting
  the passages, instead of choosing an interpretation or editing an
  expectation to match the code.
- **Do not change `contract/`,** except in a step whose scope says so
  (steps 1, 8, and 11). It is a byte-for-byte copy of financial-data-api at
  the tag `contract/CONTRACT.json` names, and `scripts/check_contract.py`
  must pass. If the API behaves differently from its own specification,
  report it; do not work around it in the SDK or the runner.
- **Follow the plan.** Do one pull request from the implementation plan at
  a time, in order, within its stated scope, as described in
  [Doing the next item](#doing-the-next-item). Note anything you deferred
  in the pull request description.
- **Follow the owner specifications.** D1 to D8 in `spec/client.md` are
  owner specifications, decided by the operator on 2026-10-05. Implement
  them as written, and don't reopen one or depart from it on your own; if
  one seems wrong, stop and report it, as for any specification. Do not
  start a step whose status is `Needs operator decision` or
  `Waiting on the API`.
- **Done means verified.** A pull request is done when formatting, lint,
  type checks, and unit tests pass, and, from step 8 on, every shared check
  and every scenario in `tests/scenarios/enabled.txt`, including the ones
  the step adds, passes against the pinned API image. Report results as
  they are; never skip, relax, or mark as expected to fail a check or a
  scenario to make it pass. A scenario is turned on only by listing it in
  `enabled.txt`.
- **Nothing but localhost in tests.** Unit tests use `httpx.MockTransport`
  or a local stub. The shared checks and scenarios use the pinned image and
  the fault proxy on localhost. Only `scripts/check_contract.py` fetches
  from GitHub. Test code never pulls the image: CI pulls it, and to verify
  a step locally you start it with the `docker run` in
  [Commands](#commands), which pulls it if needed.
- **Nothing outside this repository.** Do not open issues, post comments,
  or push to financial-data-api or any other project unless the operator
  asks. A change the SDK needs from the API, such as D7's `Request-Id`
  header, is reported to the operator.

## Implementation guidance

- Synchronous only. httpx is the only runtime dependency (D1). No
  Pydantic: records are frozen dataclasses with slots, decoded by
  `_decode.py`.
- A `value` never passes through `float`. Check that the JSON member is a
  string matching the contract's pattern, then build `Decimal(text)`.
  Convert back with `format(value, "f")`, never `str(value)`, which writes
  `0.0000001` as `1E-7`.
- Parse dates and timestamps with strict patterns, then construct them, so
  impossible dates fail. Write them with explicit zero padding, not
  `strftime("%Y")`, whose padding of years below 1000 varies by platform.
- `datetime` is a subclass of `date`, and `bool` of `int`. Check for the
  subclass first wherever the contract refuses it, in arguments and in
  decoded JSON.
- Keep `_params.py`, `_decode.py`, `_errors.py`, and `_retry.py` free of
  I/O, clocks, and randomness. Pass the clock, the sleep, and the random
  source in, so their rules can be tested directly and deterministically.
- Deadlines and waits use `time.monotonic()`. Only a `Retry-After` date is
  compared with `datetime.now(UTC)`.
- Never format a request's headers into a message or a log record. Before
  raising, redact the `Authorization` header on the request of every httpx
  exception in the `__cause__` and `__context__` chain, not only the direct
  cause. Replace the key with `[redacted]` in any text taken from a
  response, such as a problem body or `Request-Id`, before an exception or
  a log record holds it. Tests search every rendering of an exception and
  every log record for the key itself.
- Logging uses `logging.getLogger("financial_data")` with `%`-style
  arguments, at `DEBUG` and `INFO` only, with no handlers.
- Iterators are generators that hold no open response: read each response
  in full before yielding from it.
- A caller-supplied `httpx.Client` is never closed by the SDK. Check
  `is_closed` before each request, and pass the SDK's own `auth` so that
  the client's `auth` cannot replace the bearer key.
- The conversion of SDK results to JSON belongs to `tests/support/`, not
  to the package.
- The examples import only public names, and run from the installed
  wheel.

## Commands

Step 1 sets these up; until then they do not run. The commands that need
the API work from step 8, once `contract/CONTRACT.json` names the image.

| Task | Command |
| --- | --- |
| Install the development environment | `uv sync --all-extras` |
| Format check | `uv run ruff format --check .` |
| Lint | `uv run ruff check .` |
| Type check | `uv run mypy` |
| Unit tests | `uv run pytest` |
| Check the vendored contract | `uv run python scripts/check_contract.py` |
| Start the pinned API | `docker run --rm -p 8080:8080 -e TEST_CONTROL=enabled "$(jq -r .api_image contract/CONTRACT.json)"` |
| Shared checks | `uv run pytest tests/conformance --base-url http://localhost:8080 --stage 1` |
| SDK scenarios | `uv run pytest tests/scenarios --base-url http://localhost:8080` |
| One scenario | `uv run pytest tests/scenarios --base-url http://localhost:8080 -k <test name>` |
| Build | `uv build` |

## Doing the next item

When asked to do the next item:

1. Update `main` (`git checkout main && git pull --ff-only`) and read the
   **Progress** table in
   [`docs/implementation-plan.md`](docs/implementation-plan.md). The next
   item is the first step whose status is not `Done`.
2. Stop and report instead of starting if any of these holds:
   - `gh pr list --state open` shows a pull request for that step; report
     its state, because the operator reviews and merges it;
   - the step's status is `Needs operator decision`; name the decision;
   - the step's status is `Waiting on the API`; name what it waits for, as
     the plan lists it, and say whether financial-data-api appears to
     provide it yet. The operator changes the status.
3. Create a branch named `step-<N>-<short-slug>`, such as
   `step-2-decode-responses`, and implement the step within its scope.
4. Run every check the step lists. If a check fails because a
   specification seems wrong or ambiguous, stop and report it as the
   [Rules](#rules) require; do not open a pull request built on a guess.
5. In the same branch, set the step's status to `Done` in the Progress
   table.
6. Push, and open a pull request as described below. Then add its number
   to the step's row in a follow-up commit on the same branch.
7. Do not merge. Report the pull request's link, the checks you ran and
   their results, and anything deferred.

## Commits and pull requests

Write commit subjects as short imperative sentences in sentence case, such
as "Decode revision records exactly", with a body that says what changed
and why. Commit each logical change separately.

A pull request's description names its step in the implementation plan,
lists the checks and scenarios it turns on and the checks run with their
results, and lists anything deferred and any specification question
raised.
