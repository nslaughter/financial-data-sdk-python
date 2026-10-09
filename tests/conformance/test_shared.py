"""The shared checks of `contract/expected/`, through the SDK."""

from __future__ import annotations

import pytest

from .runner import Runner
from .suite import Check


def test_check(check: Check, runner: Runner) -> None:
    failure = runner.run(check)
    if failure is not None:
        pytest.fail(str(failure), pytrace=False)
