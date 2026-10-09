"""Command-line options of the suites that run against the API.

`tests/conformance` requires `--base-url` and `--stage`, and
`tests/scenarios` requires `--base-url`. They are defined here, above both,
so that one run can collect either suite or both. The unit tests use
neither.
"""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("financial-data", "the API under test")
    group.addoption(
        "--base-url",
        metavar="URL",
        help="the URL of the API image started with TEST_CONTROL=enabled",
    )
    group.addoption(
        "--stage",
        type=int,
        metavar="N",
        help="the API stage the server serves, which chooses the shared checks",
    )
