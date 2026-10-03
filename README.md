# Financial data SDK

A Python SDK demonstration for financial research: retrieve a dataset, resume
an interrupted download, and preserve the versions used in an earlier analysis.

I'm [Nathan Slaughter](https://nathanslaughter.com/), a software engineer with a
background in investment research and portfolio management. I help data teams
design the client interfaces, examples, and release processes their customers
depend on. This project will make that approach inspectable through a small
research workflow.

**Status:** Project brief. This repository currently contains this README.
The SDK, demonstration server, tests, and runnable examples are planned.

## A successful download should leave the researcher able to explain the result

The proposed example follows a fictional monthly economic series. Its August
value is released as 102.4 in September, then revised to 102.1. A researcher
reproducing an analysis from before the revision still needs the earlier value.
The client's types and query interface need to preserve that distinction all
the way into the researcher's local data.

The same workflow becomes an operational problem when a download fails after
several pages. The customer needs to know which records were saved and where
to resume. Page boundaries, checkpoints, and useful errors therefore belong
in the SDK's design and examples.

## How the first demonstration will work

1. Install the built Python distribution in a clean environment and connect
   to a small fixture-backed API included with the initial demonstration.
2. Retrieve the observations available at a chosen cutoff, retaining their
   observation and revision identities.
3. Interrupt a paginated download, resume from its saved progress, and compare
   the local records with expectations prepared separately from the client.
4. Follow subsequent updates while retaining the versions needed to reproduce
   the earlier analysis.

The planned [financial-data-api](https://github.com/nslaughter/financial-data-api)
will extend the server side of this workflow. The SDK examples will also
exercise authentication failures, throttling, bounded retries, and access
changes as the implementation develops.

## The installed examples will become compatibility checks

The first release is intended to include an installable package, a quickstart,
documented types, and examples for notebook and scheduled-job use. Running those
examples from the distribution will check the packaging as well as the client
behavior. Later API and SDK changes can be judged against the same customer
workflow, with migration examples when the behavior changes.

## Work with me on an SDK your customers can use

I take on SDK projects scoped around the workflows your customers need to
complete. The work can include interface design, implementation, documentation,
release packaging, compatibility checks, and ongoing maintenance.

[Discuss an SDK project](https://nathanslaughter.com/) with the API,
target language, and customer workflow you need to support.
