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

# A link to financial-data-api, and the rest of its URL.
API_LINK: Final = re.compile(
    r"https://github\.com/nslaughter/financial-data-api(?=[/)\s])([^)\s]*)"
)


def types_md() -> str:
    return TYPES.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", PUBLIC_NAMES)
def test_every_public_name_is_documented(name: str) -> None:
    # As a code span of its own, so `ClientClosedError` does not count for
    # `Client`.
    assert f"`{name}`" in types_md()


def test_links_to_the_apis_documents_name_the_pinned_tag() -> None:
    tag = json.loads(CONTRACT.read_text(encoding="utf-8"))["tag"]
    paths = API_LINK.findall(types_md())
    assert paths
    assert [path for path in paths if not path.startswith(f"/blob/{tag}/")] == []
