"""The fault proxy, in front of a local stub of the API on `127.0.0.1`.

The stub records each request it receives and answers by path: JSON framed
by `Content-Length` by default, a chunked body at `/chunked`, a gzip body at
`/gzip`, and a status with its own reason phrase at `/teapot`.
"""

from __future__ import annotations

import gzip
import json
import re
import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from tests.faults.proxy import (
    EVERY,
    Drop,
    FaultProxy,
    HTTPDate,
    Problem,
    Requests,
    Respond,
    Rewrite,
    Stall,
    Trickle,
    from_nth,
    http_date,
    nth,
)

BODY = {"data": [{"value": "102.4"}], "position": 36, "next_page_token": None}


class _Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _StubServer

    def do_GET(self) -> None:
        self._answer()

    def do_POST(self) -> None:
        self._answer()

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.received.append(
            {
                "method": self.command,
                "target": self.path,
                "headers": list(self.headers.items()),
                "body": body,
            }
        )
        path = self.path.partition("?")[0]
        content = json.dumps(BODY).encode()
        if path.endswith("/chunked"):
            self.send_response_only(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for start in range(0, len(content), 7):
                part = content[start : start + 7]
                self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        headers = {"Content-Type": "application/json", "X-Upstream": "stub"}
        if path.endswith("/gzip"):
            content = gzip.compress(content)
            headers["Content-Encoding"] = "gzip"
        status, reason = (
            (418, "Short and Stout") if path.endswith("/teapot") else (200, "OK")
        )
        self.send_response_only(status, reason)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _StubServer(ThreadingHTTPServer):
    daemon_threads = True
    received: list[dict[str, Any]]


@pytest.fixture
def stub() -> Iterator[_StubServer]:
    server = _StubServer(("127.0.0.1", 0), _Stub)
    server.received = []
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}
    )
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def _url(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[:2]
    return f"http://{host!s}:{port}"


@pytest.fixture
def proxy(stub: _StubServer) -> Iterator[FaultProxy]:
    with FaultProxy(_url(stub)) as proxy:
        yield proxy


@pytest.fixture
def http(proxy: FaultProxy) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=proxy.url, timeout=5.0, trust_env=False) as client:
        yield client


# Forwarding


def test_a_request_is_forwarded_unchanged(
    stub: _StubServer, proxy: FaultProxy, http: httpx.Client
) -> None:
    http.get(
        "/v1/series/a%2Fb?x=1%2B2&y=a+b",
        headers={"Authorization": "Bearer demo-research-key", "X-Customer": "acme"},
    )
    http.post(
        "/v1/series", content=b'{"a": 1}', headers={"Content-Type": "application/json"}
    )
    first, second = stub.received
    assert first["method"] == "GET"
    assert first["target"] == "/v1/series/a%2Fb?x=1%2B2&y=a+b"
    sent = dict(first["headers"])
    assert sent["Authorization"] == "Bearer demo-research-key"
    assert sent["X-Customer"] == "acme"
    assert sent["User-Agent"].startswith("python-httpx/")
    assert "Connection" not in sent
    assert second["method"] == "POST"
    assert second["body"] == b'{"a": 1}'


def test_a_response_is_returned_unchanged(
    proxy: FaultProxy, http: httpx.Client
) -> None:
    response = http.get("/v1/teapot")
    assert (response.status_code, response.reason_phrase) == (418, "Short and Stout")
    assert response.headers["X-Upstream"] == "stub"
    assert response.json() == BODY
    assert [name for name, _ in proxy.responses[0].headers] == [
        "Content-Type",
        "X-Upstream",
        "Content-Length",
    ]


