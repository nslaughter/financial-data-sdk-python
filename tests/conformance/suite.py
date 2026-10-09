"""Reading `contract/expected/` and choosing the checks a stage runs.

The files are read strictly: a member the API's conformance format does not
define is an error, so the runner never passes over a check it does not
understand.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from tests.support.api import CONTRACT

EXPECTED: Final = CONTRACT / "expected"
LAST_STAGE: Final = 2
"""The API's last stage: stage 1 is the demo API, and stage 2 the full API."""

SDK_FILES: Final[Mapping[int, tuple[str, ...]]] = {
    1: (
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
    ),
    # The stage table requires the stage 2 files of an SDK only once it adds
    # each feature, and this SDK has added none.
    2: (),
}
"""The files the stage table first requires of this SDK at each stage."""

_FILE_MEMBERS: Final = frozenset(
    {
        "contract_version",
        "case",
        "description",
        "checks",
        "pages_with_page_size_10",
        "position_checks",
        "read_checks",
        "apply_checks",
        "scenarios",
    }
)
_QUERY_CHECK: Final = (
    {"name", "query", "expected"},
    {"reason", "clock", "contrast_available_as_of"},
)
_PAGE: Final = ({"first", "last", "count"}, set[str]())
_POSITION_CHECK: Final = ({"name", "at", "expected_position"}, {"reason"})
_READ_CHECK: Final = (
    {
        "name",
        "after_position",
        "limit",
        "expected",
        "expected_next_position",
        "expected_head_position",
    },
    {"reason", "clock"},
)
_APPLY_CHECK: Final = (
    {
        "name",
        "from_position",
        "through_position",
        "observation_id",
        "expected_current_revision_id",
    },
    {"reason", "clock", "incorrect_if_applied_by_arrival"},
)
_SCENARIO: Final = (
    {"name", "steps"},
    {"reason", "clock", "stages", "expected_local_copy"},
)
_ACTIONS: Final = ("set_clock", "set_credential", "reset", "request")
_REQUEST: Final = (
    {"path"},
    {"method", "query", "credential", "authorization", "body"},
)
_EXPECT: Final = (
    {"status"},
    {"code", "body", "headers", "body_sha256", "body_lines"},
)


class SuiteError(Exception):
    """A file of `contract/expected/` that the runner cannot run as written."""


@dataclass(frozen=True, slots=True)
class Check:
    """One check the runner runs, with a reset before it.

    `kind` is `query check`, `page check` (a query check's pages with
    `page_size=10`), `position check`, `read check`, `apply check`, or
    `scenario`. `body` is the check's object as the file writes it, and
    `pages` is `pages_with_page_size_10` for a page check.
    """

    file: str
    kind: str
    name: str
    body: Mapping[str, Any] = field(repr=False)
    pages: tuple[Mapping[str, Any], ...] = field(default=(), repr=False)

    def __str__(self) -> str:
        return f'{self.file}: {self.kind} "{self.name}"'

    @property
    def id(self) -> str:
        """The check's pytest ID, such as `full-history: query check: latest`."""
        return f"{self.file}: {self.kind}: {self.name}"


@dataclass(frozen=True, slots=True)
class Selection:
    """The checks a stage runs, and the scenarios it does not run."""

    checks: tuple[Check, ...]
    not_run: tuple[Check, ...]
    """Scenarios whose `stages` member does not list the stage."""


def check_stage(stage: int) -> None:
    if not 1 <= stage <= LAST_STAGE:
        raise SuiteError(f"stage {stage}: the API's stages are 1 to {LAST_STAGE}")


def required_files(stage: int) -> tuple[str, ...]:
    """Every file the stage table requires of this SDK at or before a stage."""
    check_stage(stage)
    return tuple(name for s in range(1, stage + 1) for name in SDK_FILES[s])


def select(stage: int, directory: Path = EXPECTED) -> Selection:
    """Read the files a stage requires, and return their checks in order.

    Within a file, the order is its query checks, their page checks, its
    position, read, and apply checks, and its scenarios, as the API runner
    orders them. A scenario runs only when its `stages` member, if it has
    one, lists the stage.
    """
    checks: list[Check] = []
    not_run: list[Check] = []
    for name in required_files(stage):
        path = directory / f"{name}.json"
        if not path.is_file():
            raise SuiteError(f"expected/{name}.json is missing")
        for check in read_file(name, path):
            stages = check.body.get("stages") if check.kind == "scenario" else None
            if stages is not None and stage not in stages:
                not_run.append(check)
            else:
                checks.append(check)
    return Selection(tuple(checks), tuple(not_run))


def load_json(text: str | bytes) -> Any:
    """Parse JSON with each fractional number as a `Decimal`, so it stays exact."""
    return json.loads(text, parse_float=Decimal)


