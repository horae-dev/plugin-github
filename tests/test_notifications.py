# SPDX-License-Identifier: Apache-2.0
"""`github.notifications`: read only, bounded, validated; what the recordings do not show."""

import json
from typing import Any

import anyio
import pytest
from horae_sdk import Plugin
from horae_sdk.tasks import is_task_link
from horae_sdk.testing import Replayed
from horae_sdk.testing import call as call_tool

from conftest import NOTIFICATIONS_TOKEN, TOKEN, WALL, FakeNotifications
from horae_github import build
from horae_github.client import GitHubError
from horae_github.http import Response
from horae_github.notifications import NotificationsClient
from horae_github.plugin import parse_notification


def run(plugin: Plugin, **params: Any) -> Replayed:
    async def go() -> Replayed:
        return await call_tool(plugin, "github.notifications", params)

    return anyio.run(go)


def test_only_get_requests_and_never_a_write(
    plugin: Plugin, notifications: FakeNotifications
) -> None:
    """Read state is not modified: no PATCH on a thread, no PUT on the list, no POST."""
    run(plugin, today="2026-10-30")
    run(plugin, today="2026-10-30", since="2026-10-01T00:00:00Z")
    assert notifications.methods
    assert set(notifications.methods) == {"GET"}
    assert all(path.startswith("/notifications?") for path in notifications.paths)
    assert not any("/threads" in path for path in notifications.paths)
    # the fake has no `post`: a POST would raise AttributeError
    assert not hasattr(notifications, "post")


def test_query_is_participating_unread_and_bounded(
    plugin: Plugin, notifications: FakeNotifications
) -> None:
    run(plugin, today="2026-10-30", max_items=200)
    assert notifications.paths == ["/notifications?all=false&participating=true&per_page=50&page=1"]


def test_default_reasons_and_mapping(plugin: Plugin) -> None:
    result = run(plugin, today="2026-10-30")
    assert result.error is None
    data = result.data
    assert [(i["repo"], i["number"], i["reason"]) for i in data["items"]] == [
        ("acme-org/api", 7, "review_requested"),
        ("acme-org/api", 12, "mention"),
        ("acme-org/widgets", 5, "assign"),
        ("other-org/tools", 21, "team_mention"),
    ]
    # subscribed -> filtered; Release, Discussion, CheckSuite, wrong host, type/URL
    # disagreement and a dot segment -> skipped
    assert (data["filtered"], data["skipped"]) == (1, 6)
    assert data["poll_interval"] == 60
    assert data["rate_limit_remaining"] == 4990
    assert data["truncated"] is False
    assert data["items"][0]["fingerprint"].startswith("github:pr:acme-org-api:7::")
    assert data["items"][1]["fingerprint"].startswith("github:issue:acme-org-api:12::")
    titles = [i["title"] for i in result.intents]
    assert titles[0].startswith("Review acme-org/api\\#7")
    assert titles[2].startswith("Work on acme-org/widgets\\#5")
    assert titles[3].startswith("Answer mention in other-org/tools\\#21")
    assert [i["due"] for i in result.intents] == ["2026-11-01"] * 4  # 2 days after the run's day


def test_links_come_from_the_validated_url_only(plugin: Plugin) -> None:
    for intent in run(plugin, today="2026-10-30").intents:
        assert intent["link"].startswith("https://github.com/")
        assert is_task_link(intent["link"])


def test_hostile_title_is_neutralised(plugin: Plugin) -> None:
    result = run(plugin, today="2026-10-30")
    title = result.intents[1]["title"]
    assert "‮" not in title
    assert "](https://evil.example)" not in title
    assert "📅" not in title.replace("\\📅", "") or "\\📅" in title


@pytest.mark.parametrize(
    "thread",
    [
        None,
        [],
        {"reason": "mention"},
        {
            "reason": 3,
            "subject": {"type": "Issue", "url": "https://api.github.com/repos/a/b/issues/1"},
        },
        {
            "reason": "mention",
            "subject": {"type": "Issue", "url": "https://api.github.com/repos/a/b/issues/0"},
        },
        {
            "reason": "mention",
            "subject": {"type": "Issue", "url": "https://api.github.com/repos/a/b/issues/1?x=1"},
        },
        {
            "reason": "mention",
            "subject": {"type": "Issue", "url": "http://api.github.com/repos/a/b/issues/1"},
        },
        {
            "reason": "mention",
            "subject": {
                "type": "Issue",
                "url": "https://api.github.com/repos/a/b/issues/99999999999",
            },
        },
        {
            "reason": "mention",
            "subject": {"type": "Issue", "url": "https://api.github.com/repos/a%2Fb/c/issues/1"},
        },
        {
            "reason": "mention",
            "subject": {"type": "Issue", "url": "https://api.github.com/repos/./b/issues/1"},
        },
        {
            "reason": "mention",
            "subject": {"type": "Issue", "url": "https://api.github.com/repos/a/b/pulls/1"},
        },
        {
            "reason": "mention",
            "subject": {"type": "Release", "url": "https://api.github.com/repos/a/b/issues/1"},
        },
    ],
)
def test_malformed_threads_are_skipped(thread: object) -> None:
    assert parse_notification(thread) is None


def test_well_formed_thread_parses() -> None:
    parsed = parse_notification(
        {
            "reason": "mention",
            "subject": {
                "type": "PullRequest",
                "title": 5,
                "url": "https://api.github.com/repos/a/b.c/pulls/1",
            },
        }
    )
    assert parsed is not None
    item, reason = parsed
    assert (item.url, item.title, reason) == ("https://github.com/a/b.c/pull/1", "", "mention")