def test_a_chunked_or_compressed_body_is_returned_as_the_api_framed_it(
    http: httpx.Client,
) -> None:
    chunked = http.get("/v1/chunked")
    assert chunked.headers["Transfer-Encoding"] == "chunked"
    assert chunked.json() == BODY
    with http.stream("GET", "/v1/gzip") as response:
        raw = b"".join(response.iter_raw())
    assert response.headers["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(raw)) == BODY


def test_an_upstream_path_prefix_is_kept(stub: _StubServer) -> None:
    with (
        FaultProxy(_url(stub) + "/gateway/") as proxy,
        httpx.Client(base_url=proxy.url, trust_env=False) as http,
    ):
        http.get("/v1/meta?a=1")
        assert proxy.requests[0].path == "/v1/meta"
    assert stub.received[0]["target"] == "/gateway/v1/meta?a=1"


def test_an_unreachable_api_is_answered_with_502() -> None:
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    with (
        FaultProxy(f"http://127.0.0.1:{port}") as proxy,
        httpx.Client(base_url=proxy.url, trust_env=False) as http,
    ):
        response = http.get("/v1/meta")
        assert response.status_code == 502
        assert response.text.startswith("The fault proxy could not reach the API")
        assert proxy.responses[0].status == 502


# Actions


def test_respond_answers_without_forwarding(
    stub: _StubServer, proxy: FaultProxy, http: httpx.Client
) -> None:
    proxy.add(
        "GET",
        "/v1/datasets",
        Respond(503, {"Content-Type": "text/plain"}, "Service Unavailable"),
    )
    proxy.add(
        "GET", "/v1/series", Respond(302, {"Location": "http://example.invalid/"})
    )
    response = http.get("/v1/datasets")
    assert (response.status_code, response.text) == (503, "Service Unavailable")
    assert response.headers["Content-Type"] == "text/plain"
    assert response.headers["Content-Length"] == "19"
    redirect = http.get("/v1/series")
    assert redirect.status_code == 302
    assert redirect.headers["Location"] == "http://example.invalid/"
    assert redirect.content == b""
    assert stub.received == []


def test_problem_answers_with_the_injected_problem(
    stub: _StubServer, proxy: FaultProxy, http: httpx.Client
) -> None:
    proxy.add(
        "GET",
        "/v1/series/activity-index",
        Problem(429, "rate_limited", headers={"Retry-After": "1"}),
    )
    proxy.add("GET", "/v1/series/x", Problem(400, "something_new", "series_id"))
    response = http.get("/v1/series/activity-index")
    assert response.status_code == 429
    assert response.headers["Content-Type"] == "application/problem+json"
    assert response.headers["Retry-After"] == "1"
    assert response.json() == {
        "status": 429,
        "code": "rate_limited",
        "title": "Injected fault",
        "detail": "Injected by the fault proxy.",
        "parameter": None,
    }
    assert http.get("/v1/series/x").json()["parameter"] == "series_id"
    assert stub.received == []


def test_drop_closes_the_connection_without_a_response(
    stub: _StubServer, proxy: FaultProxy, http: httpx.Client
) -> None:
    proxy.add("GET", "/v1/datasets", Drop())
    with pytest.raises(httpx.RemoteProtocolError):
        http.get("/v1/datasets")
    assert len(proxy.requests) == 1
    assert proxy.responses == ()
    assert stub.received == []
    _wait_until(lambda: all(c.closed is not None for c in proxy.connections))


def test_stall_sends_nothing_until_the_client_closes(
    stub: _StubServer, proxy: FaultProxy
) -> None:
    proxy.add("GET", "/v1/datasets", Stall())
    began = time.monotonic()
    with (
        httpx.Client(base_url=proxy.url, timeout=0.3, trust_env=False) as http,
        pytest.raises(httpx.ReadTimeout),
    ):
        http.get("/v1/datasets")
    assert time.monotonic() - began >= 0.3
    _wait_until(lambda: all(c.closed is not None for c in proxy.connections))
    assert proxy.responses == ()
    assert stub.received == []


def test_stall_ends_when_the_scenario_ends(proxy: FaultProxy) -> None:
    proxy.add("GET", "/v1/datasets", Stall())
    with socket.create_connection(_address(proxy)) as client:
        client.sendall(b"GET /v1/datasets HTTP/1.1\r\nHost: x\r\n\r\n")
        _wait_until(lambda: len(proxy.requests) == 1)
        client.settimeout(0.2)
        with pytest.raises(TimeoutError):
            client.recv(1)
        proxy.clear()
        client.settimeout(2.0)
        assert client.recv(1) == b""  # closed, with nothing sent


def test_trickle_sends_the_headers_at_once_and_the_body_in_parts(
    proxy: FaultProxy, http: httpx.Client
) -> None:
    content = json.dumps(BODY).encode()
    pieces = -(-len(content) // 32)
    proxy.add("GET", "/v1/gzip", Trickle(32, 0.25))
    began = time.monotonic()
    with http.stream("GET", "/v1/gzip") as response:
        headers_at = time.monotonic() - began
        parts = [(time.monotonic() - began, part) for part in response.iter_raw()]
    assert headers_at < 0.25
    assert b"".join(part for _, part in parts) == content
    assert all(len(part) <= 32 for _, part in parts)
    assert len(parts) >= 2
    # Each part waited its interval first, so part i arrives no sooner than
    # i + 1 intervals after the request; a slow reader can only see it later.
    assert all(at >= 0.25 * (i + 1) - 0.01 for i, (at, _) in enumerate(parts))
    assert parts[-1][0] >= 0.25 * pieces - 0.01
    assert response.headers["Content-Length"] == str(len(content))
    assert "Content-Encoding" not in response.headers
    assert response.headers["X-Upstream"] == "stub"


def test_trickle_reframes_a_chunked_body_by_content_length(
    proxy: FaultProxy, http: httpx.Client
) -> None:
    proxy.add("GET", "/v1/chunked", Trickle(1000, 0.0))
    response = http.get("/v1/chunked")
    assert response.json() == BODY
    assert "Transfer-Encoding" not in response.headers
    assert response.headers["Content-Length"] == str(len(json.dumps(BODY)))


def test_trickle_stops_when_the_scenario_ends(proxy: FaultProxy) -> None:
    proxy.add("GET", "/v1/datasets", Trickle(1, 10.0))
    with socket.create_connection(_address(proxy)) as client:
        client.sendall(b"GET /v1/datasets HTTP/1.1\r\nHost: x\r\n\r\n")
        _wait_until(lambda: len(proxy.responses) == 1)
        began = time.monotonic()
        proxy.clear()
        client.settimeout(2.0)
        received = b""
        while chunk := client.recv(4096):
            received += chunk
        assert time.monotonic() - began < 1.0
        assert received.startswith(b"HTTP/1.1 200 OK\r\n")
        assert received.endswith(b"\r\n\r\n")  # the headers, and none of the body


def test_rewrite_changes_the_json_body(proxy: FaultProxy, http: httpx.Client) -> None:
    def change(body: dict[str, Any]) -> dict[str, Any]:
        body["position"] = 38
        body["data"][0]["value"] = 102.1
        return body

    proxy.add("GET", "/v1/gzip", Rewrite(change))
    response = http.get("/v1/gzip")
    assert response.status_code == 200
    assert response.json() == {**BODY, "position": 38, "data": [{"value": 102.1}]}
    assert response.headers["Content-Length"] == str(len(response.content))
    assert "Content-Encoding" not in response.headers
    assert response.headers["X-Upstream"] == "stub"


def test_a_rewrite_that_fails_is_answered_with_502(
    proxy: FaultProxy, http: httpx.Client
) -> None:
    def change(body: dict[str, Any]) -> dict[str, Any]:
        raise KeyError("missing")

    proxy.add("GET", "/v1/series", Rewrite(change))
    response = http.get("/v1/series")
    assert response.status_code == 502
    assert (
        response.text
        == "The fault proxy could not rewrite the body: KeyError('missing')"
    )


# Rules


def _statuses(http: httpx.Client, path: str, count: int) -> list[int]:
    return [http.get(path).status_code for _ in range(count)]


@pytest.mark.parametrize(
    ("requests", "statuses"),
    [
        (nth(2), [200, 503, 200, 200]),
        (from_nth(2), [200, 503, 503, 503]),
        (EVERY, [503, 503, 503, 503]),
        (Requests(2, 3), [200, 503, 503, 200]),
    ],
)
def test_a_rule_applies_to_the_requests_it_selects(
    proxy: FaultProxy, http: httpx.Client, requests: Requests, statuses: list[int]
) -> None:
    proxy.add("GET", "/v1/datasets", Respond(503), requests)
    assert _statuses(http, "/v1/datasets", 4) == statuses


def test_a_rule_counts_from_when_it_was_added(
    proxy: FaultProxy, http: httpx.Client
) -> None:
    assert _statuses(http, "/v1/datasets", 2) == [200, 200]
    proxy.add("GET", "/v1/datasets", Respond(503), nth(1))
    assert _statuses(http, "/v1/datasets", 2) == [503, 200]


def test_a_rule_matches_by_method_and_exact_path(
    proxy: FaultProxy, http: httpx.Client
) -> None:
    proxy.add("GET", "/v1/series", Respond(503))
    assert http.get("/v1/series?dataset_id=x").status_code == 503
    assert http.post("/v1/series").status_code == 200
    assert http.get("/v1/series/x").status_code == 200
    assert http.get("/v1/series/").status_code == 200
    assert http.get("/v1/serie").status_code == 200


def test_every_matching_rule_counts_and_the_first_added_acts(
    proxy: FaultProxy, http: httpx.Client
) -> None:
    proxy.add("GET", "/v1/series/a", Respond(404), nth(1))
    proxy.add("GET", "/v1/series/a", Problem(400, "something_new"), nth(2))
    proxy.add("GET", "/v1/series/a", Respond(503), from_nth(2))
    assert _statuses(http, "/v1/series/a", 4) == [404, 400, 503, 503]


def test_clearing_the_rules_keeps_the_records(
    proxy: FaultProxy, http: httpx.Client
) -> None:
    proxy.add("GET", "/v1/datasets", Respond(503))
    assert http.get("/v1/datasets").status_code == 503
    proxy.clear_rules()
    assert http.get("/v1/datasets").status_code == 200
    assert [r.status for r in proxy.responses] == [503, 200]


def test_clear_removes_the_rules_and_the_records(
    proxy: FaultProxy, http: httpx.Client
) -> None:
    proxy.add("GET", "/v1/datasets", Respond(503))
    http.get("/v1/datasets")
    http.close()
    _wait_until(lambda: all(c.closed is not None for c in proxy.connections))
    proxy.clear()
    assert (proxy.requests, proxy.responses, proxy.connections) == ((), (), ())
    with httpx.Client(base_url=proxy.url, trust_env=False) as again:
        assert again.get("/v1/datasets").status_code == 200


# Records


def test_requests_responses_and_connections_are_recorded(
    proxy: FaultProxy, http: httpx.Client
) -> None:
    proxy.add("GET", "/v1/datasets", Respond(503, {"Request-Id": "req_1"}))
    began = time.monotonic()
    http.get("/v1/datasets?a=1%2B2&b=x+y&c", headers={"Authorization": "Bearer k"})
    http.get("/v1/series")
    ended = time.monotonic()
    first, second = proxy.requests
    assert (first.method, first.path) == ("GET", "/v1/datasets")
    assert first.query == (("a", "1+2"), ("b", "x+y"), ("c", ""))
    assert first.header("authorization") == "Bearer k"
    assert first.headers[0] == ("Host", proxy.url.removeprefix("http://"))
    assert began <= first.arrived <= second.arrived <= ended
    assert first.connection == second.connection == 0
    responses = proxy.responses
    assert [(r.status, r.request) for r in responses] == [(503, 0), (200, 1)]
    assert ("Request-Id", "req_1") in responses[0].headers
    assert first.arrived <= responses[0].sent <= second.arrived
    (connection,) = proxy.connections
    assert connection.number == 0
    assert began <= connection.opened <= first.arrived
    assert connection.closed is None
    http.close()
    _wait_until(lambda: proxy.connections[0].closed is not None)
    closed = proxy.connections[0].closed
    assert closed is not None
    assert closed >= ended


def test_connections_are_numbered_in_the_order_accepted(proxy: FaultProxy) -> None:
    for _ in range(3):
        with httpx.Client(base_url=proxy.url, trust_env=False) as http:
            http.get("/v1/meta")
    assert [r.connection for r in proxy.requests] == [0, 1, 2]
    assert [c.number for c in proxy.connections] == [0, 1, 2]


# Retry-After dates


def test_an_http_date_is_the_proxys_time_ahead_truncated_to_a_second() -> None:
    now = datetime(2026, 10, 1, 0, 0, 0, 999999, tzinfo=UTC)
    assert HTTPDate(2).written(now) == "Thu, 01 Oct 2026 00:00:02 GMT"
    assert HTTPDate(30).written(now) == "Thu, 01 Oct 2026 00:00:30 GMT"
    assert HTTPDate(0.5).written(now) == "Thu, 01 Oct 2026 00:00:01 GMT"


def test_an_http_date_header_is_written_when_the_proxy_responds(
    proxy: FaultProxy, http: httpx.Client
) -> None:
    proxy.add(
        "GET",
        "/v1/series/activity-index",
        Problem(429, "rate_limited", headers={"Retry-After": http_date(2)}),
    )
    time.sleep(0.3)  # the date is not the time the rule was added
    before = datetime.now(UTC).timestamp()
    value = http.get("/v1/series/activity-index").headers["Retry-After"]
    after = datetime.now(UTC).timestamp()
    assert re.fullmatch(
        r"[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT", value
    )
    date = parsedate_to_datetime(value).timestamp()
    assert before + 1 < date <= after + 2


# Stopping


def test_stop_closes_open_connections_and_stops_serving(stub: _StubServer) -> None:
    proxy = FaultProxy(_url(stub))
    proxy.start()
    address = _address(proxy)
    proxy.add("GET", "/v1/datasets", Stall())
    with (
        socket.create_connection(address) as stalled,
        socket.create_connection(address) as idle,
    ):
        stalled.sendall(b"GET /v1/datasets HTTP/1.1\r\nHost: x\r\n\r\n")
        # Both accepted, not waiting in the listening socket's backlog.
        _wait_until(lambda: len(proxy.requests) == 1 and len(proxy.connections) == 2)
        began = time.monotonic()
        proxy.stop()
        assert time.monotonic() - began < 2.0
        for client in (stalled, idle):
            client.settimeout(2.0)
            assert client.recv(1) == b""
    with pytest.raises(OSError):
        socket.create_connection(address, timeout=1.0).close()
    assert not [t for t in threading.enumerate() if t.name == "fault-proxy"]


def _address(proxy: FaultProxy) -> tuple[str, int]:
    host, _, port = proxy.url.removeprefix("http://").partition(":")
    return host, int(port)


def _wait_until(condition: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("the condition did not hold in time")
        time.sleep(0.01)
