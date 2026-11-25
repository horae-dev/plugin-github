# SPDX-License-Identifier: Apache-2.0
"""`github.ci_failures` beyond the recordings: the requests it makes (pagination stop, page
cap, filters sent to GitHub), the deadline, the strict run link and the hostile text."""

import asyncio
import json
import unicodedata
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from horae_sdk import Plugin, tasks
from horae_sdk.testing import call

from conftest import NOW, TOKEN, FakeTransport
from horae_github import build, ci
from horae_github.client import Client, GitHubError
from horae_github.http import DeadlineExceeded, Response

RECORDINGS = Path(__file__).parent / "recordings"
SHA = "a" * 40


def run(plugin: Plugin, **params: Any) -> Any:
    return asyncio.run(call(plugin, "github.ci_failures", {"today": "2026-10-30", **params}))


def owner_requests(transport: FakeTransport) -> list[tuple[str, Any]]:
    return [
        (r["variables"]["login"], r["variables"]["after"])
        for r in transport.requests
        if "login" in r["variables"]
    ]


def test_paging_stops_at_the_first_repository_older_than_the_window(
    plugin: Plugin, transport: FakeTransport
) -> None:
    result = run(plugin, orgs=["acme-org"], include_my_prs=False)
    assert result.error is None
    # the second page ends with stale repositories: the third page is never asked for
    assert owner_requests(transport) == [("acme-org", None), ("acme-org", "Y3Vyc29yOjE")]
    first = transport.requests[0]
    assert first["variables"]["isFork"] is False
    assert "ownerAffiliations: [OWNER]" in first["query"]
    assert "isArchived: false" in first["query"]


def test_a_wider_window_reads_the_next_page(plugin: Plugin, transport: FakeTransport) -> None:
    # the fixture has no third page: asking for it (a failed call) proves the walk went on
    result = run(plugin, orgs=["acme-org"], include_my_prs=False, pushed_within_days=365)
    assert result.error == "github.ci_failures failed (AssertionError)"
    assert owner_requests(transport)[-1] == ("acme-org", "Y3Vyc29yOjI")


def test_my_pull_requests_are_one_search_per_owner(
    plugin: Plugin, transport: FakeTransport
) -> None:
    run(plugin, orgs=["acme-org"], users=["acme-user"])
    searches = [r["variables"]["q"] for r in transport.requests if "q" in r["variables"]]
    assert searches == [
        "is:pr is:open author:@me archived:false org:acme-org",
        "is:pr is:open author:@me archived:false user:acme-user",
    ]


def page(nodes: list[Any], cursor: str | None) -> dict[str, Any]:
    info = {"hasNextPage": cursor is not None, "endCursor": cursor}
    return {"data": {"repositoryOwner": {"repositories": {"pageInfo": info, "nodes": nodes}}}}


def fresh(n: int) -> dict[str, Any]:
    commit = {"oid": SHA, "committedDate": "2026-10-29T08:00:00Z", "statusCheckRollup": None}
    return {
        "nameWithOwner": f"acme-big/r{n}",
        "isArchived": False,
        "isFork": False,
        "pushedAt": "2026-10-29T09:00:00Z",
        "defaultBranchRef": {"name": "main", "target": commit},
    }


class Endless:
    """Every page is full of fresh repositories and has a next one."""

    def __init__(self, fail_after: int | None = None) -> None:
        self.calls = 0
        self.fail_after = fail_after

    def post(
        self, path: str, body: bytes, headers: Mapping[str, str], *, deadline: float
    ) -> Response:
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise DeadlineExceeded()
        n = self.calls
        document = page([fresh(n)], f"c{n}")
        return Response(200, {}, json.dumps(document).encode())


def collect(client: Client, **kwargs: Any) -> ci.Walk:
    options: dict[str, Any] = {
        "owners": ["acme-big"],
        "repos": [],
        "excluded": frozenset(),
        "day": date(2026, 10, 30),
        "pushed_within_days": 30,
        "include_forks": False,
        "pr_queries": None,
    }
    options.update(kwargs)
    return ci.collect(client, **options)


