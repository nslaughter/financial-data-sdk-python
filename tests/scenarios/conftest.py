"""The scenario harness as a pytest suite.

Run it against the pinned API image, started with `TEST_CONTROL=enabled`:

    uv run pytest tests/scenarios --base-url http://localhost:8080

`--base-url` is required. The fault proxy starts in front of it for the
session. Each scenario starts by clearing the proxy's rules and records and
resetting the API directly, not through the proxy, to the clock its
`clock` marker names, or to the default clock. Only the scenarios listed in
`enabled.txt` run; the run fails if a listed name has no heading in
`spec/conformance.md` or no test here.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from financial_data import Client
from tests.faults.proxy import FaultProxy
from tests.support.api import APIControl
from tests.support.options import base_url

from .harness import (
    DIRECTORY,
    SPEC,
    function_for,
    problems,
    profile_client,
    read_enabled,
    scenario_headings,
    scenario_tests,
)

_SUITE = "tests/scenarios"


def pytest_configure(config: pytest.Config) -> None:
    base_url(config, _SUITE)
    config.addinivalue_line(
        "markers", "clock(timestamp): the clock the scenario resets the API to"
    )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    # Before `-k` and `-m` deselect anything, so that the check sees every
    # listed name, and a test that is not listed is never run.
    enabled = read_enabled()
    found = problems(enabled, scenario_headings(SPEC.read_text()), scenario_tests())
    if found:
        raise pytest.UsageError("tests/scenarios/enabled.txt: " + "; ".join(found))
    wanted = {function_for(name) for name in enabled}
    kept, deselected = [], []
    for item in items:
        name = getattr(item, "originalname", item.name)
        if item.path.is_relative_to(DIRECTORY) and name not in wanted:
            deselected.append(item)
        else:
            kept.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept


@pytest.fixture(scope="session")
def proxy(pytestconfig: pytest.Config) -> Iterator[FaultProxy]:
    """The fault proxy in front of the API, for the whole session."""
    with FaultProxy(base_url(pytestconfig, _SUITE)) as proxy:
        yield proxy


@pytest.fixture(scope="session")
def api(pytestconfig: pytest.Config) -> Iterator[APIControl]:
    """Test control of the API, sent directly, not through the proxy."""
    with APIControl(base_url(pytestconfig, _SUITE)) as control:
        yield control


@pytest.fixture(autouse=True)
def _scenario_start(
    request: pytest.FixtureRequest, proxy: FaultProxy, api: APIControl
) -> None:
    proxy.clear()
    marker = request.node.get_closest_marker("clock")
    api.reset(None if marker is None else marker.args[0])


@pytest.fixture
def client(proxy: FaultProxy) -> Iterator[Client]:
    """The profile's client, through the proxy, closed when the scenario ends."""
    with profile_client(proxy.url) as client:
        yield client