def test_filters(plugin: Plugin) -> None:
    result = run(
        plugin, today="2026-10-30", reasons=["mention", "team_mention"], orgs=["Other-Org"]
    )
    assert [i["repo"] for i in result.data["items"]] == ["other-org/tools"]
    result = run(plugin, today="2026-10-30", repos=["acme-org/widgets"])
    assert [i["number"] for i in result.data["items"]] == [5]
    result = run(plugin, today="2026-10-30", exclude_repos=["acme-org/api", "acme-org/widgets"])
    assert [i["repo"] for i in result.data["items"]] == ["other-org/tools"]
    result = run(plugin, today="2026-10-30", due_in_days=None)
    assert all(i["due"] is None for i in result.data["items"])


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"reasons": []}, "github: invalid reasons filter"),
        ({"reasons": ["nope"]}, "github: invalid reasons filter"),
        ({"since": "yesterday"}, "github: since must be YYYY-MM-DDTHH:MM:SSZ"),
        ({"max_items": 0}, "github: max_items is 1 to 200"),
        ({"orgs": ["bad org"]}, "github: invalid org filter"),
        ({"due_in_days": 400}, "github: due offsets are 0 to 365 days"),
    ],
)
def test_bad_parameters(plugin: Plugin, params: dict[str, Any], message: str) -> None:
    assert run(plugin, today="2026-10-30", **params).error == message


def test_secret_is_separate_and_required(notifications: FakeNotifications) -> None:
    plugin = build(environ={"GITHUB_TOKEN": TOKEN})
    assert run(plugin).error == "github: GITHUB_NOTIFICATIONS_TOKEN is not set"
    assert notifications.paths == []


def test_declared_secrets() -> None:
    from horae_github.plugin import PERMISSIONS

    assert sorted(PERMISSIONS.secrets) == ["GITHUB_NOTIFICATIONS_TOKEN", "GITHUB_TOKEN"]


class Scripted:
    def __init__(self, *answers: Response) -> None:
        self.answers = list(answers)
        self.paths: list[str] = []

    def get(self, path: str, headers: Any, *, deadline: float) -> Response:
        self.paths.append(path)
        return self.answers.pop(0)


def client(transport: Scripted, **kwargs: Any) -> NotificationsClient:
    return NotificationsClient(
        NOTIFICATIONS_TOKEN,
        transport,  # type: ignore[arg-type]
        sleep=lambda _s: None,
        wall=lambda: WALL,
        **kwargs,
    )


def body(n: int) -> bytes:
    return json.dumps([{"id": str(i)} for i in range(n)]).encode()


NEXT = {"link": '<https://api.github.com/notifications?page=2>; rel="next"'}


def test_pages_are_bounded_by_max_items() -> None:
    transport = Scripted(Response(200, NEXT, body(50)), Response(200, NEXT, body(50)))
    found = client(transport).participating(max_items=100)
    assert len(found.threads) == 100
    assert found.truncated
    assert len(transport.paths) == 2  # ceil(100 / 50), never the page the Link header names


def test_stops_at_zero_remaining_rate_limit() -> None:
    transport = Scripted(Response(200, {**NEXT, "x-ratelimit-remaining": "0"}, body(50)))
    found = client(transport).participating(max_items=200)
    assert found.truncated
    assert found.rate_limit_remaining == 0
    assert len(transport.paths) == 1


def test_last_page_without_next_is_complete() -> None:
    transport = Scripted(Response(200, {"x-poll-interval": "999999999"}, body(3)))
    found = client(transport).participating(max_items=200)
    assert not found.truncated
    assert found.poll_interval == 86400  # capped, reported, never slept


def test_deadline_between_pages_returns_what_was_fetched() -> None:
    now = [0.0]

    class Slow(Scripted):
        def get(self, path: str, headers: Any, *, deadline: float) -> Response:
            now[0] = 24.8  # the first page used up the call's budget
            return super().get(path, headers, deadline=deadline)

    transport = Slow(Response(200, NEXT, body(50)))
    found = client(transport, clock=lambda: now[0], budget=25.0).participating(max_items=200)
    assert found.truncated
    assert len(found.threads) == 50
    assert len(transport.paths) == 1


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (
            Response(401, {}, b"{}"),
            "github: authentication failed (GITHUB_NOTIFICATIONS_TOKEN refused)",
        ),
        (Response(403, {"retry-after": "30"}, b"{}"), "github: rate limited; retry after 30 s"),
        (Response(429, {}, b"{}"), "github: rate limited; retry later"),
        (
            Response(403, {}, b'{"message":"You have exceeded a secondary rate limit"}'),
            "github: possibly rate limited; retry later",
        ),
        (
            Response(403, {}, b'{"message":"Resource not accessible"}'),
            "github: access denied (HTTP 403)",
        ),
        (Response(304, {}, b""), "github: unexpected redirect (HTTP 304), not followed"),
        (Response(200, {}, b"{}"), "github: malformed response"),
        (Response(200, {}, b"\xff"), "github: malformed response"),
    ],
)
def test_failures_have_fixed_messages(answer: Response, message: str) -> None:
    with pytest.raises(GitHubError) as raised:
        client(Scripted(answer)).participating(max_items=50)
    assert str(raised.value) == message
    assert NOTIFICATIONS_TOKEN not in repr(client(Scripted()))


def test_gateway_error_is_retried_once() -> None:
    transport = Scripted(Response(502, {}, b""), Response(200, {}, body(1)))
    assert len(client(transport).participating(max_items=50).threads) == 1
