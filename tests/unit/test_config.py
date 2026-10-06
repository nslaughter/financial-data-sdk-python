"""Checking and resolving the client's settings."""

from __future__ import annotations

import dataclasses
import math
import traceback
from decimal import Decimal
from typing import Any

import httpx
import pytest

from financial_data import Client, ConfigError, RetryPolicy

KEY = "demo-research-key"
API_KEY_ENV = "FINANCIAL_DATA_API_KEY"
BASE_URL_ENV = "FINANCIAL_DATA_BASE_URL"


def renderings(error: BaseException) -> list[str]:
    """Every rendering of an exception that could repeat what it was given."""
    texts = [str(error), repr(error), "".join(traceback.format_exception_only(error))]
    texts += [repr(arg) for arg in error.args]
    texts += [arg for arg in error.args if isinstance(arg, str)]
    return texts


def assert_hidden(error: BaseException, secret: str) -> None:
    for text in renderings(error):
        assert secret not in text
        # A key wrapped in other characters still shows its ASCII core.
        assert "demo-research" not in text


def refusing_handler(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected request: {request.method} {request.url}")


# Keys

INVALID_KEYS = {
    "empty": "",
    "space": "demo research-key",
    "leading-space": " demo-research-key",
    "trailing-space": "demo-research-key ",
    "tab": "demo-research\tkey",
    "newline": "demo-research-key\n",
    "crlf": "demo-research-key\r\n",
    "nul": "demo-research-key\x00",
    "escape": "demo-research-key\x1b",
    "delete": "demo-research-key\x7f",
    "curly-quotes": "\u201cdemo-research-key\u201d",
    "accented": "demo-research-k\u00e9y",
    "no-break-space": "demo-research-key\u00a0",
    "line-separator": "demo-research-key\u2028",
    "emoji": "demo-research-key\U0001f511",
}
# An environment variable cannot hold a NUL character.
INVALID_ENVIRONMENT_KEYS = {
    name: key for name, key in INVALID_KEYS.items() if "\x00" not in key
}


@pytest.mark.parametrize("key", INVALID_KEYS.values(), ids=INVALID_KEYS.keys())
def test_invalid_key_is_refused_without_repeating_it(key: str) -> None:
    with pytest.raises(ConfigError) as caught:
        Client(api_key=key)
    error = caught.value
    if key:
        assert_hidden(error, key)
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize(
    "key", INVALID_ENVIRONMENT_KEYS.values(), ids=INVALID_ENVIRONMENT_KEYS.keys()
)
def test_invalid_key_from_the_environment_is_refused_without_repeating_it(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty variable counts as unset, so it is refused as a missing key.
    monkeypatch.setenv(API_KEY_ENV, key)
    with pytest.raises(ConfigError) as caught:
        Client()
    error = caught.value
    if key:
        assert_hidden(error, key)
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize(
    "key",
    [
        b"demo-research-key",
        bytearray(b"demo-research-key"),
        12345,
        ["demo-research-key"],
    ],
    ids=["bytes", "bytearray", "int", "list"],
)
def test_key_that_is_not_a_string_is_refused_without_repeating_it(key: Any) -> None:
    with pytest.raises(ConfigError) as caught:
        Client(api_key=key)
    error = caught.value
    assert_hidden(error, "demo-research-key")
    assert "12345" not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_missing_key_is_refused() -> None:
    with pytest.raises(ConfigError, match=API_KEY_ENV):
        Client()


def test_empty_key_variable_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "")
    with pytest.raises(ConfigError, match="no API key"):
        Client()


def test_key_comes_from_the_environment_when_not_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(API_KEY_ENV, KEY)
    assert Client()._api_key == KEY


def test_given_key_is_used_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(API_KEY_ENV, "demo-unentitled-key")
    assert Client(api_key=KEY)._api_key == KEY


def test_given_empty_key_does_not_fall_back_to_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(API_KEY_ENV, KEY)
    with pytest.raises(ConfigError):
        Client(api_key="")


def test_every_visible_ascii_character_is_allowed_in_a_key() -> None:
    key = "".join(chr(code) for code in range(0x21, 0x7F))
    assert Client(api_key=key)._api_key == key


# Base URLs


def test_base_url_defaults_to_localhost() -> None:
    assert Client(api_key=KEY)._base_url == "http://localhost:8080"


def test_base_url_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BASE_URL_ENV, "https://api.example.com/gateway")
    assert Client(api_key=KEY)._base_url == "https://api.example.com/gateway"


