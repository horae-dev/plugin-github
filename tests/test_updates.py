# SPDX-License-Identifier: Apache-2.0
"""`github.updates` beyond the recordings: the searches it sends, who counts as "other", the
deadline and the viewer read."""

import asyncio
import json
from collections.abc import Mapping
from datetime import date
from typing import Any

import pytest
from horae_sdk import Plugin
from horae_sdk.testing import call

from conftest import TOKEN, FakeTransport
from horae_github import updates
from horae_github.client import Client, GitHubError
from horae_github.http import DeadlineExceeded, Response

ME = "acme-me"


def run(plugin: Plugin, **params: Any) -> Any:
    return asyncio.run(call(plugin, "github.updates", {"today": "2026-10-30", **params}))


def searches(transport: FakeTransport) -> list[str]:
    return [r["variables"]["q"] for r in transport.requests if "q" in r["variables"]]


def test_one_search_per_owner_then_involves_me(plugin: Plugin, transport: FakeTransport) -> None:
    result = run(plugin, orgs=["acme-up"])
    assert result.error is None
    assert transport.requests[0]["query"] == updates.VIEWER_QUERY
    assert searches(transport) == [
        "is:open archived:false sort:updated-desc updated:>=2026-10-16 org:acme-up",
        "is:open archived:false sort:updated-desc updated:>=2026-10-16 org:acme-up",
        "involves:@me is:open archived:false sort:updated-desc updated:>=2026-10-16",
    ]
    # the involves:@me search found an item the owner search had: kept once
    numbers = [(i["repo"], i["number"]) for i in result.data["items"]]
    assert len(numbers) == len(set(numbers))
    assert ("acme-up/app", 1) in numbers


