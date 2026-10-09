"""Reading `contract/expected/` and choosing the checks a stage runs."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.conformance.suite import (
    EXPECTED,
    SDK_FILES,
    SuiteError,
    read_file,
    required_files,
    select,
)

STAGE_1 = [
    "august-2026-at-cutoffs",
    "full-history",
    "missing-value",
    "withdrawal-and-rerelease",
    "out-of-order-arrival",
    "provider-correction",
    "late-source-release",
    "change-stream",
    "simulated-clock",
    "pagination",
    "access-control",
    "request-errors",
]
"""The files `spec/conformance.md` names for stage 1, in its order."""


def test_stage_1_runs_the_twelve_files_the_conformance_document_names() -> None:
    assert list(required_files(1)) == STAGE_1
    files = list(dict.fromkeys(check.file for check in select(1).checks))
    assert files == STAGE_1


def test_stage_2_adds_no_file_this_sdk_has_not_added_the_feature_for() -> None:
    assert SDK_FILES[2] == ()
    assert list(required_files(2)) == STAGE_1


@pytest.mark.parametrize("stage", [0, 3, -1])
def test_a_stage_outside_the_apis_stages_is_refused(stage: int) -> None:
    with pytest.raises(SuiteError, match="the API's stages are 1 to 2"):
        select(stage)


def test_no_file_outside_the_stage_table_runs() -> None:
    names = {check.file for check in select(2).checks}
    for skipped in ("published-as-of", "revision-history", "release-calendar"):
        assert skipped not in names
    assert "release-timing" not in names
    assert "exports" not in names
    assert "export-handoff" not in names


def test_every_stage_1_check_is_selected_in_the_api_runners_order() -> None:
    kinds = [(check.file, check.kind) for check in select(1).checks]
    counts = {
        ("august-2026-at-cutoffs", "query check"): 7,
        ("full-history", "query check"): 2,
        ("full-history", "page check"): 2,
        ("change-stream", "position check"): 4,
        ("change-stream", "read check"): 4,
        ("change-stream", "apply check"): 1,
        ("change-stream", "scenario"): 5,
        ("request-errors", "scenario"): 16,
    }
    for key, count in counts.items():
        assert kinds.count(key) == count, key
    change_stream = [kind for file, kind in kinds if file == "change-stream"]
    assert change_stream == sorted(
        change_stream,
        key=["position check", "read check", "apply check", "scenario"].index,
    )
    full_history = [kind for file, kind in kinds if file == "full-history"]
    assert full_history == ["query check", "query check", "page check", "page check"]
    assert len(kinds) == 95


def test_a_page_check_carries_the_files_expected_pages() -> None:
    (check, _) = [c for c in select(1).checks if c.kind == "page check"]
    assert check.name == "as of the September 4 research date"
    assert [page["count"] for page in check.pages] == [10, 10, 10, 2]


def test_a_scenario_runs_only_at_the_stages_it_lists() -> None:
    name = "a stage 1 server refuses stage 2 parameters and paths"
    at_1 = select(1)
    at_2 = select(2)
    assert name in {check.name for check in at_1.checks}
    assert at_1.not_run == ()
    assert name not in {check.name for check in at_2.checks}
    assert [(check.file, check.name) for check in at_2.not_run] == [
        ("request-errors", name)
    ]
    assert len(at_2.checks) == len(at_1.checks) - 1


def test_checks_name_their_file_kind_and_name() -> None:
    check = select(1).checks[0]
    assert str(check) == (
        "august-2026-at-cutoffs: query check "
        '"published and received, not yet available"'
    )
    assert check.id == (
        "august-2026-at-cutoffs: query check: published and received, not yet available"
    )


def _copy(tmp_path: Path) -> Path:
    directory = tmp_path / "expected"
    shutil.copytree(EXPECTED, directory)
    return directory


def _write(directory: Path, name: str, document: dict[str, Any]) -> None:
    (directory / f"{name}.json").write_text(json.dumps(document))


def test_a_missing_file_is_an_error(tmp_path: Path) -> None:
    directory = _copy(tmp_path)
    (directory / "pagination.json").unlink()
    with pytest.raises(SuiteError, match=r"expected/pagination\.json is missing"):
        select(1, directory)


def test_numbers_are_read_exactly(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text(
        '{"contract_version": "0.3.0", "position_checks": [{"name": "n", '
        '"at": "2026-10-01T00:00:00Z", "expected_position": 0.1}]}'
    )
    (check,) = read_file("x", path)
    assert str(check.body["expected_position"]) == "0.1"


SCENARIO = {"name": "s", "steps": [{"set_clock": "2026-10-01T00:00:00Z"}]}


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({}, "missing contract_version"),
        ({"contract_version": "0.3.0", "extra": 1}, "unknown member extra"),
        (
            {"contract_version": "0.3.0", "checks": [{"name": "n", "query": {}}]},
            "missing expected",
        ),
        (
            {
                "contract_version": "0.3.0",
                "checks": [{"name": "n", "query": {"a": 1}, "expected": []}],
            },
            "not an object of strings and nulls",
        ),
        (
            {
                "contract_version": "0.3.0",
                "read_checks": [
                    {
                        "name": "n",
                        "after_position": "16",
                        "limit": None,
                        "expected": [],
                        "expected_next_position": 16,
                        "expected_head_position": 37,
                    }
                ],
            },
            "after_position is not an integer",
        ),
        (
            {
                "contract_version": "0.3.0",
                "scenarios": [{**SCENARIO, "stages": [True]}],
            },
            "stages is not an array of integers",
        ),
        (
            {"contract_version": "0.3.0", "scenarios": [{**SCENARIO, "steps": []}]},
            "steps is not a non-empty array",
        ),
        (
            {
                "contract_version": "0.3.0",
                "scenarios": [{**SCENARIO, "steps": [{"set_clock": "x", "reset": {}}]}],
            },
            "step 1: has 2 actions",
        ),
        (
            {
                "contract_version": "0.3.0",
                "scenarios": [
                    {**SCENARIO, "steps": [{"request": {"path": "/v1/meta"}}]}
                ],
            },
            "step 1: missing expect",
        ),
        (
            {
                "contract_version": "0.3.0",
                "scenarios": [
                    {
                        **SCENARIO,
                        "steps": [
                            {
                                "request": {"path": "/v1/meta", "headers": {}},
                                "expect": {"status": 200},
                            }
                        ],
                    }
                ],
            },
            "request: unknown member headers",
        ),
        (
            {
                "contract_version": "0.3.0",
                "scenarios": [
                    {
                        **SCENARIO,
                        "steps": [
                            {
                                "request": {"path": "/v1/meta"},
                                "expect": {"status": 200, "json": {}},
                            }
                        ],
                    }
                ],
            },
            "expect: unknown member json",
        ),
        (
            {
                "contract_version": "0.3.0",
                "scenarios": [{**SCENARIO, "steps": [{"set_clock": "x", "id": "a"}]}],
            },
            "unknown member id",
        ),
        (
            {
                "contract_version": "0.3.0",
                "scenarios": [{**SCENARIO, "steps": [{"set_credential": {}}]}],
            },
            "no credential_id",
        ),
    ],
)
def test_a_member_the_format_does_not_define_is_an_error(
    tmp_path: Path, document: dict[str, Any], message: str
) -> None:
    path = tmp_path / "x.json"
    path.write_text(json.dumps(document))
    with pytest.raises(SuiteError, match=message):
        read_file("x", path)


def test_every_vendored_file_but_release_timing_reads() -> None:
    # SDK runners do not run release-timing, whose checks the monitor runs.
    for path in sorted(EXPECTED.glob("*.json")):
        if path.stem != "release-timing":
            assert read_file(path.stem, path), path.stem
