# SPDX-License-Identifier: Apache-2.0
"""A bounded HTTPS client for exactly one host, `api.github.com` (the declared network host).

Built on `http.client`, not `urllib` or `httpx`, on purpose:

- **no ambient proxy**: `http.client` never reads `HTTPS_PROXY` / `ALL_PROXY` / `.netrc`, so
  the only connection is the one opened here. Connections are direct today; from M5 the core's
  egress proxy is the network, and this transport will have to go through the proxy the host
  provides once M5 defines how;
- **https only**: the connection is an `HTTPSConnection` with the default, verifying TLS
  context (certificate and host name checked);
- **no redirect is followed**: a 3xx is an answer like any other, which the client refuses;
- **bounded**: every request has an absolute deadline (the caller's, shared by all the pages
  and retries of one call). Before each socket operation the timeout is set to the smaller of
  `TIMEOUT_S` and the time left; the body is read with `read1` (at most one `recv` per read),
  and a watchdog shuts the socket down at the deadline, so an answer dripped one byte at a
  time, in its headers or its body, cannot outlive it. The name is resolved in a thread that
  is waited for at most the time left (`resolve`). The body is capped (a larger
  `Content-Length` is refused before reading).

Errors are `TransportError` with a fixed message naming the failure, never a header, a URL with
a query or a body (so never the token).
"""

import contextlib
import http.client
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

HOST = "api.github.com"
PORT = 443
TIMEOUT_S = 10.0
"""Each socket operation (connect, send, every read), never more than the time left."""
CALL_DEADLINE_S = 25.0
"""One whole call to a source, every page and retry included. The host's per-call timeout is
30 s, so the plugin always answers first."""
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
CHUNK = 64 * 1024
MAX_ADDRESSES = 4
"""Addresses tried, in the resolver's order, within one connect timeout."""
DIGITS_RE = re.compile(r"[0-9]{1,30}")
"""ASCII digits only (`str.isdigit` also accepts `٣` and `²`)."""

Connect = Callable[[str, int, float], http.client.HTTPConnection]


class TransportError(Exception):
    """The request failed before an HTTP answer was read (fixed message)."""


class DeadlineExceeded(TransportError):
    """The call's deadline passed: the caller may keep what earlier pages returned."""

    def __init__(self) -> None:
        super().__init__("github: request timed out")


def digits(value: str | None) -> int | None:
    """`value` as a non-negative integer when it is ASCII digits only, else `None`."""
    if value is None or not DIGITS_RE.fullmatch(value.strip()):
        return None
    return int(value.strip())


def resolve(host: str, port: int, timeout: float) -> list[tuple[Any, ...]]:
    """`getaddrinfo` in a daemon thread, waited for at most `timeout` (the call has no timeout
    of its own). On a timeout the thread is left to finish on its own; it holds no socket."""
    found: list[list[tuple[Any, ...]]] = []
    failed: list[OSError] = []

    def run() -> None:
        try:
            found.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError as error:
            failed.append(error)

    worker = threading.Thread(target=run, name="horae-github-resolve", daemon=True)
    worker.start()
    worker.join(timeout)
    if found:
        return found[0]
    if failed:
        raise failed[0]
    raise TimeoutError("name resolution timed out")


