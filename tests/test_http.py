# SPDX-License-Identifier: Apache-2.0
"""The bounded transport, against a fake server on 127.0.0.1 (plain HTTP: the only change
from production is the connection factory; the plugin always uses `https_connection`)."""

import http.client
import json
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from conftest import TOKEN
from horae_github.client import Client, GitHubError, RateLimited
from horae_github.http import HOST, PORT, Transport, TransportError, https_connection


class Server:
    def __init__(self) -> None:
        self.answers: list[tuple[int, dict[str, str], bytes]] = []
        self.seen: list[dict[str, Any]] = []
        self.delay = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                server.seen.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": dict(self.headers.items()),
                        "body": json.loads(self.rfile.read(length)),
                    }
                )
                status, headers, body = server.answers.pop(0)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                if "Content-Length" not in headers:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body == b"SLOW":
                    server.delay.wait(2)
                    return
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True)
        self.thread.start()

    def transport(self, **kwargs: Any) -> Transport:
        def connect(host: str, port: int, timeout: float) -> http.client.HTTPConnection:
            assert (host, port) == (HOST, PORT)  # the transport only ever targets the API
            return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

        return Transport(connect=connect, **kwargs)

    def close(self) -> None:
        self.delay.set()
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server() -> Iterator[Server]:
    running = Server()
    yield running
    running.close()


def page(*numbers: int, cursor: str | None = None) -> bytes:
    nodes = [
        {
            "__typename": "PullRequest",
            "id": f"PR_{n}",
            "number": n,
            "title": f"PR {n}",
            "repository": {"nameWithOwner": "acme-org/api"},
            "milestone": None,
        }
        for n in numbers
    ]
    info = {"hasNextPage": cursor is not None, "endCursor": cursor}
    return json.dumps({"data": {"search": {"pageInfo": info, "nodes": nodes}}}).encode()


def client(server: Server, **kwargs: Any) -> Client:
    return Client(TOKEN, server.transport(**kwargs), sleep=lambda _s: None, wall=lambda: 1000.0)


def test_production_connection_is_verifying_https() -> None:
    connection = https_connection(HOST, PORT, 5.0)
    assert isinstance(connection, http.client.HTTPSConnection)
    context = connection._context  # type: ignore[attr-defined]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert getattr(connection, "_tunnel_host", None) is None  # no proxy tunnel
    transport = Transport()
    assert (transport.host, transport.port, transport.connect) == (HOST, PORT, https_connection)


