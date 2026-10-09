"""The SDK runner's matching rule and references, as the API's format defines them."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from tests.conformance.matching import (
    Body,
    Difference,
    UnresolvedReference,
    decimal_text,
    match,
    resolve,
    resolve_text,
)
from tests.conformance.suite import load_json


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        ({"a": 1}, {"a": 1, "b": 2}),
        ({}, {"a": 1}),
        ([1, 2], [1, 2]),
        ([], []),
        ("102.0", "102.0"),
        (None, None),
        (True, True),
        (37, 37),
        (37, Decimal("37.0")),
        (37, 37.0),
        (Decimal("1.50"), Decimal("15e-1")),
        (Decimal("102.1"), 102.1),
        ({"data": [{"value": None}]}, {"data": [{"value": None, "x": 1}]}),
    ],
)
def test_values_that_match(expected: Any, actual: Any) -> None:
    assert match("body", expected, actual) is None


@pytest.mark.parametrize(
    ("expected", "actual", "at"),
    [
        ("102.0", "102", "body"),
        ("102.0", 102, "body"),
        ("102", Decimal("102"), "body"),
        (102, "102", "body"),
        (1, True, "body"),
        (True, 1, "body"),
        (0, False, "body"),
        (None, "null", "body"),
        (None, 0, "body"),
        ("a", None, "body"),
        ([1], [1, 2], "body"),
        ([1, 2], [1], "body"),
        ([1, 2], [2, 1], "body[0]"),
        ({"a": 1}, [1], "body"),
        ([1], {"0": 1}, "body"),
        ({"a": {"b": "x"}}, {"a": {"b": "y"}}, "body.a.b"),
        (
            {"data": [{"v": 1}, {"v": 2}]},
            {"data": [{"v": 1}, {"v": 3}]},
            "body.data[1].v",
        ),
        (Decimal("1.0000000000000001"), 1, "body"),
    ],
)
def test_values_that_differ_report_where(expected: Any, actual: Any, at: str) -> None:
    difference = match("body", expected, actual)
    assert difference is not None
    assert difference.at == at


def test_a_missing_member_differs_from_null() -> None:
    assert match("body", {"next_page_token": None}, {}) == Difference(
        "body.next_page_token", "null", "no member"
    )


def test_an_array_of_another_length_reports_both_lengths() -> None:
    assert match("data", [{"a": 1}], [{"a": 1}, {"a": 2}]) == Difference(
        "data", "1 elements", '2 elements: [{"a": 1}, {"a": 2}]'
    )


def test_numbers_are_rendered_as_numbers() -> None:
    difference = match("body", {"position": 36}, load_json('{"position": 3.6e1}'))
    assert difference is None
    difference = match("body", {"value": Decimal("1.5")}, {"value": "1.5"})
    assert difference == Difference("body.value", "1.5", '"1.5"')


def test_a_long_value_is_shortened() -> None:
    difference = match("body", "x", "y" * 1000)
    assert difference is not None
    assert difference.actual.endswith("…")
    assert len(difference.actual) == 401


@pytest.mark.parametrize(
    ("number", "text"),
    [
        (36, "36"),
        (-1, "-1"),
        (Decimal("36.0"), "36"),
        (Decimal("3.6e1"), "36"),
        (Decimal("1.50"), "1.5"),
        (Decimal("15e-1"), "1.5"),
        (Decimal("1e-7"), "0.0000001"),
        (2.5, "2.5"),
    ],
)
def test_numbers_are_written_in_decimal(number: Any, text: str) -> None:
    assert decimal_text(number) == text


BODIES = {
    "p1": Body({"next_page_token": "tok", "position": 36, "data": [{"id": "a"}]}),
    "e1": Body({"files": [{"url": "/x"}], "count": Decimal("2.0"), "ok": True}),
    "bad": Body(error="not JSON: Expecting value"),
}


def test_a_whole_reference_keeps_its_type() -> None:
    assert resolve_text("${p1.position}", BODIES) == 36
    assert resolve_text("${p1.next_page_token}", BODIES) == "tok"
    assert resolve_text("${e1.ok}", BODIES) is True
    assert resolve_text("${p1.data}", BODIES) == [{"id": "a"}]


def test_a_reference_in_a_longer_string_is_inserted_as_text() -> None:
    assert resolve_text("token=${p1.next_page_token};", BODIES) == "token=tok;"
    assert resolve_text("at ${p1.position}", BODIES) == "at 36"
    assert resolve_text("${e1.count}${e1.count}", BODIES) == "22"


def test_array_indexes_and_nested_members_resolve() -> None:
    assert resolve_text("${e1.files.0.url}", BODIES) == "/x"
    assert resolve_text("${p1.data.0.id}", BODIES) == "a"


def test_resolve_copies_strings_at_any_depth_but_not_member_names() -> None:
    value = {
        "${p1.position}": ["${p1.next_page_token}", {"x": "${p1.position}"}],
        "n": 1,
    }
    assert resolve(value, BODIES) == {"${p1.position}": ["tok", {"x": 36}], "n": 1}
    assert value["n"] == 1  # unchanged


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("${p2.next_page_token}", "names no step that has run"),
        ("${p1.missing}", "has no member 'missing'"),
        ("${p1.data.1}", "an array of 1 elements, has no element 1"),
        ("${p1.data.01}", "has no element 01"),
        ("${p1.data.-1}", "has no element -1"),
        ("${p1.position.x}", "is a number, which has no member 'x'"),
        ("${bad.x}", "step bad has no body: not JSON"),
        ("${p1}", "malformed reference"),
        ("${.x}", "malformed reference"),
        ("${p1.a..b}", "an empty member name"),
        ("x${e1.ok}", "must name a string or a number, not a boolean"),
        ("x${p1.data}", "must name a string or a number, not an array"),
    ],
)
def test_references_that_do_not_resolve_say_why(text: str, message: str) -> None:
    with pytest.raises(UnresolvedReference, match=message.replace(".", r"\.")):
        resolve_text(text, BODIES)


def test_text_without_references_is_unchanged() -> None:
    assert resolve_text("$p1.position and {p1}", BODIES) == "$p1.position and {p1}"
