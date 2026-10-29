# SPDX-License-Identifier: Apache-2.0
"""A bounded HTTPS client for exactly one host, `api.github.com` (the declared network host).

Built on `http.client`, not `urllib` or `httpx`, on purpose:

- **no ambient proxy**: `http.client` never reads `HTTPS_PROXY` / `ALL_PROXY` / `.netrc`, so
  the only connection is the one opened here (from M5 the core's egress proxy is the network);
- **https only**: the connection is an `HTTPSConnection` with the default, verifying TLS
  context (certificate and host name checked);
- **no redirect is followed**: a 3xx is an answer like any other, which the client refuses;
- **bounded**: a timeout on every socket operation, a total deadline per request and a cap on
  the response body (a larger `Content-Length` is refused before reading).

Errors are `TransportError` with a fixed message naming the failure, never a header, a URL with
a query or a body (so never the token).
"""

import http.client
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

HOST = "api.github.com"
PORT = 443
TIMEOUT_S = 10.0
"""Each socket operation (connect, send, every read)."""
DEADLINE_S = 20.0
"""One whole request, connect to last byte. The host's per-call timeout is 30 s."""
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
CHUNK = 64 * 1024

Connect = Callable[[str, int, float], http.client.HTTPConnection]


def https_connection(host: str, port: int, timeout: float) -> http.client.HTTPConnection:
    """A verifying TLS connection (`ssl.create_default_context`): no proxy, no tunnel."""
    return http.client.HTTPSConnection(
        host, port, timeout=timeout, context=ssl.create_default_context()
    )


class TransportError(Exception):
    """The request failed before an HTTP answer was read (fixed message)."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    """Lower-case names."""
    body: bytes


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
        deadline: float = DEADLINE_S,
        max_bytes: int = MAX_RESPONSE_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host, self.port = host, port
        self.connect, self.timeout, self.deadline = connect, timeout, deadline
        self.max_bytes, self.clock = max_bytes, clock

    def post(self, path: str, body: bytes, headers: Mapping[str, str]) -> Response:
        started = self.clock()
        connection = self.connect(self.host, self.port, self.timeout)
        try:
            try:
                connection.request("POST", path, body=body, headers=dict(headers))
                answer = connection.getresponse()
            except TimeoutError:
                raise TransportError("github: request timed out") from None
            except (OSError, http.client.HTTPException) as error:
                raise TransportError(
                    f"github: connection failed ({type(error).__name__})"
                ) from None
            length = answer.getheader("content-length")
            if length is not None and length.isdigit() and int(length) > self.max_bytes:
                raise TransportError("github: response too large")
            chunks: list[bytes] = []
            size = 0
            try:
                while chunk := answer.read(CHUNK):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise TransportError("github: response too large")
                    if self.clock() - started > self.deadline:
                        raise TransportError("github: request timed out")
                    chunks.append(chunk)
            except TimeoutError:
                raise TransportError("github: request timed out") from None
            except (OSError, http.client.HTTPException) as error:
                raise TransportError(
                    f"github: connection failed ({type(error).__name__})"
                ) from None
            if length is not None and length.isdigit() and size != int(length):
                raise TransportError("github: connection failed (IncompleteRead)")
            names = {name.lower(): value for name, value in answer.getheaders()}
            return Response(answer.status, names, b"".join(chunks))
        finally:
            connection.close()
