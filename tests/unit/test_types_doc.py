"""The type documentation, `docs/types.md`, against the public names and the contract.

It maps every public name to the API's contract, and links the API's
documents at the tag the vendored contract pins.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

import pytest

import financial_data
import financial_data.pandas

ROOT: Final = Path(__file__).resolve().parents[2]
TYPES: Final = ROOT / "docs" / "types.md"
CONTRACT: Final = ROOT / "contract" / "CONTRACT.json"

# Every public name: `__all__`, and the dataframe conversion (D6).
PUBLIC_NAMES: Final = [*financial_data.__all__, *financial_data.pandas.__all__]

# The API's repository, and a link to it with the rest of the link's URL.
# A repository's name holds ASCII letters, digits, `-`, `.`, and `_`, but
# GitHub leaves a trailing `.` or `_` out of a bare link, as at the end of a
# sentence. So a link is to another repository only if a letter, digit, or
# `-` follows the name, after any `.` and `_`. A link to the repository
# itself counts too. The rest of the URL ends at `)`, `>`, or whitespace,
# which end an inline link, an autolink, and a reference or bare link.
REPOSITORY: Final = "https://github.com/nslaughter/financial-data-api"
API_LINK: Final = re.compile(re.escape(REPOSITORY) + r"(?![._]*[A-Za-z0-9-])([^)\s>]*)")


def types_md() -> str:
    return TYPES.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", PUBLIC_NAMES)
def test_every_public_name_is_documented(name: str) -> None:
    # As a code span of its own, so `ClientClosedError` does not count for
    # `Client`.
    assert f"`{name}`" in types_md()


@pytest.mark.parametrize(
    ("text", "path"),
    [
        pytest.param(
            f"[api]({REPOSITORY}/blob/main/spec/api.md)",
            "/blob/main/spec/api.md",
            id="inline",
        ),
        pytest.param(
            f"[api]: {REPOSITORY}/blob/main/spec/api.md\n",
            "/blob/main/spec/api.md",
            id="reference",
        ),
        pytest.param(
            f"<{REPOSITORY}/blob/main/spec/api.md>",
            "/blob/main/spec/api.md",
            id="autolink",
        ),
        pytest.param(f"<{REPOSITORY}>", "", id="autolink-to-the-repository"),
        pytest.param(f"[repo]({REPOSITORY}#readme)", "#readme", id="fragment"),
        pytest.param(f"[repo]: {REPOSITORY}?tab=readme\n", "?tab=readme", id="query"),
        pytest.param(f"See {REPOSITORY}", "", id="bare-at-the-end"),
        pytest.param(f"See {REPOSITORY}.", ".", id="bare-before-a-period"),
        pytest.param(f"_See {REPOSITORY}._", "._", id="bare-ending-emphasis"),
    ],
)
def test_the_link_pattern_finds_every_form_of_link(text: str, path: str) -> None:
    assert API_LINK.findall(text) == [path]


@pytest.mark.parametrize("suffix", ["-monitor", ".go", "_v2"])
def test_the_link_pattern_ignores_another_repository(suffix: str) -> None:
    assert API_LINK.findall(f"<{REPOSITORY}{suffix}/blob/main/README.md>") == []


def test_links_to_the_apis_documents_name_the_pinned_tag() -> None:
    tag = json.loads(CONTRACT.read_text(encoding="utf-8"))["tag"]
    paths = API_LINK.findall(types_md())
    assert paths
    assert [path for path in paths if not path.startswith(f"/blob/{tag}/")] == []