def test_request_shape_and_pagination(server: Server) -> None:
    server.answers += [(200, {}, page(1, 2, cursor="c1")), (200, {}, page(3))]
    found = client(server).search("is:open review-requested:@me", max_items=60)
    assert [node["number"] for node in found.nodes] == [1, 2, 3]
    assert not found.truncated
    first, second = server.seen
    assert first["method"] == "POST"
    assert first["path"] == "/graphql"
    assert first["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert first["headers"]["User-Agent"] == "horae-plugin-github"
    assert first["body"]["variables"] == {
        "q": "is:open review-requested:@me",
        "first": 50,
        "after": None,
    }
    assert second["body"]["variables"]["after"] == "c1"
    assert second["body"]["variables"]["first"] == 50


def test_pages_are_bounded_by_max_items(server: Server) -> None:
    server.answers += [(200, {}, page(1, 2, cursor="c1"))]
    found = client(server).search("q", max_items=2)
    assert found.truncated
    assert len(server.seen) == 1
    assert server.seen[0]["body"]["variables"]["first"] == 2


def test_an_empty_page_with_a_next_cursor_stops(server: Server) -> None:
    server.answers += [(200, {}, page(cursor="c1"))]
    assert client(server).search("q", max_items=10).truncated


def test_auth_failure_never_shows_the_token(server: Server) -> None:
    server.answers += [(401, {}, json.dumps({"message": f"Bad credentials {TOKEN}"}).encode())]
    with pytest.raises(GitHubError) as raised:
        client(server).search("q", max_items=5)
    assert str(raised.value) == "github: authentication failed (GITHUB_TOKEN refused)"
    assert TOKEN not in repr(raised.value)
    assert TOKEN not in repr(client(server))


@pytest.mark.parametrize(
    ("status", "headers", "message"),
    [
        (403, {"Retry-After": "61"}, "github: rate limited; retry after 61 s"),
        (429, {}, "github: rate limited"),
        (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1300"}, "retry after 300 s"),
        (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "900"}, "retry after 0 s"),
        (403, {"Retry-After": "999999"}, "retry after 3600 s"),
        (403, {}, "github: access denied (HTTP 403)"),
        (301, {"Location": "https://evil.example/"}, "unexpected redirect (HTTP 301)"),
        (418, {}, "github: unexpected answer (HTTP 418)"),
    ],
)
def test_error_answers(server: Server, status: int, headers: dict[str, str], message: str) -> None:
    server.answers += [(status, headers, b"{}")]
    with pytest.raises(GitHubError, match=message.replace("(", r"\(").replace(")", r"\)")):
        client(server).search("q", max_items=5)
    assert len(server.seen) == 1  # never retried, never redirected


def test_rate_limit_is_reported_not_slept_through(server: Server) -> None:
    slept: list[float] = []
    server.answers += [(403, {"Retry-After": "120"}, b"{}")]
    with pytest.raises(RateLimited) as raised:
        Client(TOKEN, server.transport(), sleep=slept.append).search("q", max_items=5)
    assert raised.value.retry_after == 120
    assert slept == []


def test_one_retry_on_a_gateway_error(server: Server) -> None:
    slept: list[float] = []
    server.answers += [(502, {}, b"bad gateway"), (200, {}, page(1))]
    found = Client(TOKEN, server.transport(), sleep=slept.append).search("q", max_items=5)
    assert [node["number"] for node in found.nodes] == [1]
    assert slept == [1.0]


def test_a_second_gateway_error_fails(server: Server) -> None:
    server.answers += [(503, {}, b""), (503, {}, b"")]
    with pytest.raises(GitHubError, match=r"service unavailable \(HTTP 503\)"):
        client(server).search("q", max_items=5)
    assert len(server.seen) == 2


@pytest.mark.parametrize(
    "body",
    [b"not json", b"\xff\xfe", b"[]", b'{"data": {"search": {"nodes": "x"}}}', b"{}"],
)
def test_malformed_answers(server: Server, body: bytes) -> None:
    server.answers += [(200, {}, body)]
    with pytest.raises(GitHubError, match="github: malformed response"):
        client(server).search("q", max_items=5)


def test_graphql_errors(server: Server) -> None:
    errors = [{"type": "FORBIDDEN", "message": "x"}, {"type": "<script>", "message": "y"}]
    server.answers += [(200, {}, json.dumps({"errors": errors}).encode())]
    with pytest.raises(GitHubError, match=r"^github: query failed \(FORBIDDEN\)$"):
        client(server).search("q", max_items=5)
    server.answers += [(200, {}, json.dumps({"errors": "boom"}).encode())]
    with pytest.raises(GitHubError, match=r"query failed \(unknown error\)"):
        client(server).search("q", max_items=5)


def test_a_declared_oversized_answer_is_refused(server: Server) -> None:
    server.answers += [(200, {}, b"x" * 2048)]
    with pytest.raises(GitHubError, match="response too large"):
        client(server, max_bytes=1024).search("q", max_items=5)


def test_a_streamed_oversized_answer_is_refused(server: Server) -> None:
    server.answers += [(200, {"Content-Length": "999999999999999999999x"}, b"y" * 2048)]
    with pytest.raises(GitHubError, match="response too large"):
        client(server, max_bytes=1024).search("q", max_items=5)


def test_a_slow_answer_times_out(server: Server) -> None:
    server.answers += [(200, {"Content-Length": "100"}, b"SLOW")]
    with pytest.raises(GitHubError, match="github: request timed out"):
        client(server, timeout=0.2).search("q", max_items=5)


def test_the_total_deadline(server: Server) -> None:
    ticks = iter([0.0, 100.0])
    server.answers += [(200, {}, page(1))]
    with pytest.raises(TransportError, match="timed out"):
        server.transport(clock=lambda: next(ticks)).post("/graphql", b"{}", {})


def test_connection_refused() -> None:
    def connect(host: str, port: int, timeout: float) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", 9, timeout=timeout)

    with pytest.raises(GitHubError, match=r"^github: connection failed \(\w+\)$"):
        Client(TOKEN, Transport(connect=connect)).search("q", max_items=5)


def test_a_connection_dropped_mid_answer(server: Server) -> None:
    server.answers += [(200, {"Content-Length": "100"}, b"short")]
    server.delay.set()
    with pytest.raises(GitHubError, match=r"connection failed \(IncompleteRead\)"):
        client(server).search("q", max_items=5)