def test_empty_base_url_variable_counts_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BASE_URL_ENV, "")
    assert Client(api_key=KEY)._base_url == "http://localhost:8080"


def test_given_base_url_is_used_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BASE_URL_ENV, "https://other.example.com")
    client = Client(api_key=KEY, base_url="http://127.0.0.1:9000")
    assert client._base_url == "http://127.0.0.1:9000"


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("http://localhost:8080", "http://localhost:8080"),
        ("http://localhost:8080/", "http://localhost:8080"),
        ("https://api.example.com", "https://api.example.com"),
        (
            "https://api.example.com/gateway/financial",
            "https://api.example.com/gateway/financial",
        ),
        ("https://api.example.com/gateway/", "https://api.example.com/gateway"),
        ("http://127.0.0.1:65535", "http://127.0.0.1:65535"),
        ("http://[::1]:8080", "http://[::1]:8080"),
        ("HTTP://LOCALHOST", "HTTP://LOCALHOST"),
    ],
)
def test_valid_base_url_is_kept_without_a_trailing_slash(given: str, kept: str) -> None:
    assert Client(api_key=KEY, base_url=given)._base_url == kept


INVALID_BASE_URLS = [
    pytest.param("", id="empty"),
    pytest.param("localhost:8080", id="no-scheme"),
    pytest.param("//localhost:8080", id="scheme-relative"),
    pytest.param("ftp://localhost", id="ftp"),
    pytest.param("http://", id="no-host"),
    pytest.param("http:///v1", id="empty-host"),
    pytest.param("http:/localhost", id="one-slash"),
    pytest.param("http://localhost:8080?", id="empty-query"),
    pytest.param("http://localhost:8080/?region=eu", id="query"),
    pytest.param("http://localhost:8080#", id="empty-fragment"),
    pytest.param("http://localhost:8080/#top", id="fragment"),
    pytest.param("http://user:secret@localhost", id="user-information"),
    pytest.param("http://@localhost", id="empty-user-information"),
    pytest.param("http://localhost:http", id="port-not-a-number"),
    pytest.param("http://localhost:+80", id="port-with-sign"),
    pytest.param("http://localhost:65536", id="port-out-of-range"),
    pytest.param("http://localhost:1:2", id="two-ports"),
    pytest.param("http://[::1", id="unclosed-bracket"),
    pytest.param(" http://localhost", id="leading-space"),
    pytest.param("http://local host", id="space-in-host"),
    pytest.param("http://localhost/a b", id="space-in-path"),
    pytest.param("http://localhost\n", id="newline"),
    pytest.param("http://localhost\x00", id="nul"),
]


@pytest.mark.parametrize("base_url", INVALID_BASE_URLS)
def test_invalid_base_url_is_refused(base_url: str) -> None:
    with pytest.raises(ConfigError, match="base_url") as caught:
        Client(api_key=KEY, base_url=base_url)
    assert_hidden(caught.value, KEY)
    assert caught.value.__context__ is None


def test_invalid_base_url_from_the_environment_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BASE_URL_ENV, "localhost:8080")
    with pytest.raises(ConfigError, match=BASE_URL_ENV) as caught:
        Client(api_key=KEY)
    assert_hidden(caught.value, KEY)


def test_base_url_that_is_not_a_string_is_refused() -> None:
    with pytest.raises(ConfigError, match="base_url") as caught:
        Client(api_key=KEY, base_url=httpx.URL("http://localhost:8080"))  # type: ignore[arg-type]
    assert_hidden(caught.value, KEY)


# Timeouts