class VerifiedConnection(http.client.HTTPSConnection):
    """`HTTPSConnection` whose `connect` resolves with `resolve` (bounded) and tries at most
    `MAX_ADDRESSES` addresses within its timeout; TLS is verified for the host name."""

    def __init__(self, host: str, port: int, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self.verify_context = context

    def connect(self) -> None:
        budget = self.timeout if isinstance(self.timeout, int | float) else TIMEOUT_S
        stop = time.monotonic() + budget
        addresses = resolve(self.host, self.port, budget)
        last: OSError = OSError("no address")
        for family, kind, proto, _name, address in addresses[:MAX_ADDRESSES]:
            left = stop - time.monotonic()
            if left <= 0:
                raise TimeoutError("connect timed out")
            sock = socket.socket(family, kind, proto)
            try:
                sock.settimeout(left)
                sock.connect(address)
                self.sock = self.verify_context.wrap_socket(sock, server_hostname=self.host)
            except OSError as error:
                sock.close()
                last = error
                continue
            return
        raise last


def https_connection(host: str, port: int, timeout: float) -> http.client.HTTPConnection:
    """A verifying TLS connection (`ssl.create_default_context`): no proxy, no tunnel."""
    return VerifiedConnection(host, port, timeout, ssl.create_default_context())


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    """Lower-case names."""
    body: bytes


def _shut(sock: socket.socket) -> None:
    """Unblock a read in progress: shut the descriptor down (the base method, so a TLS socket
    does not touch its TLS state from this thread)."""
    with contextlib.suppress(OSError):
        socket.socket.shutdown(sock, socket.SHUT_RDWR)


class Transport:
    """POST requests to one host, bounded. `connect` is replaceable for tests only (a plain
    connection to a fake server on 127.0.0.1); the plugin always uses `https_connection`."""

    def __init__(
        self,
        host: str = HOST,
        port: int = PORT,
        *,
        connect: Connect = https_connection,
        timeout: float = TIMEOUT_S,
        max_bytes: int = MAX_RESPONSE_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host, self.port = host, port
        self.connect, self.timeout = connect, timeout
        self.max_bytes, self.clock = max_bytes, clock

    def post(
        self,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
        *,
        deadline: float | None = None,
    ) -> Response:
        """One request, finished before `deadline` (on `clock`; default: `CALL_DEADLINE_S`
        from now) or `DeadlineExceeded`."""
        end = self.clock() + CALL_DEADLINE_S if deadline is None else deadline
        expired = threading.Event()

        def left() -> float:
            remaining = end - self.clock()
            if remaining <= 0 or expired.is_set():
                raise DeadlineExceeded()
            return min(self.timeout, remaining)

        def failed(error: BaseException) -> TransportError:
            if expired.is_set() or self.clock() >= end:
                return DeadlineExceeded()
            if isinstance(error, TimeoutError):
                return TransportError("github: request timed out")
            return TransportError(f"github: connection failed ({type(error).__name__})")

        connection = self.connect(self.host, self.port, left())
        try:
            try:
                connection.connect()
            except (OSError, http.client.HTTPException) as error:
                raise failed(error) from None
            sock = connection.sock
            watchdog = threading.Timer(max(0.0, end - self.clock()), self._expire, (sock, expired))
            watchdog.daemon = True
            watchdog.start()
            try:
                return self._exchange(connection, sock, path, body, headers, left)
            except (OSError, http.client.HTTPException) as error:
                raise failed(error) from None
            finally:
                watchdog.cancel()
        finally:
            connection.close()

    @staticmethod
    def _expire(sock: socket.socket, expired: threading.Event) -> None:
        expired.set()
        _shut(sock)

    def _exchange(
        self,
        connection: http.client.HTTPConnection,
        sock: socket.socket,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
        left: Callable[[], float],
    ) -> Response:
        sock.settimeout(left())
        connection.request("POST", path, body=body, headers=dict(headers))
        sock.settimeout(left())
        answer = connection.getresponse()
        length = digits(answer.getheader("content-length"))
        if length is not None and length > self.max_bytes:
            raise TransportError("github: response too large")
        chunks: list[bytes] = []
        size = 0
        while not answer.isclosed():  # closed once the declared length is read, or at EOF
            sock.settimeout(left())
            chunk = answer.read1(CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > self.max_bytes:
                raise TransportError("github: response too large")
            chunks.append(chunk)
        left()
        if length is not None and size != length:
            raise TransportError("github: connection failed (IncompleteRead)")
        names = {name.lower(): value for name, value in answer.getheaders()}
        return Response(answer.status, names, b"".join(chunks))
