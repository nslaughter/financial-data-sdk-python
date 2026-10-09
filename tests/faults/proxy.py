"""The fault proxy: a local HTTP/1.1 server in front of the API that injects faults.

D3 places throttling and transient failures here, because the API defines
neither. `spec/conformance.md` defines the proxy under "The fault proxy".
It listens on `127.0.0.1` and serves from its own thread, one thread per
connection. It forwards each request to the API unchanged and returns the
API's response unchanged, unless a rule matches the request.

A rule matches by method and exact path, then by which of its matching
requests it applies to: request _n_ only (`nth(n)`), request _n_ and every
later one (`from_nth(n)`), or every request (`EVERY`). Each rule counts the
requests that match its method and path from when it was added, whether or
not it applies to them. When several rules apply to a request, the one
added first acts.

The proxy records every request it receives, every response it returns,
and when each connection it accepts opens and closes, all on
`time.monotonic()`. `clear()` starts a scenario: it removes the rules and
the records and ends any stall or trickle still running.
"""

from __future__ import annotations

import contextlib
import json
import select
import socket
import socketserver
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from tests.support.query import parse_query

_HOP_BY_HOP: Final = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
"""Request headers that describe one connection, so are not forwarded."""
_NOT_FORWARDED: Final = _HOP_BY_HOP | {"host", "content-length"}
"""Request headers httpx writes again for the request to the API."""
_REFRAMED: Final = frozenset(
    {"content-length", "transfer-encoding", "content-encoding"}
)
"""Response headers replaced when the proxy sends a body of its own framing."""


# Rules


@dataclass(frozen=True, slots=True)
class Requests:
    """Which of a rule's matching requests it applies to, counted from 1."""

    first: int
    last: int | None
    """The last request it applies to, or `None` for every later one."""

    def __contains__(self, number: object) -> bool:
        return (
            isinstance(number, int)
            and number >= self.first
            and (self.last is None or number <= self.last)
        )


def nth(n: int) -> Requests:
    """Request `n` only."""
    return Requests(n, n)


def from_nth(n: int) -> Requests:
    """Request `n` and every later one."""
    return Requests(n, None)


EVERY: Final = Requests(1, None)
"""Every request."""


@dataclass(frozen=True, slots=True)
class HTTPDate:
    """A header value written when the proxy responds: an HTTP date ahead of it.

    The date is the proxy's own UTC time plus `seconds`, truncated to a
    whole second, in IMF-fixdate form, so the wait it asks for is more than
    `seconds` - 1 and at most `seconds`.
    """

    seconds: float

    def written(self, now: datetime) -> str:
        instant = (now + timedelta(seconds=self.seconds)).replace(microsecond=0)
        return format_datetime(instant, usegmt=True)


def http_date(seconds: float) -> HTTPDate:
    """A `Retry-After` HTTP date `seconds` ahead of the proxy's time."""
    return HTTPDate(seconds)


HeaderValue = str | HTTPDate


@dataclass(frozen=True, slots=True)
class Respond:
    """Answer with this status, headers, and body, without forwarding."""

    status: int
    headers: Mapping[str, HeaderValue] = field(default_factory=dict)
    body: bytes | str = b""


