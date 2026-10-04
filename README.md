# Financial data SDK for Python

A Python SDK demonstration for financial research: retrieve the data available
at a chosen cutoff, resume an interrupted download, and keep the versions an
earlier analysis used.

I'm [Nathan Slaughter](https://nathanslaughter.com/), a software engineer with a
background in investment research and portfolio management. I help data teams
design the client interfaces, examples, and release processes their customers
depend on. This project will make that approach inspectable through a small
research workflow.

**Status:** Project brief. This repository currently contains this README.
The SDK, tests, and runnable examples are planned; nothing described here has
been implemented or tested yet. The demo API and the shared data contract live
in [financial-data-api](https://github.com/nslaughter/financial-data-api). The dataset is synthetic, and this is a
demonstration project, not client work.

## What this project demonstrates

This is the supporting example for my SDK development and maintenance work.
A data provider should be able to install the package, complete a customer
workflow with it, and inspect how the client behaves when that workflow fails
partway through. It is meant to show:

- **A scope set by the customer's task.** One research workflow, run from the
  installed distribution in a clean environment, defines what the SDK covers.
- **Data whose meaning survives the client.** Records keep observation and
  revision identities, observation periods, availability times, decimal
  values, units, and missing values.
- **Downloads that can resume.** Pages and continuation tokens are available
  alongside a simple record iterator, so a scheduled job can checkpoint its
  work and resume without missing or duplicating records.
- **Failures a customer can act on.** Denied access, invalid credentials,
  invalid input, throttling, and transient errors are distinguishable from one
  another and from an empty result. Retries are bounded.
- **Examples that act as release checks.** CI builds the package, installs it
  outside the source tree, and runs the examples and the shared contract's
  checks on each supported Python version before a tagged release.

This SDK opens the first of four stages in a demonstration for financial data
providers. SDKs in [Go](https://github.com/nslaughter/financial-data-sdk-go)
and [TypeScript](https://github.com/nslaughter/financial-data-sdk-ts) follow
it, covering the same workflow and passing the same checks against the same
demo API. The [financial-data-api](https://github.com/nslaughter/financial-data-api) supplies that demo API from the start
and expands it in the second stage, the
[financial-data-api-monitor](https://github.com/nslaughter/financial-data-api-monitor)
checks what customers receive from it, and a final migration stage makes a
deliberate contract change to the SDKs and the API.

## A successful download should leave the researcher able to explain the result

The example follows a fictional monthly activity index. Its August 2026 value
is released as 102.4 on September 3, then revised to 102.1 on September 10.
A researcher reproducing an analysis made on September 4 still needs 102.4.
The client's types and query interface need to preserve that distinction all
the way into the researcher's local data.

The same workflow becomes an operational problem when a scheduled download
fails on its fourth page. The customer needs to know which records were saved
and where to resume. Page boundaries, checkpoints, and useful errors therefore
belong in the SDK's design and examples.

## How the first demonstration will work

1. Install the built Python distribution in a clean environment and start the
   demo API from the container image that financial-data-api publishes, at a
   pinned version. The demonstration needs no external data credentials.
2. Retrieve the observations available at a chosen cutoff, retaining their
   observation and revision identities.
3. Interrupt a paginated download, resume from its saved checkpoint, and
   compare the local records with the expected results in the shared data
   contract, which were prepared before any client code.
4. Follow subsequent releases, revisions, and withdrawals through an update
   cursor while retaining the versions needed to reproduce the earlier
   analysis.
5. Revoke the credential's access to the series, force throttling beyond the
   retry budget, and introduce a revision during a download.

The intended query interface looks like this. It is a sketch; no package has
been published.

```python
import os

with Client(api_key=os.environ["FINANCIAL_DATA_API_KEY"]) as client:
    observations = client.observations.iterate(
        series_id="activity-index",
        period_start="2026-08-01",
        period_end="2026-09-01",
        available_as_of="2026-09-04T00:00:00Z",
    )
    for observation in observations:
        print(observation.period_start, observation.value, observation.revision_id)
```

The period selects August, with September 1 excluded. `available_as_of` asks
which version the provider could deliver by the cutoff, so the query returns
102.4 even after the revision has been published.

## Design choices the examples will make visible

- **The caller controls concurrency and cleanup.** Calls are synchronous, and
  the iterator fetches a page only when the caller needs the next record.
  There is no background prefetching. The client is a context manager, so
  connections close when a loop fails or stops early.
- **Convenience keeps the underlying controls.** A notebook can iterate over
  records. A scheduled job can take each page with its continuation token and
  save both in one local transaction, so a crash leaves a usable checkpoint.
- **Pagination stays on one snapshot.** Later pages read from the snapshot
  selected by the first request, so a revision arriving mid-download cannot
  change them. An expired snapshot is reported as requiring a restart.
- **Retries are bounded and safe to repeat.** The client uses a finite attempt
  limit, a total retry budget, backoff with jitter, and `Retry-After` in both
  its date and delay forms. The caller's deadline covers requests and retry
  waits. Only reads that are safe to repeat are retried.
- **Errors are traceable and omit secrets.** Errors include the provider's
  request ID. Neither errors nor logs contain the credential.
- **The client fits the application around it.** Customers can supply their
  own transport for connection requirements or test fixtures. Logging uses the
  standard library without installing global handlers. Dataframe support is an
  optional dependency, and any conversion that drops revision information is
  left to the customer's analysis.

## Scope of the first release

The first release covers one synthetic dataset and the research workflow above:
authentication, typed records, pagination, errors, bounded retries, resumable
downloads, and update following. At this stage the demo API implements only
what that workflow needs.

The expanded API, bulk exports, and hosted deployment are outside this release.
Python suits the research example; the Go and TypeScript SDKs cover the same
workflow in their own repositories.

## The demonstration is complete when

- The built package installs in a clean environment and the documented
  workflow runs against the pinned demo API, passing the shared contract's
  checks for this stage.
- Authentication and rate-limit errors are reported distinctly, and retries
  stop within their configured bounds.
- An interrupted paginated download resumes with no missing or duplicate
  records.
- The September 4 query still returns 102.4 after the revision is loaded.

## What the repository will contain

- A customer-focused README and quickstart.
- A runnable research example or notebook, and a scheduled-job example that
  checkpoints and resumes.
- Type documentation mapped to the shared data contract.
- CI that builds the distribution, installs it, and runs the examples and the
  contract's checks against the pinned demo API image on each supported Python
  version.
- A tagged release with an installable distribution.
- Documented limitations and a clear demonstration label.

## A later contract change will test the maintenance work

The fourth stage adds a deliberate change to this SDK and the API: replacing
an ambiguous `date` field with explicit publication, observation period, and
availability fields. The SDK will gain tagged old and new releases, an
explicit mode for the old API behavior, a compatibility matrix with CI
results, migration examples that include data the customer has already stored,
release notes, and support and deprecation guidance. Unsupported SDK and API
combinations will fail with a clear error instead of silently returning a
different data model.

## Related projects and writing

- [financial-data-sdk-go](https://github.com/nslaughter/financial-data-sdk-go)
  and [financial-data-sdk-ts](https://github.com/nslaughter/financial-data-sdk-ts):
  the same client in Go and TypeScript.
- [financial-data-api](https://github.com/nslaughter/financial-data-api): the demo API, the shared data contract, and the
  full API this SDK will act as a customer of.
- [financial-data-api-monitor](https://github.com/nslaughter/financial-data-api-monitor):
  scheduled customer-level checks against that API.
- *Building an SDK your customers love*: an article on the design behind this
  project, in preparation. I'll link it here when it is published.
- *Shipping an API change your customers can adopt confidently*: an article
  on the migration stage, also in preparation.

## Work with me on an SDK your customers can use

I take on SDK projects scoped around the workflows your customers need to
complete. The work can include interface design, implementation, documentation,
release packaging, compatibility checks, and ongoing maintenance.

[Discuss an SDK project](https://www.linkedin.com/in/nathan-slaughter) with the
API, target language, and customer workflow you need to support.
