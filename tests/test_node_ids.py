# SPDX-License-Identifier: Apache-2.0
"""`github.notifications` node id lookup (#8): one batched GraphQL read per 50 items, only with
the search token, falling back to the repository/number fingerprint otherwise."""

import json
from collections.abc import Mapping
from typing import Any

import anyio
import pytest
from horae_sdk import Plugin
from horae_sdk.testing import Replayed
from horae_sdk.testing import call as call_tool

from conftest import NOTIFICATIONS_TOKEN, NOW, TOKEN, WALL, FakeNotifications, FakeTransport
from horae_github import build, nodeids
from horae_github.client import Client
from horae_github.http import DeadlineExceeded, Response
from horae_github.notifications import NotificationsClient
from horae_github.plugin import parse

SINCE = "2026-05-01T00:00:00Z"


def run(target: Plugin, **params: Any) -> Replayed:
    async def go() -> Replayed:
        return await call_tool(target, "github.notifications", {"today": "2026-10-30", **params})

    return anyio.run(go)


def lookups(transport: FakeTransport) -> list[dict[str, Any]]:
    return [r for r in transport.requests if "issueOrPullRequest" in r["query"]]


def make(
    transport: Any, notifications: FakeNotifications, environ: Mapping[str, str] | None = None
) -> Plugin:
    return build(
        lambda token: Client(token, transport, sleep=lambda _s: None, wall=lambda: WALL),
        notifications_client=lambda token: NotificationsClient(
            token,
            notifications,  # type: ignore[arg-type]
            sleep=lambda _s: None,
            wall=lambda: WALL,
        ),
        clock=lambda: NOW,
        environ=environ
        or {"GITHUB_TOKEN": TOKEN, "GITHUB_NOTIFICATIONS_TOKEN": NOTIFICATIONS_TOKEN},
    )


def test_one_batched_query_resolves_every_item(plugin: Plugin, transport: FakeTransport) -> None:
    result = run(plugin, since=SINCE)
    assert result.error is None
    sent = lookups(transport)
    assert len(sent) == 1  # four items, one query
    assert sent[0]["variables"] == {
        "o0": "acme-org", "n0": "api", "i0": 50,
        "o1": "acme-org", "n1": "api", "i1": 51,
        "o2": "acme-org", "n2": "api", "i2": 52,
        "o3": "acme-org", "n3": "api", "i3": 54,
    }  # fmt: skip
    # names and numbers are variables: nothing from the answer is in the document
    assert "acme-org" not in sent[0]["query"]
    assert result.data["node_ids"] == {"resolved": 2, "fallback": 2}


def test_a_resolved_item_has_the_fingerprint_the_search_sources_give(plugin: Plugin) -> None:
    node = {
        "__typename": "PullRequest",
        "id": "PR_kwDOAcmeApi50",
        "number": 50,
        "title": "Resolved pull request",
        "repository": {"nameWithOwner": "acme-org/api"},
    }
    searched = parse(node)
    assert searched is not None
    notified = run(plugin, since=SINCE).data["items"][0]
    assert notified["fingerprint"] == searched.fingerprint
    assert notified["fingerprint"] == "github:pr:PR_kwDOAcmeApi50"


def test_the_answer_of_the_other_type_keeps_the_fallback(plugin: Plugin) -> None:
    items = {i["number"]: i["fingerprint"] for i in run(plugin, since=SINCE).data["items"]}
    assert items[52].startswith("github:pr:acme-org-api:52::")  # the node is an issue
    assert items[54].startswith("github:issue:acme-org-api:54::")  # not visible: NOT_FOUND


def test_without_the_search_token_nothing_is_looked_up(
    transport: FakeTransport, notifications: FakeNotifications
) -> None:
    target = make(transport, notifications, {"GITHUB_NOTIFICATIONS_TOKEN": NOTIFICATIONS_TOKEN})
    result = run(target, since=SINCE)
    assert result.error is None
    assert transport.requests == []
    assert result.data["node_ids"] == {"resolved": 0, "fallback": 4}
    assert all("acme-org-api" in i["fingerprint"] for i in result.data["items"])


def test_node_ids_false_looks_nothing_up(plugin: Plugin, transport: FakeTransport) -> None:
    result = run(plugin, since=SINCE, node_ids=False)
    assert transport.requests == []
    assert result.data["node_ids"] == {"resolved": 0, "fallback": 4}


