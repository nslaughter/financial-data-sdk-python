"""The SDK runner as a pytest suite: one test per check of the stage given.

Run it against the pinned API image, started with `TEST_CONTROL=enabled`:

    uv run pytest tests/conformance --base-url http://localhost:8080 --stage 1

Both options are required. The summary at the end gives, for each file,
how many checks ran and how many request steps went through the SDK and
over HTTP.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.support.options import base_url

from .runner import Runner
from .suite import Check, SuiteError, check_stage, select

_RUNNER = pytest.StashKey[Runner]()
_NOT_RUN = pytest.StashKey[tuple[Check, ...]]()
_SUITE = "tests/conformance"


def pytest_configure(config: pytest.Config) -> None:
    base_url(config, _SUITE)
    stage: int | None = config.getoption("stage")
    if stage is None:
        raise pytest.UsageError(f"{_SUITE} needs --stage, the stage the API serves")
    try:
        check_stage(stage)
    except SuiteError as error:
        raise pytest.UsageError(f"--stage {error}") from None


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "check" not in metafunc.fixturenames:
        return
    selection = select(metafunc.config.getoption("stage"))
    metafunc.config.stash[_NOT_RUN] = selection.not_run
    metafunc.parametrize(
        "check", selection.checks, ids=[check.id for check in selection.checks]
    )


@pytest.fixture(scope="session")
def runner(pytestconfig: pytest.Config) -> Iterator[Runner]:
    runner = Runner(base_url(pytestconfig, _SUITE))
    pytestconfig.stash[_RUNNER] = runner
    yield runner
    runner.close()


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, config: pytest.Config
) -> None:
    runner = config.stash.get(_RUNNER, None)
    if runner is None:
        return
    stage = config.getoption("stage")
    terminalreporter.write_sep("=", f"SDK runner, stage {stage}")
    width = max(len(name) for name in runner.counts) if runner.counts else 0
    terminalreporter.write_line(
        f"{'file':<{width}}  checks  steps through the SDK  steps over HTTP"
    )
    for name, counts in runner.counts.items():
        terminalreporter.write_line(
            f"{name:<{width}}  {counts.checks:>6}  {counts.sdk:>21}  {counts.http:>15}"
        )
    for check in config.stash.get(_NOT_RUN, ()):
        terminalreporter.write_line(
            f"not run at stage {stage}, which its stages member does not list: {check}"
        )
