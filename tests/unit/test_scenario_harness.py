"""The scenario harness: the profile, and which scenarios are turned on."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from financial_data import RetryPolicy
from tests.conformance import conftest as conformance_conftest
from tests.scenarios import conftest as scenarios_conftest
from tests.scenarios.harness import (
    DIRECTORY,
    ENABLED,
    PROFILE_KEY,
    PROFILE_RETRY,
    PROFILE_TIMEOUT,
    SPEC,
    function_for,
    problems,
    profile_client,
    read_enabled,
    scenario_headings,
    scenario_tests,
)
from tests.support.options import base_url


def test_the_specification_has_33_scenario_headings() -> None:
    # Decision 1 of spec/conformance.md counts 33.
    headings = scenario_headings(SPEC.read_text())
    assert len(headings) == 33
    assert len(set(headings)) == 33
    assert headings[0] == "retry-after-seconds"
    assert headings[-1] == "scheduled-job-starts-empty"
    assert "request-id-from-the-api" in headings
    assert "dataframe-keeps-revision-information" in headings


def test_headings_count_only_under_scenarios() -> None:
    text = "\n".join(
        [
            "## Profile",
            "#### `not-a-scenario`",
            "## Scenarios",
            "### Throttling",
            "#### `first`",
            "Text with `#### \\`inline\\``.",
            "#### `second`",
            "#### second-without-backticks",
            "## Coverage",
            "#### `after`",
        ]
    )
    assert scenario_headings(text) == ["first", "second"]


def test_each_scenarios_test_is_named_from_it() -> None:
    assert function_for("retry-after-seconds") == "test_retry_after_seconds"
    assert function_for("key-absent-from-errors-and-logs") == (
        "test_key_absent_from_errors_and_logs"
    )


def test_a_listed_name_needs_a_heading_and_a_test_and_is_listed_once() -> None:
    headings = ["retry-after-seconds", "iteration-is-lazy"]
    tests = {"test_retry_after_seconds", "test_iteration_is_lazy", "test_other"}
    assert problems(["retry-after-seconds", "iteration-is-lazy"], headings, tests) == []
    assert problems([], headings, tests) == []
    assert problems(["no-such-scenario"], headings, tests) == [
        "'no-such-scenario' is not a heading under Scenarios in spec/conformance.md",
        "'no-such-scenario' has no test test_no_such_scenario in tests/scenarios",
    ]
    assert problems(["iteration-is-lazy"], headings, {"test_retry_after_seconds"}) == [
        "'iteration-is-lazy' has no test test_iteration_is_lazy in tests/scenarios"
    ]
    assert problems(["retry-after-seconds"] * 2, headings, tests) == [
        "'retry-after-seconds' is listed more than once"
    ]


def test_enabled_lists_one_name_per_line(tmp_path: Path) -> None:
    path = tmp_path / "enabled.txt"
    path.write_text("retry-after-seconds\n\niteration-is-lazy\n")
    assert read_enabled(path) == ["retry-after-seconds", "iteration-is-lazy"]
    path.write_text("")
    assert read_enabled(path) == []
    path.write_text(" retry-after-seconds\n")
    assert problems(read_enabled(path), ["retry-after-seconds"], set()) != []


def test_scenario_tests_are_read_from_the_files_without_importing_them(
    tmp_path: Path,
) -> None:
    (tmp_path / "test_throttling.py").write_text(
        "import no_such_module\n"
        "def test_retry_after_seconds(client): ...\n"
        "def helper(): ...\n"
        "async def test_not_sync(): ...\n"
        "class TestGroup:\n"
        "    def test_method(self): ...\n"
    )
    (tmp_path / "test_responses.py").write_text(
        "def test_responses_are_validated(): ...\n"
    )
    (tmp_path / "conftest.py").write_text("def test_in_conftest(): ...\n")
    assert scenario_tests(tmp_path) == {
        "test_retry_after_seconds",
        "test_responses_are_validated",
    }


def test_the_enabled_list_agrees_with_the_specification_and_the_tests() -> None:
    enabled = read_enabled(ENABLED)
    assert (
        problems(enabled, scenario_headings(SPEC.read_text()), scenario_tests()) == []
    )


def test_the_profile_is_the_conformance_documents() -> None:
    assert PROFILE_KEY == "demo-research-key"
    assert PROFILE_TIMEOUT == 5.0
    assert (
        RetryPolicy(
            max_attempts=3,
            max_retry_wait=3.0,
            base_delay=0.1,
            max_delay=0.4,
            jitter=False,
        )
        == PROFILE_RETRY
    )


def test_the_profile_client_sends_the_key_through_the_url_given() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    with (
        httpx.Client(transport=httpx.MockTransport(answer)) as http,
        profile_client("http://proxy.test:1234", http_client=http) as client,
    ):
        assert client.series.list() == ()
    (request,) = seen
    assert str(request.url) == "http://proxy.test:1234/v1/series"
    assert request.headers["Authorization"] == "Bearer demo-research-key"
    assert request.extensions["timeout"]["read"] == pytest.approx(5.0, abs=0.1)


# The pytest hooks


class _Item:
    def __init__(self, path: Path, name: str) -> None:
        self.path = path
        self.name = f"{name}[x]"
        self.originalname = name


class _Hook:
    def __init__(self) -> None:
        self.deselected: list[Any] = []

    def pytest_deselected(self, items: list[Any]) -> None:
        self.deselected.extend(items)


def _collect(
    monkeypatch: pytest.MonkeyPatch, enabled: list[str], tests: set[str]
) -> tuple[list[Any], list[Any]]:
    monkeypatch.setattr(scenarios_conftest, "read_enabled", lambda: enabled)
    monkeypatch.setattr(scenarios_conftest, "scenario_tests", lambda: tests)
    hook = _Hook()
    items: list[Any] = [
        _Item(DIRECTORY / "test_throttling.py", "test_retry_after_seconds"),
        _Item(DIRECTORY / "test_throttling.py", "test_retry_after_invalid"),
        _Item(DIRECTORY.parent / "unit" / "test_x.py", "test_retry_after_invalid"),
    ]
    config = cast(pytest.Config, SimpleNamespace(hook=hook))
    scenarios_conftest.pytest_collection_modifyitems(config, items)
    return items, hook.deselected


def test_only_listed_scenarios_are_collected(monkeypatch: pytest.MonkeyPatch) -> None:
    tests = {"test_retry_after_seconds", "test_retry_after_invalid"}
    kept, deselected = _collect(monkeypatch, ["retry-after-seconds"], tests)
    assert [(i.path.parent.name, i.originalname) for i in kept] == [
        ("scenarios", "test_retry_after_seconds"),
        ("unit", "test_retry_after_invalid"),
    ]
    assert [i.originalname for i in deselected] == ["test_retry_after_invalid"]
    kept, deselected = _collect(monkeypatch, [], tests)
    assert [i.path.parent.name for i in kept] == ["unit"]
    assert len(deselected) == 2


def test_a_listed_name_without_a_test_stops_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(pytest.UsageError, match="has no test test_retry_after_seconds"):
        _collect(monkeypatch, ["retry-after-seconds"], set())


def _config(**options: object) -> pytest.Config:
    values = {"base_url": None, "stage": None, **options}
    return cast(
        pytest.Config,
        SimpleNamespace(
            getoption=lambda name: values[name],
            addinivalue_line=lambda name, line: None,
        ),
    )


@pytest.mark.parametrize(
    ("url", "message"),
    [
        (None, "needs --base-url"),
        ("localhost:8080", "want http:// or https:// and a host"),
        ("ftp://localhost", "want http:// or https:// and a host"),
        ("http://", "want http:// or https:// and a host"),
        ("http://localhost:8080?x=1", "want no query or fragment"),
        ("http://localhost:8080#x", "want no query or fragment"),
    ],
)
def test_the_base_url_is_required_and_checked(url: str | None, message: str) -> None:
    with pytest.raises(pytest.UsageError, match=message):
        base_url(_config(base_url=url), "suite")


def test_a_base_url_may_have_a_path() -> None:
    url = "https://api.test/gateway/"
    assert base_url(_config(base_url=url), "suite") == url
    scenarios_conftest.pytest_configure(_config(base_url=url))


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"stage": 1}, "tests/conformance needs --base-url"),
        ({"base_url": "http://localhost:8080"}, "tests/conformance needs --stage"),
        (
            {"base_url": "http://localhost:8080", "stage": 0},
            "the API's stages are 1 to 2",
        ),
        (
            {"base_url": "http://localhost:8080", "stage": 3},
            "the API's stages are 1 to 2",
        ),
    ],
)
def test_the_runner_requires_both_options(
    options: dict[str, object], message: str
) -> None:
    with pytest.raises(pytest.UsageError, match=message):
        conformance_conftest.pytest_configure(_config(**options))


def test_the_runner_accepts_both_options() -> None:
    conformance_conftest.pytest_configure(
        _config(base_url="http://localhost:8080", stage=2)
    )
