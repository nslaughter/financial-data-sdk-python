"""Checking the options of the suites that run against the API."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest


def base_url(config: pytest.Config, suite: str) -> str:
    """Return `--base-url`, which the suite requires, or raise `UsageError`."""
    url: str | None = config.getoption("base_url")
    if url is None:
        raise pytest.UsageError(f"{suite} needs --base-url, the API's URL")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise pytest.UsageError(
            f"--base-url {url!r}: want http:// or https:// and a host"
        )
    if parts.query or parts.fragment:
        raise pytest.UsageError(f"--base-url {url!r}: want no query or fragment")
    return url