@pytest.mark.parametrize(
    ("given", "kept"), [(60.0, 60.0), (1, 1.0), (0.001, 0.001), (None, None)]
)
def test_valid_timeout_is_kept(given: float | None, kept: float | None) -> None:
    assert Client(api_key=KEY, timeout=given)._timeout == kept


def test_timeout_defaults_to_sixty_seconds() -> None:
    assert Client(api_key=KEY)._timeout == 60.0


INVALID_TIMEOUTS = [
    pytest.param(0, id="zero"),
    pytest.param(0.0, id="zero-float"),
    pytest.param(-1, id="negative"),
    pytest.param(math.nan, id="nan"),
    pytest.param(math.inf, id="inf"),
    pytest.param(-math.inf, id="negative-inf"),
    pytest.param(10**400, id="beyond-float"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param("60", id="string"),
    pytest.param(Decimal("60"), id="decimal"),
    pytest.param(httpx.Timeout(60.0), id="httpx-timeout"),
]


@pytest.mark.parametrize("timeout", INVALID_TIMEOUTS)
def test_invalid_timeout_is_refused(timeout: Any) -> None:
    with pytest.raises(ConfigError, match="timeout") as caught:
        Client(api_key=KEY, timeout=timeout)
    assert_hidden(caught.value, KEY)


# Retry policies


def test_retry_policy_defaults() -> None:
    policy = RetryPolicy()
    assert (
        policy.max_attempts,
        policy.max_retry_wait,
        policy.base_delay,
        policy.max_delay,
        policy.jitter,
    ) == (4, 30.0, 0.5, 8.0, True)


def test_retry_defaults_to_the_default_policy() -> None:
    assert Client(api_key=KEY)._retry == RetryPolicy()


def test_given_retry_policy_is_kept() -> None:
    policy = RetryPolicy(max_attempts=1)
    assert Client(api_key=KEY, retry=policy)._retry is policy


@pytest.mark.parametrize(
    "fields",
    [
        {"max_attempts": 1},
        {"max_attempts": 10},
        {"max_retry_wait": 0},
        {"max_retry_wait": 0.0},
        {"base_delay": 0, "max_delay": 0},
        {"base_delay": 2, "max_delay": 2},
        {"jitter": False},
    ],
)
def test_valid_retry_policy_is_accepted(fields: dict[str, Any]) -> None:
    policy = RetryPolicy(**fields)
    for name, value in fields.items():
        assert getattr(policy, name) == value


INVALID_POLICIES = [
    pytest.param({"max_attempts": 0}, id="max_attempts-zero"),
    pytest.param({"max_attempts": -1}, id="max_attempts-negative"),
    pytest.param({"max_attempts": 2.0}, id="max_attempts-float"),
    pytest.param({"max_attempts": True}, id="max_attempts-bool"),
    pytest.param({"max_attempts": "4"}, id="max_attempts-string"),
    pytest.param({"max_attempts": None}, id="max_attempts-none"),
    pytest.param({"jitter": 1}, id="jitter-int"),
    pytest.param({"jitter": "yes"}, id="jitter-string"),
    pytest.param({"jitter": None}, id="jitter-none"),
    pytest.param({"base_delay": 9.0}, id="base_delay-above-max_delay"),
    pytest.param({"base_delay": 1, "max_delay": 0.5}, id="max_delay-below-base_delay"),
] + [
    pytest.param({name: value}, id=f"{name}-{label}")
    for name in ("max_retry_wait", "base_delay", "max_delay")
    for label, value in [
        ("negative", -0.5),
        ("nan", math.nan),
        ("inf", math.inf),
        ("beyond-float", 10**400),
        ("bool", True),
        ("string", "1"),
        ("none", None),
    ]
]


@pytest.mark.parametrize("fields", INVALID_POLICIES)
def test_invalid_retry_policy_is_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ConfigError, match="RetryPolicy"):
        RetryPolicy(**fields)


def test_replacing_a_retry_policy_field_is_checked() -> None:
    with pytest.raises(ConfigError, match="max_attempts"):
        dataclasses.replace(RetryPolicy(), max_attempts=0)


def test_retry_policy_is_frozen() -> None:
    policy = RetryPolicy()
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.max_attempts = 1  # type: ignore[misc]


