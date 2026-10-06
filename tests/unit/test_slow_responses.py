"""The deadline against a local server that answers slowly, over real sockets.

`httpx.MockTransport` bypasses httpcore's framing, so these tests send a
response from a small server on `127.0.0.1` through the SDK's own HTTP
client. Each read waits less than the attempt's timeout, so no httpx timeout
expires, and only the SDK's checks of the deadline can end the call (D9).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress

import pytest

from financial_data import DeadlineExceededError, RetryPolicy, _params
from financial_data._decode import decode_meta
from financial_data._transport import Pool, Transport

KEY = "demo-research-key"
BODY = json.dumps(
    {
        "api_version": "v1",
        "supported_api_versions": ["v1"],
        "contract_version": "0.3.0",
        "server_time": "2026-10-01T00:00:00Z",
    }
).encode()
TIMEOUT = 0.5
INTERVAL = 0.05
# Time for scheduling on a CI runner.
ALLOWANCE = 0.3

Respond = Callable[[socket.socket, threading.Event], None]


@contextmanager
def serve(respond: Respond) -> Iterator[str]:
    """Answer one request with `respond`, and yield the server's URL.

    `respond` sends the response through the connection, and stops early
    once the event is set, when the test ends.
    """
    stop = threading.Event()
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.05)

    def run() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            with connection:
                request = b""
                while b"\r\n\r\n" not in request:
                    data = connection.recv(65536)
                    if not data:
                        return
                    request += data
                # OSError: the client closed the connection.
                with suppress(OSError):
                    respond(connection, stop)
            return

    thread = threading.Thread(target=run)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        stop.set()
        thread.join()
        listener.close()


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[Pool]:
    """The SDK's own HTTP client, with no proxy from the environment."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    pool = Pool(None)
    yield pool
    pool.close()


def call(pool: Pool, url: str) -> tuple[DeadlineExceededError, float, float]:
    """Make a call that must exceed its deadline.

    Return the error, and when the call started and ended on the monotonic
    clock.
    """
    transport = Transport(
        api_key=KEY, base_url=url, timeout=TIMEOUT, retry=RetryPolicy(), pool=pool
    )
    started = time.monotonic()
    with pytest.raises(DeadlineExceededError) as caught:
        transport.call(_params.meta(), decode_meta)
    return caught.value, started, time.monotonic()


def test_a_body_framed_by_content_length_is_bounded_within_one_read(
    pool: Pool,
) -> None:
    def respond(connection: socket.socket, stop: threading.Event) -> None:
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: %d\r\n\r\n" % len(BODY)
        )
        for byte in BODY:
            if stop.wait(INTERVAL):
                return
            connection.sendall(bytes([byte]))

    with serve(respond) as url:
        error, started, ended = call(pool, url)
    # The whole body would take len(BODY) * INTERVAL, several seconds.
    assert TIMEOUT <= ended - started < TIMEOUT + INTERVAL + ALLOWANCE
    assert error.attempts == 1
    assert error.__cause__ is None
    assert error.__context__ is None


def test_a_chunked_body_is_checked_when_httpx_returns_the_chunks_data(
    pool: Pool,
) -> None:
    # The first chunk's size line arrives a byte at a time until after the
    # deadline. httpx returns nothing until the chunk's data arrives, and the
    # SDK raises at that check, the first after the deadline.
    late = 0.3
    data_sent: list[float] = []

    def respond(connection: socket.socket, stop: threading.Event) -> None:
        began = time.monotonic()
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n%x;" % len(BODY)
        )
        while time.monotonic() < began + TIMEOUT + late:
            if stop.wait(INTERVAL):
                return
            connection.sendall(b"x")
        data_sent.append(time.monotonic())
        connection.sendall(b"\r\n" + BODY + b"\r\n0\r\n\r\n")

    with serve(respond) as url:
        error, started, ended = call(pool, url)
    [sent] = data_sent
    assert ended - started >= TIMEOUT + late
    assert sent <= ended < sent + ALLOWANCE
    assert error.attempts == 1
    assert error.__cause__ is None


def test_a_request_that_never_answers_ends_with_an_httpx_timeout(pool: Pool) -> None:
    def respond(connection: socket.socket, stop: threading.Event) -> None:
        stop.wait()

    with serve(respond) as url:
        error, started, ended = call(pool, url)
    assert TIMEOUT <= ended - started < TIMEOUT + ALLOWANCE
    assert error.attempts == 1
    assert type(error.__cause__).__name__ == "ReadTimeout"