def read_file(name: str, path: Path) -> list[Check]:
    """Read one file of `contract/expected/` into its checks."""
    document = load_json(path.read_text(encoding="utf-8"))
    where = f"expected/{name}.json"
    _members(where, document, ({"contract_version"}, _FILE_MEMBERS))
    checks: list[Check] = []
    queries = _entries(where, document, "checks", _QUERY_CHECK)
    for entry in queries:
        _string_map(f"{where}: query check {entry['name']!r}: query", entry["query"])
        checks.append(Check(name, "query check", entry["name"], entry))
    pages = _entries(where, document, "pages_with_page_size_10", _PAGE)
    if pages:
        for entry in queries:
            checks.append(Check(name, "page check", entry["name"], entry, tuple(pages)))
    for entry in _entries(where, document, "position_checks", _POSITION_CHECK):
        checks.append(Check(name, "position check", entry["name"], entry))
    for entry in _entries(where, document, "read_checks", _READ_CHECK):
        _integer(where, entry, "after_position")
        if entry["limit"] is not None:
            _integer(where, entry, "limit")
        checks.append(Check(name, "read check", entry["name"], entry))
    for entry in _entries(where, document, "apply_checks", _APPLY_CHECK):
        _integer(where, entry, "from_position")
        _integer(where, entry, "through_position")
        checks.append(Check(name, "apply check", entry["name"], entry))
    for entry in _entries(where, document, "scenarios", _SCENARIO):
        _scenario(f"{where}: scenario {entry['name']!r}", entry)
        checks.append(Check(name, "scenario", entry["name"], entry))
    return checks


def _members(
    where: str,
    value: object,
    members: tuple[AbstractSet[str], AbstractSet[str]],
) -> None:
    required, optional = members
    if not isinstance(value, dict):
        raise SuiteError(f"{where}: not an object")
    missing = sorted(set(required) - value.keys())
    unknown = sorted(value.keys() - set(required) - set(optional))
    if missing:
        raise SuiteError(f"{where}: missing {', '.join(missing)}")
    if unknown:
        raise SuiteError(f"{where}: unknown member {', '.join(unknown)}")


def _entries(
    where: str,
    document: Mapping[str, Any],
    member: str,
    members: tuple[AbstractSet[str], AbstractSet[str]],
) -> list[dict[str, Any]]:
    entries = document.get(member, [])
    if not isinstance(entries, list):
        raise SuiteError(f"{where}: {member} is not an array")
    for index, entry in enumerate(entries):
        _members(f"{where}: {member}[{index}]", entry, members)
        if "name" in members[0] and not isinstance(entry["name"], str):
            raise SuiteError(f"{where}: {member}[{index}]: name is not a string")
    return entries


def _string_map(where: str, value: object) -> None:
    if not isinstance(value, dict) or not all(
        v is None or isinstance(v, str) for v in value.values()
    ):
        raise SuiteError(f"{where}: not an object of strings and nulls")


def _integer(where: str, entry: Mapping[str, Any], member: str) -> None:
    value = entry[member]
    if not isinstance(value, int) or isinstance(value, bool):
        raise SuiteError(f"{where}: {entry['name']!r}: {member} is not an integer")


def _scenario(where: str, scenario: Mapping[str, Any]) -> None:
    stages = scenario.get("stages")
    if stages is not None and not (
        isinstance(stages, list)
        and all(isinstance(s, int) and not isinstance(s, bool) for s in stages)
    ):
        raise SuiteError(f"{where}: stages is not an array of integers")
    steps = scenario["steps"]
    if not isinstance(steps, list) or not steps:
        raise SuiteError(f"{where}: steps is not a non-empty array")
    for number, step in enumerate(steps, 1):
        _step(f"{where}: step {number}", step)


def _step(where: str, step: object) -> None:
    if not isinstance(step, dict):
        raise SuiteError(f"{where}: not an object")
    actions = [action for action in _ACTIONS if action in step]
    if len(actions) != 1:
        raise SuiteError(f"{where}: has {len(actions)} actions; it must have one")
    if actions[0] == "request":
        _members(where, step, ({"request", "expect"}, {"id"}))
        _members(f"{where}: request", step["request"], _REQUEST)
        _members(f"{where}: expect", step["expect"], _EXPECT)
        if "id" in step and not isinstance(step["id"], str):
            raise SuiteError(f"{where}: id is not a string")
        return
    _members(where, step, ({actions[0]}, set()))
    action = step[actions[0]]
    if actions[0] == "set_clock" and not isinstance(action, str):
        raise SuiteError(f"{where}: set_clock is not a string")
    if actions[0] == "reset" and not isinstance(action, dict):
        raise SuiteError(f"{where}: reset is not an object")
    if actions[0] == "set_credential" and not (
        isinstance(action, dict) and isinstance(action.get("credential_id"), str)
    ):
        raise SuiteError(f"{where}: set_credential has no credential_id string")


def scenario_steps(check: Check) -> Sequence[Mapping[str, Any]]:
    """Return a scenario's steps."""
    steps: Sequence[Mapping[str, Any]] = check.body["steps"]
    return steps
