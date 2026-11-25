# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures: a fake GitHub answering from synthetic data (never real accounts).

`FakeTransport` stands where the HTTPS connection to api.github.com would be: it answers each
GraphQL request from `tests/fixtures/graphql.json`, `ci.json` and `updates.json`, chosen by the
request's variables (the search string and page cursor, or the owner or repository) or, for
a request without variables, by its document (`viewer { login }`), so the
client's parsing, pagination and error handling all run."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from horae_sdk import Plugin

from horae_github import build
from horae_github.client import Client
from horae_github.http import Response
from horae_github.notifications import NotificationsClient

HERE = Path(__file__).parent
TOKEN = "synthetic-test-token-0123456789"  # noqa: S105 (not a real token)
NOW = datetime(2026, 10, 30, 21, 30, tzinfo=UTC)  # Friday evening UTC, Saturday in Tokyo
WALL = 1793350500.0  # X-RateLimit-Reset in the fixtures is 300 s later


class FakeTransport:
    def __init__(self, fixtures: list[dict[str, Any]]) -> None:
        self.fixtures = fixtures
        self.requests: list[dict[str, Any]] = []

    def post(
        self, path: str, body: bytes, headers: Mapping[str, str], *, deadline: float
    ) -> Response:
        assert path == "/graphql"
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        request = json.loads(body)
        self.requests.append(request)
        variables = request["variables"]
        for fixture in self.fixtures:
            if "query" in fixture:  # a request without variables, chosen by its document
                if fixture["query"] not in request["query"]:
                    continue
                return Response(
                    fixture["status"], fixture["headers"], json.dumps(fixture["body"]).encode()
                )
            wanted = fixture.get("vars") or {"q": fixture["q"], "after": fixture["after"]}
            if all(key in variables and variables[key] == v for key, v in wanted.items()):
                return Response(
                    fixture["status"], fixture["headers"], json.dumps(fixture["body"]).encode()
                )
        raise AssertionError(f"no fixture for {variables!r}")


NOTIFICATIONS_TOKEN = "synthetic-classic-token-9876543210"  # noqa: S105 (not a real token)


class FakeNotifications:
    """Answers `GET /notifications?...` from `tests/fixtures/notifications.json` by exact path.
    It has no `post`, and records every method so tests can assert only GETs happen."""

    def __init__(self) -> None:
        self.fixtures: list[dict[str, Any]] = json.loads(
            (HERE / "fixtures" / "notifications.json").read_text("utf-8")
        )
        self.paths: list[str] = []
        self.methods: list[str] = []

    def get(self, path: str, headers: Mapping[str, str], *, deadline: float) -> Response:
        self.methods.append("GET")
        assert headers["Authorization"] == f"Bearer {NOTIFICATIONS_TOKEN}"
        assert "Content-Type" not in headers
        self.paths.append(path)
        for fixture in self.fixtures:
            if fixture["path"] == path:
                return Response(
                    fixture["status"], fixture["headers"], json.dumps(fixture["body"]).encode()
                )
        raise AssertionError(f"no fixture for {path!r}")


def load_fixtures() -> list[dict[str, Any]]:
    fixtures: list[dict[str, Any]] = []
    for name in ("graphql.json", "ci.json", "updates.json"):
        fixtures += json.loads((HERE / "fixtures" / name).read_text("utf-8"))
    return fixtures


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport(load_fixtures())


@pytest.fixture
def notifications() -> FakeNotifications:
    return FakeNotifications()


@pytest.fixture
def plugin(transport: FakeTransport, notifications: FakeNotifications) -> Plugin:
    def client(token: str) -> Client:
        return Client(token, transport, sleep=lambda _s: None, wall=lambda: WALL)  # type: ignore[arg-type]

    def notifier(token: str) -> NotificationsClient:
        return NotificationsClient(
            token,
            notifications,  # type: ignore[arg-type]
            sleep=lambda _s: None,
            wall=lambda: WALL,
        )

    return build(
        client,
        notifications_client=notifier,
        clock=lambda: NOW,
        environ={
            "GITHUB_TOKEN": TOKEN,
            "GITHUB_NOTIFICATIONS_TOKEN": NOTIFICATIONS_TOKEN,
            "TZ": "Europe/Paris",
        },
    )