@dataclass(frozen=True, slots=True)
class Problem:
    """Answer with a problem response, without forwarding.

    The body is `{"status": <status>, "code": <code>, "title": "Injected
    fault", "detail": "Injected by the fault proxy.", "parameter":
    <parameter or null>}`, as `application/problem+json`, with any given
    headers.
    """

    status: int
    code: str
    parameter: str | None = None
    headers: Mapping[str, HeaderValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Drop:
    """Read the request, then close the connection without a response."""


@dataclass(frozen=True, slots=True)
class Stall:
    """Read the request and send nothing.

    The connection stays silent until the client closes it or the scenario
    ends.
    """


@dataclass(frozen=True, slots=True)
class Trickle:
    """Forward, then send the API's body `size` bytes at a time.

    The API's status and headers go at once. Each part of the body waits
    `interval` seconds first. The body is the API's content, decoded, and
    framed by `Content-Length` without `Transfer-Encoding` or
    `Content-Encoding`, so that httpx returns each part as it arrives.
    """

    size: int
    interval: float


@dataclass(frozen=True, slots=True)
class Rewrite:
    """Forward, then answer with the API's response, its JSON body changed.

    `change` takes the parsed body and returns the body to send, which is
    serialized again. The API's status and headers are kept, with
    `Content-Length` for the new body.
    """

    change: Callable[[Any], Any]


Action = Respond | Problem | Drop | Stall | Trickle | Rewrite


@dataclass(slots=True)
class _Rule:
    method: str
    path: str
    action: Action
    requests: Requests
    matched: int = 0


# Records


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """A request the proxy received."""

    method: str
    path: str
    """The path as received, percent-encoded, without the query."""
    query: tuple[tuple[str, str], ...]
    """Each query parameter, decoded, in order."""
    headers: tuple[tuple[str, str], ...]
    """Each header as received, in order."""
    arrived: float
    """When its request line arrived, on `time.monotonic()`."""
    connection: int
    """The number of the connection it came on."""

    def header(self, name: str) -> str | None:
        """Return the first value of a header, by a name in any case."""
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None


@dataclass(frozen=True, slots=True)
class RecordedResponse:
    """A response the proxy returned: its status and headers as sent."""

    status: int
    headers: tuple[tuple[str, str], ...]
    sent: float
    """When its headers were sent, on `time.monotonic()`."""
    request: int
    """The index of its request among the recorded requests."""


@dataclass(frozen=True, slots=True)
class RecordedConnection:
    """A connection the proxy accepted, numbered from 0."""

    number: int
    opened: float
    closed: float | None
    """When the proxy closed it, or `None` while it is open."""


# The server


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    proxy: FaultProxy

    def server_bind(self) -> None:
        # HTTPServer looks up the host's fully qualified name here, which
        # can wait on DNS; the proxy has no use for it.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server
    number: int

    def setup(self) -> None:
        super().setup()
        self.number = self.server.proxy._opened(self.connection)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.server.proxy._closed(self.number, self.connection)

    def handle_one_request(self) -> None:
        try:
            self.raw_requestline = self.rfile.readline(65537)
        except OSError:
            self.close_connection = True
            return
        if not self.raw_requestline:
            self.close_connection = True
            return
        arrived = time.monotonic()
        if len(self.raw_requestline) > 65536:
            self.send_error(414)
            return
        if not self.parse_request():
            return
        try:
            self.server.proxy._serve(self, arrived)
            self.wfile.flush()
        except OSError:  # the client went away
            self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:
        pass


class FaultProxy:
    """A fault proxy in front of the API at `upstream`.

    Use it as a context manager, or call `start()` and `stop()`. `url` is
    where clients reach it.
    """

    def __init__(self, upstream: str, *, timeout: float = 30.0) -> None:
        self.upstream = upstream.rstrip("/")
        self._prefix = urlsplit(self.upstream).path.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout, follow_redirects=False, trust_env=False
        )
        # A request is forwarded with its own headers, and no others.
        self._client.headers.clear()
        self._lock = threading.Lock()
        self._rules: list[_Rule] = []
        self._requests: list[RecordedRequest] = []
        self._responses: list[RecordedResponse] = []
        self._connections: list[RecordedConnection] = []
        self._sockets: dict[int, socket.socket] = {}
        self._accepted = 0
        self._stopping = False
        self._release = threading.Event()
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.proxy = self
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="fault-proxy",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """End every stall and trickle, close every connection, and stop serving."""
        with self._lock:
            self._stopping = True
            self._release.set()
            sockets = list(self._sockets.values())
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join()
            self._thread = None
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        self._server.server_close()
        self._client.close()

    def __enter__(self) -> FaultProxy:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    # Rules and records

    def add(
        self, method: str, path: str, action: Action, requests: Requests = EVERY
    ) -> None:
        """Add a rule. Its count of matching requests starts now."""
        with self._lock:
            self._rules.append(_Rule(method, path, action, requests))

    def clear_rules(self) -> None:
        with self._lock:
            self._rules.clear()

    def clear(self) -> None:
        """Start a scenario: clear the rules and records, end stalls and trickles."""
        with self._lock:
            self._rules.clear()
            self._requests.clear()
            self._responses.clear()
            self._connections.clear()
            released, self._release = self._release, threading.Event()
        released.set()

    @property
    def requests(self) -> tuple[RecordedRequest, ...]:
        with self._lock:
            return tuple(self._requests)

    @property
    def responses(self) -> tuple[RecordedResponse, ...]:
        with self._lock:
            return tuple(self._responses)

    @property
    def connections(self) -> tuple[RecordedConnection, ...]:
        """The connections accepted since `clear()`."""
        with self._lock:
            return tuple(self._connections)

    # Connections

    def _opened(self, sock: socket.socket) -> int:
        with self._lock:
            number = self._accepted
            self._accepted += 1
            self._sockets[number] = sock
            self._connections.append(RecordedConnection(number, time.monotonic(), None))
            if self._stopping:
                # Accepted as the proxy stops, after it closed the others.
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
            return number

    def _closed(self, number: int, sock: socket.socket) -> None:
        now = time.monotonic()
        with self._lock:
            self._sockets.pop(number, None)
            self._connections = [
                RecordedConnection(c.number, c.opened, now) if c.number == number else c
                for c in self._connections
            ]

    # Serving

    def _serve(self, handler: _Handler, arrived: float) -> None:
        method = handler.command
        target = handler.path
        path, _, query = target.partition("?")
        headers = tuple(handler.headers.items())
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length > 0 else b""
        with self._lock:
            number = len(self._requests)
            self._requests.append(
                RecordedRequest(
                    method,
                    path,
                    parse_query(query),
                    headers,
                    arrived,
                    handler.number,
                )
            )
            action = self._choose(method, path)
            release = self._release
        if action is None:
            self._forward(handler, number, method, target, headers, body)
        elif isinstance(action, Respond):
            content = (
                action.body.encode() if isinstance(action.body, str) else action.body
            )
            self._write(handler, number, action.status, None, action.headers, content)
        elif isinstance(action, Problem):
            document = {
                "status": action.status,
                "code": action.code,
                "title": "Injected fault",
                "detail": "Injected by the fault proxy.",
                "parameter": action.parameter,
            }
            self._write(
                handler,
                number,
                action.status,
                None,
                {"Content-Type": "application/problem+json", **action.headers},
                json.dumps(document).encode(),
            )
        elif isinstance(action, Drop):
            handler.close_connection = True
        elif isinstance(action, Stall):
            self._stall(handler, release)
        elif isinstance(action, Trickle):
            self._trickle(
                handler, number, method, target, headers, body, action, release
            )
        else:
            self._rewrite(handler, number, method, target, headers, body, action)

    def _choose(self, method: str, path: str) -> Action | None:
        """Count the request on every rule it matches; return the first that applies."""
        chosen = None
        for rule in self._rules:
            if rule.method == method and rule.path == path:
                rule.matched += 1
                if chosen is None and rule.matched in rule.requests:
                    chosen = rule.action
        return chosen

    def _upstream(
        self,
        method: str,
        target: str,
        headers: tuple[tuple[str, str], ...],
        body: bytes,
        *,
        decoded: bool,
    ) -> tuple[httpx.Response, bytes]:
        """Send a request to the API, and read its whole response.

        The body is returned raw, as the API sent it, or `decoded` from its
        `Content-Encoding`.
        """
        # http.server decodes the request line as Latin-1, so encoding it back
        # sends the target's bytes as they arrived.
        url = httpx.URL(self.upstream).copy_with(
            raw_path=(self._prefix + target).encode("latin-1")
        )
        forwarded = [
            (name, value)
            for name, value in headers
            if name.lower() not in _NOT_FORWARDED
        ]
        request = self._client.build_request(
            method, url, headers=forwarded, content=body or None
        )
        response = self._client.send(request, stream=True)
        try:
            content = response.read() if decoded else b"".join(response.iter_raw())
        finally:
            response.close()
        return response, content

    def _failed(self, handler: _Handler, number: int, reason: str) -> None:
        """Answer 502 when the proxy cannot act, so a scenario fails visibly."""
        self._write(
            handler,
            number,
            502,
            None,
            {"Content-Type": "text/plain"},
            f"The fault proxy {reason}".encode(),
        )

    def _forward(
        self,
        handler: _Handler,
        number: int,
        method: str,
        target: str,
        headers: tuple[tuple[str, str], ...],
        body: bytes,
    ) -> None:
        try:
            response, content = self._upstream(
                method, target, headers, body, decoded=False
            )
        except httpx.HTTPError as error:
            self._failed(handler, number, f"could not reach the API: {error!r}")
            return
        raw = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in response.headers.raw
        ]
        self._write(
            handler,
            number,
            response.status_code,
            response.reason_phrase,
            raw,
            content,
            framed=True,
        )

    def _trickle(
        self,
        handler: _Handler,
        number: int,
        method: str,
        target: str,
        headers: tuple[tuple[str, str], ...],
        body: bytes,
        action: Trickle,
        release: threading.Event,
    ) -> None:
        try:
            response, content = self._upstream(
                method, target, headers, body, decoded=True
            )
        except httpx.HTTPError as error:
            self._failed(handler, number, f"could not reach the API: {error!r}")
            return
        sent = self._reframed(response, content)
        self._head(handler, number, response.status_code, response.reason_phrase, sent)
        for offset in range(0, len(content), action.size):
            if release.wait(action.interval):
                handler.close_connection = True
                return
            handler.wfile.write(content[offset : offset + action.size])
            handler.wfile.flush()

    def _rewrite(
        self,
        handler: _Handler,
        number: int,
        method: str,
        target: str,
        headers: tuple[tuple[str, str], ...],
        body: bytes,
        action: Rewrite,
    ) -> None:
        try:
            response, original = self._upstream(
                method, target, headers, body, decoded=True
            )
        except httpx.HTTPError as error:
            self._failed(handler, number, f"could not reach the API: {error!r}")
            return
        try:
            content = json.dumps(action.change(json.loads(original))).encode()
        except Exception as error:  # report it to the client, not to stderr
            self._failed(handler, number, f"could not rewrite the body: {error!r}")
            return
        sent = self._reframed(response, content)
        self._head(handler, number, response.status_code, response.reason_phrase, sent)
        handler.wfile.write(content)

    @staticmethod
    def _reframed(response: httpx.Response, content: bytes) -> list[tuple[str, str]]:
        """The API's headers, framing `content` by `Content-Length` alone."""
        kept = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in response.headers.raw
            if name.decode("latin-1").lower() not in _REFRAMED
        ]
        return [*kept, ("Content-Length", str(len(content)))]

    def _head(
        self,
        handler: _Handler,
        number: int,
        status: int,
        reason: str | None,
        headers: list[tuple[str, str]],
    ) -> None:
        """Send a status line and headers, and record them as a response."""
        handler.send_response_only(status, reason or None)
        for name, value in headers:
            handler.send_header(name, value)
        handler.end_headers()
        with self._lock:
            self._responses.append(
                RecordedResponse(status, tuple(headers), time.monotonic(), number)
            )

    def _write(
        self,
        handler: _Handler,
        number: int,
        status: int,
        reason: str | None,
        headers: Mapping[str, HeaderValue] | list[tuple[str, str]],
        body: bytes,
        *,
        framed: bool = False,
    ) -> None:
        """Send a whole response.

        Unless `framed`, `Content-Length` is added when the headers have
        none. With `framed`, the headers are the API's: a body the API sent
        chunked is sent as one chunk, and one the API delimited by closing
        the connection is delimited the same way.
        """
        now = datetime.now(UTC)
        items = headers.items() if isinstance(headers, Mapping) else headers
        written = [
            (name, value.written(now) if isinstance(value, HTTPDate) else value)
            for name, value in items
        ]
        names = {name.lower() for name, _ in written}
        bodiless = (
            handler.command == "HEAD" or status in (204, 304) or 100 <= status < 200
        )
        chunked = framed and "transfer-encoding" in names
        if not framed and "content-length" not in names and not bodiless:
            written.append(("Content-Length", str(len(body))))
        elif framed and not chunked and "content-length" not in names:
            handler.close_connection = True
        self._head(handler, number, status, reason, written)
        if bodiless:
            return
        if chunked:
            if body:
                handler.wfile.write(f"{len(body):X}\r\n".encode() + body + b"\r\n")
            handler.wfile.write(b"0\r\n\r\n")
        else:
            handler.wfile.write(body)

    @staticmethod
    def _stall(handler: _Handler, release: threading.Event) -> None:
        sock = handler.connection
        while not release.is_set():
            readable, _, _ = select.select([sock], [], [], 0.05)
            if readable:
                try:
                    if not sock.recv(65536):
                        break  # the client closed the connection
                except OSError:
                    break
        handler.close_connection = True