def test_pages_per_owner_are_capped() -> None:
    transport = Endless()
    walk = collect(Client(TOKEN, transport))  # type: ignore[arg-type]
    assert transport.calls == ci.MAX_OWNER_PAGES
    assert walk.truncated
    assert walk.repos == ci.MAX_OWNER_PAGES


def test_your_pull_requests_are_read_after_the_page_cap() -> None:
    """A capped owner walk (`truncated`) must not hide your failing pull requests."""

    class WithSearch(Endless):
        def post(
            self, path: str, body: bytes, headers: Mapping[str, str], *, deadline: float
        ) -> Response:
            if "q" not in json.loads(body)["variables"]:
                return super().post(path, body, headers, deadline=deadline)
            commit = {
                "oid": SHA,
                "committedDate": "2026-10-29T08:00:00Z",
                "statusCheckRollup": {"state": "FAILURE"},
            }
            node = {
                "__typename": "PullRequest",
                "id": "PR_1",
                "number": 7,
                "headRefName": "fix",
                "repository": {
                    "nameWithOwner": "acme-big/r1",
                    "isArchived": False,
                    "isFork": False,
                },
                "commits": {"nodes": [{"commit": commit}]},
            }
            info = {"hasNextPage": False, "endCursor": None}
            document = {"data": {"search": {"pageInfo": info, "nodes": [node]}}}
            return Response(200, {}, json.dumps(document).encode())

    walk = collect(Client(TOKEN, WithSearch()), pr_queries=["is:pr"])  # type: ignore[arg-type]
    assert walk.truncated
    assert [f.pr for f in walk.failures.values()] == [7]


def test_out_of_time_after_a_page_returns_what_was_read() -> None:
    walk = collect(Client(TOKEN, Endless(fail_after=1)))  # type: ignore[arg-type]
    assert walk.truncated
    assert walk.repos == 1


def test_out_of_time_on_the_first_page_fails() -> None:
    with pytest.raises(GitHubError, match="timed out"):
        collect(Client(TOKEN, Endless(fail_after=0)))  # type: ignore[arg-type]


def test_no_page_is_started_without_time_left() -> None:
    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0])
    transport = Endless()
    client = Client(TOKEN, transport, clock=lambda: next(ticks), budget=25.0)  # type: ignore[arg-type]
    walk = collect(client, pr_queries=["is:pr"])
    assert transport.calls == 1
    assert walk.truncated


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://github.com/acme-org/api/actions/runs/123", True),
        ("https://github.com/acme-org/api/actions/runs/123/job/4", False),
        ("https://github.com/acme-org/api/actions/runs/123?check_suite_focus=true", False),
        ("https://github.com/acme-org/api/actions/runs/123#x", False),
        ("https://github.com/acme-org/other/actions/runs/123", False),
        ("https://github.com/Acme-Org/api/actions/runs/123", False),
        ("https://github.com.evil.example/acme-org/api/actions/runs/123", False),
        ("http://github.com/acme-org/api/actions/runs/123", False),
        ("https://github.com/acme-org/api/actions/runs/12a", False),
        ("https://github.com/acme-org/api/actions/runs/", False),
        ("https://user@github.com/acme-org/api/actions/runs/1", False),
        ("https://github.com/acme-org/api/actions/runs/1\n", False),
    ],
)
def test_a_run_link_is_strict(url: str, ok: bool) -> None:
    assert ci.run_link(url, "acme-org/api") is ok


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return []


def test_hostile_text_has_no_control_or_format_character() -> None:
    expect = json.loads((RECORDINGS / "ci-failures-hostile.json").read_text("utf-8"))["expect"]
    found = strings(expect)
    assert any("📅" in s for s in found)  # the hostile text is there, as data
    for text in found:
        assert not any(unicodedata.category(c) in ("Cc", "Cf") for c in text), text
    for intent in expect["intents"]:
        assert intent["link"].startswith("https://github.com/acme-hostile/app/commit/")


