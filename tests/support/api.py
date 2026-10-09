"""The demo API's credentials and its test control, as the test suites use them.

The credentials are those of `contract/fixtures/credentials.json`. Test
control resets the API, sets its clock, and changes a credential, with the
test-control key. The SDK has no method for any of it (client decision 14),
so the conformance runner and the scenarios call it over HTTP themselves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Final
from urllib.parse import quote

import httpx

CONTRACT: Final = Path(__file__).resolve().parents[2] / "contract"
DEFAULT_CREDENTIAL: Final = "cred_research"
"""The credential every check and scenario uses unless it names another."""


@dataclass(frozen=True, slots=True)
class Credential:
    credential_id: str
    api_key: str
    kind: str
    """`customer` or `test_control`."""


def _credentials() -> dict[str, Credential]:
    document = json.loads((CONTRACT / "fixtures" / "credentials.json").read_text())
    return {
        entry["credential_id"]: Credential(
            entry["credential_id"], entry["api_key"], entry["kind"]
        )
        for entry in document["credentials"]
    }


CREDENTIALS: Final = _credentials()
"""Every fixture credential, by `credential_id`."""
CUSTOMERS: Final = tuple(
    credential_id
    for credential_id, credential in CREDENTIALS.items()
    if credential.kind == "customer"
)
"""The customer credentials' IDs, in the fixture's order."""
(CONTROL_CREDENTIAL,) = [
    credential_id
    for credential_id, credential in CREDENTIALS.items()
    if credential.kind == "test_control"
]
"""The test-control credential's ID. Only `/test` accepts its key."""
CONTROL_KEY: Final = CREDENTIALS[CONTROL_CREDENTIAL].api_key
KEYS: Final = tuple(credential.api_key for credential in CREDENTIALS.values())
"""Every fixture key, which no failure report may contain."""


def without_keys(text: str) -> str:
    """Replace every fixture key in text with `[redacted]`."""
    for key in KEYS:
        text = text.replace(key, "[redacted]")
    return text


class APIControl:
    """Test control of the API at a base URL, sent directly, never through a proxy.

    Each method raises `AssertionError` unless the API answers `200`.
    """

    def __init__(
        self, base_url: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {CONTROL_KEY}"},
            timeout=30.0,
            transport=transport,
            trust_env=False,
        )

    def reset(self, clock: str | None = None) -> None:
        """Reset the API: its clock, credentials, page tokens, and exports."""
        self._send("POST", "/test/reset", {} if clock is None else {"clock": clock})

    def set_clock(self, now: str) -> None:
        """Move the API's clock forward to `now`."""
        self._send("PUT", "/test/clock", {"now": now})

    def set_credential(self, credential_id: str, **changes: Any) -> None:
        """Change a credential's `active` or `datasets` until the next reset."""
        self._send("PUT", f"/test/credentials/{quote(credential_id, safe='')}", changes)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> APIControl:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _send(self, method: str, path: str, body: object) -> None:
        response = self._http.request(method, path, json=body)
        if response.status_code != 200:
            raise AssertionError(
                f"test control {method} {path} answered {response.status_code}: "
                f"{without_keys(response.text[:400])}"
            )
