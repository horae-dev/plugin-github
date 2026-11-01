# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures: a fake GitHub answering from synthetic data (never real accounts).

`FakeTransport` stands where the HTTPS connection to api.github.com would be: it answers each
GraphQL request from `tests/fixtures/graphql.json`, chosen by the search string and the page
cursor the request carries, so the client's parsing, pagination and error handling all run."""

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
            if fixture["q"] == variables["q"] and fixture["after"] == variables["after"]:
                return Response(
                    fixture["status"], fixture["headers"], json.dumps(fixture["body"]).encode()
                )
        raise AssertionError(f"no fixture for {variables['q']!r} after {variables['after']!r}")


def load_fixtures() -> list[dict[str, Any]]:
    fixtures: list[dict[str, Any]] = json.loads(
        (HERE / "fixtures" / "graphql.json").read_text("utf-8")
    )
    return fixtures


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport(load_fixtures())


@pytest.fixture
def plugin(transport: FakeTransport) -> Plugin:
    def client(token: str) -> Client:
        return Client(token, transport, sleep=lambda _s: None, wall=lambda: WALL)  # type: ignore[arg-type]

    return build(client, clock=lambda: NOW, environ={"GITHUB_TOKEN": TOKEN, "TZ": "Europe/Paris"})