def test_no_items_no_lookup(plugin: Plugin, transport: FakeTransport) -> None:
    run(plugin, since="2026-06-01T00:00:00Z")
    assert transport.requests == []


class Failing:
    """Answers every lookup with `answer`, or raises."""

    def __init__(self, answer: Response | None) -> None:
        self.answer = answer
        self.calls = 0

    def post(
        self, path: str, body: bytes, headers: Mapping[str, str], *, deadline: float
    ) -> Response:
        self.calls += 1
        if self.answer is None:
            raise DeadlineExceeded()
        return self.answer


@pytest.mark.parametrize(
    "answer",
    [
        None,
        Response(502, {}, b"bad gateway"),
        Response(401, {}, b'{"message": "Bad credentials"}'),
        Response(403, {"retry-after": "30"}, b""),
        Response(200, {}, b"not json"),
        Response(200, {}, b'{"data": null, "errors": [{"type": "RATE_LIMITED"}]}'),
        Response(200, {}, b'{"data": [], "errors": []}'),
        Response(200, {}, b'{"data": {"r0": {"issueOrPullRequest": {"id": "bad id!"}}}}'),
    ],
)
def test_a_failing_lookup_never_fails_the_call(
    notifications: FakeNotifications, answer: Response | None
) -> None:
    transport = Failing(answer)
    result = run(make(transport, notifications), since=SINCE)
    assert result.error is None
    assert len(result.data["items"]) == 4
    assert result.data["node_ids"]["resolved"] == 0
    assert all(":acme-org-api:" in i["fingerprint"] for i in result.data["items"])
    assert TOKEN not in json.dumps(result.data)


def refs(count: int) -> list[nodeids.Ref]:
    return [(f"acme-org/r{n}", n + 1, "pr" if n % 2 else "issue") for n in range(count)]


class Echo:
    """Resolves every alias; records the queries."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def post(
        self, path: str, body: bytes, headers: Mapping[str, str], *, deadline: float
    ) -> Response:
        request = json.loads(body)
        self.requests.append(request)
        variables = request["variables"]
        data = {}
        for n in range(len(variables) // 3):
            typename = (
                "PullRequest" if variables[f"n{n}"].endswith(("1", "3", "5", "7", "9")) else "Issue"
            )
            data[f"r{n}"] = {
                "issueOrPullRequest": {"__typename": typename, "id": f"ID_{variables[f'n{n}']}"}
            }
        return Response(200, {}, json.dumps({"data": data}).encode())


def test_batches_hold_at_most_fifty_aliases() -> None:
    echo = Echo()
    found = nodeids.resolve(Client(TOKEN, echo), refs(120))  # type: ignore[arg-type]
    assert [len(r["variables"]) // 3 for r in echo.requests] == [50, 50, 20]
    assert all(
        r["query"].count("issueOrPullRequest") == len(r["variables"]) // 3 for r in echo.requests
    )
    assert len(found.ids) == 120
    assert found.failed == 0


def test_a_repeated_ref_is_asked_once() -> None:
    echo = Echo()
    nodeids.resolve(Client(TOKEN, echo), refs(3) + refs(3))  # type: ignore[arg-type]
    assert [len(r["variables"]) // 3 for r in echo.requests] == [3]


def test_a_later_batch_failing_keeps_the_earlier_ids() -> None:
    class SecondFails(Echo):
        def post(
            self, path: str, body: bytes, headers: Mapping[str, str], *, deadline: float
        ) -> Response:
            if self.requests:
                self.requests.append({})
                return Response(502, {}, b"")
            return super().post(path, body, headers, deadline=deadline)

    found = nodeids.resolve(Client(TOKEN, SecondFails(), sleep=lambda _s: None), refs(60))  # type: ignore[arg-type]
    assert len(found.ids) == 50
    assert found.failed == 10


def test_no_batch_starts_without_time_left() -> None:
    echo = Echo()
    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0])
    client = Client(TOKEN, echo, clock=lambda: next(ticks))  # type: ignore[arg-type]
    found = nodeids.resolve(client, refs(60))
    assert len(echo.requests) == 1
    assert found.failed == 10
