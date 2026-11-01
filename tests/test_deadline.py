# SPDX-License-Identifier: Apache-2.0
"""One deadline per call, against a real server on 127.0.0.1 that drips its answer one byte at
a time (each byte well within the per-operation timeout, so only the deadline can stop it)."""

import http.client
import json
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from conftest import TOKEN
from horae_github import http as transport_module
from horae_github.client import Client, GitHubError
from horae_github.http import (
    HOST,
    PORT,
    DeadlineExceeded,
    Transport,
    TransportError,
    VerifiedConnection,
    resolve,
)

DRIP_S = 0.02
TIMEOUT_S = 0.3
"""Per socket operation: far longer than one drip."""


def page(*numbers: int, cursor: str | None = None) -> bytes:
    nodes = [
        {
            "__typename": "Issue",
            "id": f"I_{n}",
            "number": n,
            "title": f"Issue {n}",
            "repository": {"nameWithOwner": "acme-org/api"},
        }
        for n in numbers
    ]
    info = {"hasNextPage": cursor is not None, "endCursor": cursor}
    return json.dumps({"data": {"search": {"pageInfo": info, "nodes": nodes}}}).encode()


def head(body: bytes) -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
    )


class Dripping:
    """Answers each connection with the next script: `(answer, drip_from)`, the bytes before
    `drip_from` sent at once and the rest one byte every `DRIP_S`."""

    def __init__(self) -> None:
        self.scripts: list[tuple[bytes, int]] = []
        self.stop = threading.Event()
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(0.05)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except TimeoutError:
                continue
            with conn:
                conn.settimeout(2)
                request = b""
                while b"\r\n\r\n" not in request:
                    request += conn.recv(65536)
                answer, drip_from = self.scripts.pop(0)
                try:
                    conn.sendall(answer[:drip_from])
                    for byte in answer[drip_from:]:
                        if self.stop.is_set():
                            break
                        time.sleep(DRIP_S)
                        conn.sendall(bytes([byte]))
                except OSError:
                    pass  # the client gave up: what the test wants

    def transport(self) -> Transport:
        def connect(host: str, port: int, timeout: float) -> http.client.HTTPConnection:
            assert (host, port) == (HOST, PORT)
            return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

        return Transport(connect=connect, timeout=TIMEOUT_S)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5)
        self.listener.close()


@pytest.fixture
def drip() -> Iterator[Dripping]:
    server = Dripping()
    yield server
    server.close()


def timed(budget: float, call: Any) -> float:
    started = time.monotonic()
    call(time.monotonic() + budget)
    return time.monotonic() - started


def test_dripped_headers_cannot_outlive_the_deadline(drip: Dripping) -> None:
    body = page(1)
    drip.scripts.append((head(body) + body, 0))  # ~100 header bytes: about 2 s
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded, match="github: request timed out"):
        drip.transport().post("/graphql", b"{}", {}, deadline=time.monotonic() + 0.5)
    assert time.monotonic() - started < 1.0


def test_a_dripped_body_cannot_outlive_the_deadline(drip: Dripping) -> None:
    body = page(1, 2, 3)
    drip.scripts.append((head(body) + body, len(head(body)) + 5))
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        drip.transport().post("/graphql", b"{}", {}, deadline=time.monotonic() + 0.5)
    assert time.monotonic() - started < 1.0


def test_a_short_drip_within_the_deadline_is_read(drip: Dripping) -> None:
    body = b'{"data": null}'
    drip.scripts.append((head(body) + body, len(head(body))))
    answer = drip.transport().post("/graphql", b"{}", {}, deadline=time.monotonic() + 5)
    assert answer.body == body


def test_out_of_time_after_a_page_returns_what_was_fetched(drip: Dripping) -> None:
    first, second = page(1, 2, cursor="c1"), page(3)
    drip.scripts += [(head(first) + first, len(head(first) + first)), (head(second) + second, 0)]
    client = Client(TOKEN, drip.transport(), sleep=lambda _s: None, budget=1.0)
    started = time.monotonic()
    found = client.search(["q"], max_items=50)
    assert time.monotonic() - started < 1.5
    assert found.truncated
    assert [node["number"] for node in found.nodes] == [1, 2]


def test_out_of_time_on_the_first_page_fails(drip: Dripping) -> None:
    body = page(1)
    drip.scripts.append((head(body) + body, 0))
    client = Client(TOKEN, drip.transport(), sleep=lambda _s: None, budget=0.5)
    with pytest.raises(GitHubError, match=r"^github: request timed out$"):
        client.search(["q"], max_items=50)


def test_no_page_is_started_without_time_left(drip: Dripping) -> None:
    first = page(1, cursor="c1")
    drip.scripts.append((head(first) + first, len(head(first) + first)))
    calls: list[float] = []

    def clock() -> float:  # the deadline, the retry check, then 29.8 s later
        calls.append(0)
        return time.monotonic() + (29.8 if len(calls) > 2 else 0.0)

    client = Client(TOKEN, drip.transport(), clock=clock, budget=30.0)
    found = client.search(["q"], max_items=50)
    assert found.truncated
    assert [node["number"] for node in found.nodes] == [1]


def test_resolution_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def slow(*_args: Any, **_kwargs: Any) -> list[Any]:
        release.wait(5)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        resolve(HOST, PORT, 0.2)
    assert time.monotonic() - started < 1.0
    release.set()


def test_resolution_errors_and_answers() -> None:
    assert resolve("127.0.0.1", 443, 5)[0][4] == ("127.0.0.1", 443)
    with pytest.raises(socket.gaierror):
        resolve("no such host.invalid", 443, 5)


def test_verified_connection_tries_each_address(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = socket.create_server(("127.0.0.1", 0))
    refused = closed.getsockname()
    closed.close()
    plain = socket.create_server(("127.0.0.1", 0))  # accepts, then speaks no TLS

    def serve() -> None:
        conn, _ = plain.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    addresses = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", refused),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", plain.getsockname()),
    ]
    monkeypatch.setattr(transport_module, "resolve", lambda *_a: addresses)
    connection = VerifiedConnection(HOST, PORT, 2.0, ssl.create_default_context())
    with pytest.raises(ssl.SSLError):
        connection.connect()
    plain.close()
    monkeypatch.setattr(transport_module, "resolve", lambda *_a: [])
    with pytest.raises(OSError, match="no address"):
        VerifiedConnection(HOST, PORT, 2.0, ssl.create_default_context()).connect()


def test_a_connect_failure_is_a_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport_module, "resolve", lambda *_a: [])
    with pytest.raises(TransportError, match=r"^github: connection failed \(OSError\)$"):
        Transport().post("/graphql", b"{}", {}, deadline=time.monotonic() + 5)
