"""The scenario harness: the profile, and which scenarios are turned on.

`spec/conformance.md` defines both under "How a scenario runs". A scenario
is turned on only by listing its name in `enabled.txt`, one per line. Each
listed name must be a heading under **Scenarios** in `spec/conformance.md`,
and have a test, `test_<name>` with hyphens replaced by underscores, in a
`test_*.py` file of this directory. Nothing else skips or relaxes a scenario.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Final

from financial_data import Client, RetryPolicy
from tests.support.api import CREDENTIALS, DEFAULT_CREDENTIAL

DIRECTORY: Final = Path(__file__).resolve().parent
ENABLED: Final = DIRECTORY / "enabled.txt"
SPEC: Final = DIRECTORY.parents[1] / "spec" / "conformance.md"

PROFILE_KEY: Final = CREDENTIALS[DEFAULT_CREDENTIAL].api_key
PROFILE_TIMEOUT: Final = 5.0
PROFILE_RETRY: Final = RetryPolicy(
    max_attempts=3, max_retry_wait=3.0, base_delay=0.1, max_delay=0.4, jitter=False
)
"""Without jitter, the waits before the second and third attempts are 0.1 and 0.2 s."""

_HEADING: Final = re.compile(r"#### `([^`]+)`")


def profile_client(base_url: str, **changes: Any) -> Client:
    """Return the client every scenario starts from, unless it says otherwise.

    Its values are short so that scenarios run in seconds; they are not
    recommended defaults. `changes` replaces any of `Client`'s arguments.
    """
    arguments: dict[str, Any] = {
        "api_key": PROFILE_KEY,
        "base_url": base_url,
        "timeout": PROFILE_TIMEOUT,
        "retry": PROFILE_RETRY,
    }
    return Client(**{**arguments, **changes})


def scenario_headings(text: str) -> list[str]:
    """Return the scenario names under `## Scenarios`, in order.

    Each is a `####` heading that is a name in backticks, until the next
    `##` heading.
    """
    names: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line == "## Scenarios"
        elif inside:
            heading = _HEADING.fullmatch(line)
            if heading is not None:
                names.append(heading.group(1))
    return names


def function_for(scenario: str) -> str:
    """Return the name of a scenario's test: `test_<name>`, with `_` for `-`."""
    return "test_" + scenario.replace("-", "_")


def read_enabled(path: Path = ENABLED) -> list[str]:
    """Return the names listed in `enabled.txt`, ignoring blank lines."""
    return [line for line in path.read_text().splitlines() if line.strip()]


def scenario_tests(directory: Path = DIRECTORY) -> set[str]:
    """Return the names of the test functions in the directory's `test_*.py` files.

    The files are parsed, not imported, so the check needs no API and sees
    every test, even when a run collects only some of them.
    """
    names: set[str] = set()
    for path in sorted(directory.glob("test_*.py")):
        tree = ast.parse(path.read_text(), str(path))
        names.update(
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        )
    return names


def problems(
    enabled: Iterable[str], headings: Iterable[str], tests: Iterable[str]
) -> list[str]:
    """Return what is wrong with the listed names: each one must be a heading,
    have a test, and be listed once."""
    headings = set(headings)
    tests = set(tests)
    found = []
    seen: set[str] = set()
    for name in enabled:
        if name in seen:
            found.append(f"{name!r} is listed more than once")
        seen.add(name)
        if name not in headings:
            found.append(
                f"{name!r} is not a heading under Scenarios in spec/conformance.md"
            )
        if function_for(name) not in tests:
            found.append(
                f"{name!r} has no test {function_for(name)} in tests/scenarios"
            )
    return found