@pytest.mark.parametrize(
    ("commit", "why"),
    [
        (None, "malformed"),
        (
            {"oid": SHA, "committedDate": "2026-10-29T08:00:00Z", "statusCheckRollup": None},
            "pending",
        ),
        ({"oid": SHA, "committedDate": "bad", "statusCheckRollup": None}, "malformed"),
        (
            {"oid": SHA, "committedDate": "2026-02-30T08:00:00Z", "statusCheckRollup": None},
            "malformed",
        ),
        (
            {"oid": SHA, "committedDate": "2026-10-29T08:00:00Z", "statusCheckRollup": []},
            "malformed",
        ),
        (
            {
                "oid": SHA,
                "committedDate": "2026-10-29T08:00:00Z",
                "statusCheckRollup": {"state": "SUCCESS"},
            },
            "passing",
        ),
        (
            {
                "oid": SHA,
                "committedDate": "2026-10-29T08:00:00Z",
                "statusCheckRollup": {"state": "EXPECTED"},
            },
            "pending",
        ),
    ],
)
def test_a_head_commit_that_is_not_a_failure(commit: Any, why: str) -> None:
    assert ci.failure("acme-org/api", "main", None, commit) == why


def test_many_failing_checks_are_counted_not_listed() -> None:
    nodes = [
        {"__typename": "StatusContext", "context": f"ci/{n:02}", "state": "FAILURE"}
        for n in range(12)
    ]
    rollup = {"state": "FAILURE", "contexts": {"totalCount": 12, "nodes": nodes}}
    commit = {"oid": SHA, "committedDate": "2026-10-29T08:00:00Z", "statusCheckRollup": rollup}
    found = ci.failure("acme-org/api", "main", None, commit)
    assert isinstance(found, ci.Failure)
    assert len(found.checks) == ci.MAX_CHECKS_SHOWN
    assert found.more_checks == 2
    assert found.what.endswith("and 2 more")


def test_a_long_push_title_fits() -> None:
    commit = {
        "oid": SHA,
        "committedDate": "2026-10-29T08:00:00Z",
        "statusCheckRollup": {"state": "ERROR", "contexts": None},
    }
    found = ci.failure("o" * 100 + "/" + "r" * 100, "b" * 300, 7, commit)
    assert isinstance(found, ci.Failure)
    push = found.push()
    assert len(push["title"]) <= tasks.MAX_TITLE
    assert push["title"].endswith("…)")
    assert push["body"].startswith("Checks failed")


def test_the_same_failing_commit_keeps_its_fingerprint_a_new_one_does_not() -> None:
    def fingerprint(sha: str, branch: str = "main", pr: int | None = None) -> str:
        commit = {
            "oid": sha,
            "committedDate": "2026-10-29T08:00:00Z",
            "statusCheckRollup": {"state": "FAILURE"},
        }
        found = ci.failure("acme-org/api", branch, pr, commit)
        assert isinstance(found, ci.Failure)
        return found.fingerprint

    assert fingerprint(SHA) == fingerprint(SHA)
    assert fingerprint(SHA) != fingerprint("b" * 40)
    assert fingerprint(SHA) != fingerprint(SHA, "release")
    assert fingerprint(SHA, pr=3) != fingerprint(SHA, pr=4)


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"pushed_within_days": -1}, "pushed_within_days is 0 to 365"),
        ({"max_items": 0}, "max_items is 1 to 100"),
        ({"today": "30/10/2026"}, "today must be YYYY-MM-DD"),
    ],
)
def test_bad_parameters_fail(plugin: Plugin, params: dict[str, Any], message: str) -> None:
    result = run(plugin, orgs=["acme-org"], **params)
    assert result.error is not None
    assert message in result.error


def test_a_missing_token_fails(transport: FakeTransport) -> None:
    plugin = build(lambda t: Client(t, transport), clock=lambda: NOW, environ={})  # type: ignore[arg-type]
    result = run(plugin, orgs=["acme-org"])
    assert result.error == "github: GITHUB_TOKEN is not set"
    assert transport.requests == []