class Recorder:
    """Answers every search with nothing, the viewer with `acme-me`."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def post(
        self, path: str, body: bytes, headers: Mapping[str, str], *, deadline: float
    ) -> Response:
        variables = json.loads(body)["variables"]
        if "q" not in variables:
            document: dict[str, Any] = {"data": {"viewer": {"login": ME}}}
        else:
            self.queries.append(variables["q"])
            info = {"hasNextPage": False, "endCursor": None}
            document = {"data": {"search": {"pageInfo": info, "nodes": []}}}
        return Response(200, {}, json.dumps(document).encode())


def recorded(**params: Any) -> tuple[Any, list[str]]:
    from horae_github import build

    transport = Recorder()
    plugin = build(
        lambda token: Client(token, transport),  # type: ignore[arg-type]
        environ={"GITHUB_TOKEN": TOKEN},
    )
    return run(plugin, **params), transport.queries


def test_filters_and_options_reach_every_search() -> None:
    result, queries = recorded(
        users=["acme-user"],
        repos=["acme-org/api"],
        exclude_repos=["acme-org/old"],
        include_closed=True,
        since_days=0,
    )
    assert result.error is None
    head = "archived:false sort:updated-desc updated:>=2026-10-30"
    assert queries == [
        f"{head} user:acme-user -repo:acme-org/old",
        f"{head} repo:acme-org/api -repo:acme-org/old",
        f"involves:@me {head} -repo:acme-org/old",
    ]


def test_involves_me_off_reads_only_the_owners() -> None:
    _result, queries = recorded(orgs=["acme-org"], involves_me=False)
    assert queries == ["is:open archived:false sort:updated-desc updated:>=2026-10-16 org:acme-org"]


@pytest.mark.parametrize("value", [-1, 91, None])
def test_since_days_is_checked(value: Any) -> None:
    result, queries = recorded(orgs=["acme-org"], since_days=value)
    assert result.error is not None
    assert "since_days" in result.error
    assert queries == []


def actor(login: str | None, bot: bool = False) -> dict[str, str] | None:
    return None if login is None else {"__typename": "Bot" if bot else "User", "login": login}


def comment(n: int, login: str | None, day: int, bot: bool = False) -> dict[str, Any]:
    return {
        "__typename": "IssueComment",
        "id": f"IC_{n}",
        "createdAt": f"2026-10-{day:02d}T10:00:00Z",
        "author": actor(login, bot),
    }


def issue(author: str | None, timeline: list[Any], day: int = 17) -> dict[str, Any]:
    return {
        "__typename": "Issue",
        "id": "I_1",
        "number": 1,
        "title": "An issue",
        "createdAt": f"2026-10-{day:02d}T09:00:00Z",
        "author": actor(author),
        "repository": {"nameWithOwner": "acme-org/api"},
        "issueTimeline": {"nodes": timeline},
    }


def verdict(node: dict[str, Any], include_bots: bool = False) -> updates.Update | str:
    return updates.update(node, ME, "2026-10-16", include_bots)


def test_a_comment_by_someone_else_after_yours_is_new() -> None:
    found = verdict(issue("bob", [comment(1, ME, 20), comment(2, "bob", 21)]))
    assert isinstance(found, updates.Update)
    assert (found.last.kind, found.last.login, found.since) == (
        "commented",
        "bob",
        "2026-10-20T10:00:00Z",
    )


def test_your_comment_last_is_not_new() -> None:
    assert verdict(issue("bob", [comment(1, "bob", 20), comment(2, ME, 21)])) == "old"


def test_your_login_is_compared_without_case() -> None:
    assert verdict(issue("bob", [comment(1, "bob", 20), comment(2, "ACME-ME", 21)])) == "old"


def test_the_same_second_is_not_new() -> None:
    assert verdict(issue("bob", [comment(1, "bob", 20), comment(2, ME, 20)])) == "old"


def test_your_own_item_without_answers_is_not_new() -> None:
    assert verdict(issue(ME, [])) == "old"


def test_bot_activity_only_counts_with_include_bots() -> None:
    node = issue(ME, [comment(1, "renovate[bot]", 20), comment(2, "ci", 21, bot=True)])
    assert verdict(node) == "old"
    found = verdict(node, include_bots=True)
    assert isinstance(found, updates.Update)
    assert found.last.login == "ci"


def test_activity_before_the_window_is_not_new() -> None:
    assert verdict(issue("bob", [comment(1, "bob", 15)], day=1)) == "old"


def test_a_merge_wins_over_its_close() -> None:
    at = "2026-10-20T10:00:00Z"
    node = {
        **issue(ME, []),
        "__typename": "PullRequest",
        "prTimeline": {
            "nodes": [
                {"__typename": "MergedEvent", "id": "M", "createdAt": at, "actor": actor("bob")},
                {"__typename": "ClosedEvent", "id": "C", "createdAt": at, "actor": actor("bob")},
            ]
        },
    }
    found = verdict(node)
    assert isinstance(found, updates.Update)
    assert found.last.kind == "merged"
    assert found.url == "https://github.com/acme-org/api/pull/1"


def test_a_review_request_counts_only_when_it_asks_you() -> None:
    def requested(login: str) -> dict[str, Any]:
        return {
            "__typename": "ReviewRequestedEvent",
            "id": "RR",
            "createdAt": "2026-10-20T10:00:00Z",
            "actor": actor("bob"),
            "requestedReviewer": {"__typename": "User", "login": login},
        }

    node = {**issue(ME, []), "__typename": "PullRequest"}
    assert verdict({**node, "prTimeline": {"nodes": [requested("carol")]}}) == "old"
    found = verdict({**node, "prTimeline": {"nodes": [requested(ME)]}})
    assert isinstance(found, updates.Update)
    assert found.last.kind == "review_requested"


def test_a_pending_review_is_not_an_action() -> None:
    pending = {
        "__typename": "PullRequestReview",
        "id": "P",
        "state": "PENDING",
        "submittedAt": None,
        "author": actor(ME),
    }
    node = {**issue("bob", []), "__typename": "PullRequest", "prTimeline": {"nodes": [pending]}}
    found = verdict(node)
    assert isinstance(found, updates.Update)
    assert found.last.kind == "opened"


def test_a_commit_without_a_github_author_counts_as_a_bot() -> None:
    commit = {
        "__typename": "PullRequestCommit",
        "id": "PRC",
        "commit": {"committedDate": "2026-10-20T10:00:00Z", "author": {"user": None}},
    }
    node = {**issue(ME, []), "__typename": "PullRequest", "prTimeline": {"nodes": [commit]}}
    assert verdict(node) == "old"
    found = verdict(node, include_bots=True)
    assert isinstance(found, updates.Update)
    assert found.by == "someone"


@pytest.mark.parametrize("stamp", ["2026-10-20", "2026-13-40T10:00:00Z", "2026-10-20T10:00:00Z\n"])
def test_a_bad_time_drops_the_event(stamp: str) -> None:
    bad = {**comment(1, "bob", 20), "createdAt": stamp}
    assert verdict(issue(ME, [bad])) == "old"


class Timed:
    """The viewer answers; then every request runs out of time (or the viewer does)."""

    def __init__(self, viewer_times_out: bool = False, login: Any = ME) -> None:
        self.viewer_times_out = viewer_times_out
        self.login = login

    def post(
        self, path: str, body: bytes, headers: Mapping[str, str], *, deadline: float
    ) -> Response:
        if "q" in json.loads(body)["variables"] or self.viewer_times_out:
            raise DeadlineExceeded()
        document = {"data": {"viewer": {"login": self.login}}}
        return Response(200, {}, json.dumps(document).encode())


def collect(transport: Any) -> updates.Found:
    return updates.collect(
        Client(TOKEN, transport),
        ["q1", "q2"],
        excluded=frozenset(),
        cutoff=date(2026, 10, 16),
        include_bots=False,
    )


def test_out_of_time_after_the_viewer_returns_truncated() -> None:
    found = collect(Timed())
    assert found.truncated
    assert found.updates == []


def test_out_of_time_on_the_viewer_fails() -> None:
    with pytest.raises(GitHubError, match="timed out"):
        collect(Timed(viewer_times_out=True))


@pytest.mark.parametrize("login", [None, "", "a b", "acme-me\n", 42])
def test_a_malformed_viewer_fails(login: Any) -> None:
    with pytest.raises(GitHubError, match="malformed"):
        collect(Timed(login=login))


def test_no_search_is_started_without_time_left() -> None:
    ticks = iter([0.0, 0.0, 100.0, 100.0])
    found = updates.collect(
        Client(TOKEN, Timed(), clock=lambda: next(ticks), budget=25.0),  # type: ignore[arg-type]
        ["q1"],
        excluded=frozenset(),
        cutoff=date(2026, 10, 16),
        include_bots=False,
    )
    assert found.truncated


def test_an_excluded_repository_is_filtered_even_when_returned() -> None:
    class One(Recorder):
        def post(
            self, path: str, body: bytes, headers: Mapping[str, str], *, deadline: float
        ) -> Response:
            if "q" not in json.loads(body)["variables"]:
                return super().post(path, body, headers, deadline=deadline)
            info = {"hasNextPage": False, "endCursor": None}
            node = issue("bob", [])
            document = {"data": {"search": {"pageInfo": info, "nodes": [node]}}}
            return Response(200, {}, json.dumps(document).encode())

    found = updates.collect(
        Client(TOKEN, One()),  # type: ignore[arg-type]
        ["q1"],
        excluded=frozenset({"acme-org/api"}),
        cutoff=date(2026, 10, 16),
        include_bots=False,
    )
    assert found.updates == []
    assert found.filtered == 1
    assert found.checked == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"__typename": "IssueComment", "createdAt": "2026-10-20T10:00:00Z", "author": "bob"},
        {"__typename": "IssueComment", "createdAt": "2026-10-20T10:00:00Z", "author": {}},
        {"__typename": "PullRequestCommit", "commit": "abc"},
        {"__typename": "PullRequestReview", "state": "APPROVED", "submittedAt": 3},
        {"__typename": "LabeledEvent", "createdAt": "2026-10-20T10:00:00Z", "actor": None},
    ],
)
def test_a_malformed_event_is_ignored_not_raised(bad: dict[str, Any]) -> None:
    assert verdict(issue(ME, [bad])) == "old"


def test_without_a_node_id_the_fingerprint_falls_back_to_the_number() -> None:
    node = {**issue("bob", []), "id": "bad id!"}
    found = verdict(node)
    assert isinstance(found, updates.Update)
    assert found.node_id is None
    assert found.item_fingerprint.startswith("github:issue:acme-org-api:1::")
    assert found.fingerprint.startswith("github:update:acme-org-api-1:opened-2026-10-17T09-00-00Z")


def forged_commit(at: str) -> dict[str, Any]:
    return {
        "__typename": "PullRequestCommit",
        "id": "PRC_forged",
        "commit": {"committedDate": at, "author": {"user": {"login": ME}}},
    }


def test_a_forged_commit_in_your_name_does_not_hide_a_newer_comment() -> None:
    """Anyone can author a commit with your e-mail and any date: it is not your action."""
    node = {
        **issue("bob", []),
        "__typename": "PullRequest",
        "prTimeline": {"nodes": [comment(1, "bob", 21), forged_commit("2099-01-01T00:00:00Z")]},
    }
    found = verdict(node)
    assert isinstance(found, updates.Update)
    assert found.last.kind == "commented"
    assert found.since is None


def test_your_real_actions_still_count_next_to_your_commits() -> None:
    node = {
        **issue(ME, []),
        "__typename": "PullRequest",
        "prTimeline": {"nodes": [comment(1, "bob", 20), forged_commit("2026-10-21T10:00:00Z")]},
    }
    found = verdict(node)
    assert isinstance(found, updates.Update)
    assert found.since == "2026-10-17T09:00:00Z"


def test_a_future_time_is_clamped_to_the_end_of_the_run_day() -> None:
    future = {
        "__typename": "PullRequestCommit",
        "id": "PRC_future",
        "commit": {"committedDate": "2099-01-01T00:00:00Z", "author": {"user": {"login": "bob"}}},
    }
    node = {**issue(ME, []), "__typename": "PullRequest", "prTimeline": {"nodes": [future]}}
    found = updates.update(node, ME, "2026-10-16", False, "2026-10-30T23:59:59Z")
    assert isinstance(found, updates.Update)
    assert found.last.at == "2026-10-30T23:59:59Z"