@pytest.mark.parametrize("retry", ["fast", {"max_attempts": 1}, 4])
def test_retry_that_is_not_a_policy_is_refused(retry: Any) -> None:
    with pytest.raises(ConfigError, match="retry") as caught:
        Client(api_key=KEY, retry=retry)
    assert_hidden(caught.value, KEY)


# HTTP clients


def test_supplied_http_client_is_kept_and_not_used_at_construction() -> None:
    with httpx.Client(transport=httpx.MockTransport(refusing_handler)) as http_client:
        client = Client(api_key=KEY, http_client=http_client)
        assert client._http_client is http_client
        assert not http_client.is_closed


def test_http_client_that_is_not_an_httpx_client_is_refused() -> None:
    with pytest.raises(ConfigError, match="http_client") as caught:
        Client(api_key=KEY, http_client="http://localhost:8080")  # type: ignore[arg-type]
    assert_hidden(caught.value, KEY)


def test_async_http_client_is_refused() -> None:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(refusing_handler))
    with pytest.raises(ConfigError, match="http_client"):
        Client(api_key=KEY, http_client=http_client)  # type: ignore[arg-type]


# Deriving a client


def test_with_options_keeps_what_it_is_not_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = RetryPolicy(max_attempts=2)
    with httpx.Client(transport=httpx.MockTransport(refusing_handler)) as http_client:
        client = Client(
            api_key=KEY,
            base_url="https://api.example.com/gateway",
            timeout=5,
            retry=policy,
            http_client=http_client,
        )
        # The derived client reads nothing from the environment.
        monkeypatch.setenv(API_KEY_ENV, "demo-unentitled-key")
        monkeypatch.setenv(BASE_URL_ENV, "http://other.example.com")
        derived = client.with_options()
        assert derived is not client
        assert derived._api_key == KEY
        assert derived._base_url == "https://api.example.com/gateway"
        assert derived._timeout == 5.0
        assert derived._retry is policy
        assert derived._http_client is http_client


def test_with_options_changes_what_it_is_given() -> None:
    client = Client(api_key=KEY)
    policy = RetryPolicy(max_attempts=1)
    derived = client.with_options(timeout=2.5, retry=policy)
    assert (derived._timeout, derived._retry) == (2.5, policy)
    assert (client._timeout, client._retry) == (60.0, RetryPolicy())


def test_with_options_timeout_none_removes_the_deadline() -> None:
    derived = Client(api_key=KEY).with_options(timeout=None)
    assert derived._timeout is None
    assert derived._retry == RetryPolicy()


def test_with_options_retry_none_means_the_default_policy() -> None:
    client = Client(api_key=KEY, retry=RetryPolicy(max_attempts=1))
    assert client.with_options(retry=None)._retry == RetryPolicy()


def test_with_options_can_derive_from_a_derived_client() -> None:
    derived = Client(api_key=KEY, timeout=1).with_options(timeout=2)
    again = derived.with_options(retry=RetryPolicy(jitter=False))
    assert (again._api_key, again._timeout, again._retry.jitter) == (KEY, 2.0, False)


@pytest.mark.parametrize("timeout", INVALID_TIMEOUTS)
def test_with_options_refuses_an_invalid_timeout(timeout: Any) -> None:
    with pytest.raises(ConfigError, match="timeout") as caught:
        Client(api_key=KEY).with_options(timeout=timeout)
    assert_hidden(caught.value, KEY)


@pytest.mark.parametrize("retry", ["fast", {"max_attempts": 1}, 4])
def test_with_options_refuses_an_invalid_retry(retry: Any) -> None:
    with pytest.raises(ConfigError, match="retry") as caught:
        Client(api_key=KEY).with_options(retry=retry)
    assert_hidden(caught.value, KEY)


# repr


def test_repr_shows_the_base_url_and_not_the_key() -> None:
    client = Client(api_key=KEY, base_url="https://api.example.com/gateway/")
    assert repr(client) == "Client(base_url='https://api.example.com/gateway')"
    assert repr(client.with_options(timeout=None)) == repr(client)
    assert KEY not in repr(client)
