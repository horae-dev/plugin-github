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


def pr_node(number: int = 7, repo: str = "acme-big/r1") -> dict[str, Any]:
    commit = {
        "oid": SHA,
        "committedDate": "2026-10-29T08:00:00Z",
        "statusCheckRollup": {"state": "FAILURE"},
    }
    return {
        "__typename": "PullRequest",
        "id": f"PR_{number}",
        "number": number,
        "headRefName": "fix",
        "repository": {"nameWithOwner": repo, "isArchived": False, "isFork": False},
        "commits": {"nodes": [{"commit": commit}]},
    }


def search_page(nodes: list[Any]) -> Response:
    info = {"hasNextPage": False, "endCursor": None}
    document = {"data": {"search": {"pageInfo": info, "nodes": nodes}}}
    return Response(200, {}, json.dumps(document).encode())


class WithSearch(Endless):
    """`Endless` owner pages, and one failing pull request of `acme-big/r1` for the search."""

    def __init__(self, fail_after: int | None = None) -> None:
        super().__init__(fail_after)
        self.searches = 0

    def post(
        self, path: str, body: bytes, headers: Mapping[str, str], *, deadline: float
    ) -> Response:
        if "q" not in json.loads(body)["variables"]:
            return super().post(path, body, headers, deadline=deadline)
        self.searches += 1
        return search_page([pr_node()])


def test_pages_per_owner_are_capped() -> None:
    # with your pull requests too: the cap of the owner walk must not cost the search (the
    # bug of #12 went unnoticed while this test had no pull request query)
    transport = WithSearch()
    walk = collect(Client(TOKEN, transport), pr_queries=["is:pr"])  # type: ignore[arg-type]
    assert transport.calls == ci.MAX_OWNER_PAGES
    assert transport.searches == 1
    assert walk.truncated
    assert walk.repos == ci.MAX_OWNER_PAGES + 1  # the pages' repositories and the pull request
    plain = Endless()
    capped = collect(Client(TOKEN, plain))  # type: ignore[arg-type]
    assert plain.calls == ci.MAX_OWNER_PAGES
    assert capped.repos == ci.MAX_OWNER_PAGES


def test_your_pull_requests_are_read_after_the_page_cap() -> None:
    """A capped owner walk (`truncated`) must not hide your failing pull requests."""
    walk = collect(Client(TOKEN, WithSearch()), pr_queries=["is:pr"])  # type: ignore[arg-type]
    assert walk.truncated
    assert [f.pr for f in walk.failures.values()] == [7]


class Scripted:
    """Answers owner reads, repository reads and searches from callables."""

    def __init__(self, owner: Any, repository: Any = None, nodes: Any = None) -> None:
        self.owner = owner
        self.repository = repository
        self.nodes = nodes or []
        self.owner_calls = 0

    def post(
        self, path: str, body: bytes, headers: Mapping[str, str], *, deadline: float
    ) -> Response:
        variables = json.loads(body)["variables"]
        if "q" in variables:
            return search_page(self.nodes)
        if "login" in variables:
            self.owner_calls += 1
            document = self.owner(self.owner_calls)
        else:
            document = {"data": {"repository": self.repository}}
        return Response(200, {}, json.dumps(document).encode())


def test_an_owner_vanishing_on_page_two_is_truncated_not_missing() -> None:
    def owner(call: int) -> dict[str, Any]:
        if call == 1:
            return page([fresh(1)], "c1")
        return {"data": {"repositoryOwner": None}}

    walk = collect(Client(TOKEN, Scripted(owner)))  # type: ignore[arg-type]
    assert walk.missing == 0
    assert walk.truncated
    assert walk.repos == 1


def test_a_repository_in_an_owner_walk_and_in_repos_is_counted_once() -> None:
    transport = Scripted(lambda _n: page([fresh(1)], None), repository=fresh(1))
    walk = collect(Client(TOKEN, transport), repos=["acme-big/r1"])  # type: ignore[arg-type]
    assert walk.repos == 1
    assert len(walk.failures) == 0  # no rollup: nothing failing, still looked at once


def test_unreadable_owners_keep_your_pull_requests() -> None:
    transport = Scripted(lambda _n: {"data": {"repositoryOwner": None}}, nodes=[pr_node()])
    walk = collect(Client(TOKEN, transport), pr_queries=["is:pr"])  # type: ignore[arg-type]
    assert walk.missing == 1
    assert [f.pr for f in walk.failures.values()] == [7]


def test_unreadable_owners_and_no_pull_requests_fail() -> None:
    transport = Scripted(lambda _n: {"data": {"repositoryOwner": None}}, nodes=[])
    # the search worked and found nothing: still no owner could be read, but the search is a
    # read of its own, so the call returns it (an empty, partial answer) instead of failing
    walk = collect(Client(TOKEN, transport), pr_queries=["is:pr"])  # type: ignore[arg-type]
    assert walk.missing == 1
    assert not walk.failures
    with pytest.raises(GitHubError, match="no owner or repository"):
        collect(Client(TOKEN, transport), pr_queries=None)  # type: ignore[arg-type]


def test_a_check_run_without_its_workflow_run_is_named_by_its_job() -> None:
    """A token that cannot read `workflowRun` (`Actions: Read` is not confirmed to be needed
    for GraphQL) still gets the failing job's name, and the link falls back to the commit."""
    nodes = [
        {
            "__typename": "CheckRun",
            "name": "build",
            "conclusion": "FAILURE",
            "checkSuite": {"workflowRun": None},
        }
    ]
    rollup = {"state": "FAILURE", "contexts": {"totalCount": 1, "nodes": nodes}}
    commit = {"oid": SHA, "committedDate": "2026-10-29T08:00:00Z", "statusCheckRollup": rollup}
    found = ci.failure("acme-org/api", "main", None, commit)
    assert isinstance(found, ci.Failure)
    assert found.checks == ("build",)
    assert found.url == f"https://github.com/acme-org/api/commit/{SHA}"


def test_notify_max_bounds_the_pushes_not_the_data(plugin: Plugin) -> None:
    everything = run(plugin, orgs=["acme-org"], users=["acme-user"], notify=True)
    assert everything.error is None
    total = len(everything.data["items"])
    assert total > 2
    assert len(everything.intents) == total
    assert everything.data["notify_skipped"] == 0
    capped = run(plugin, orgs=["acme-org"], users=["acme-user"], notify=True, notify_max=2)
    assert len(capped.data["items"]) == total
    assert [i["key"] for i in capped.intents] == [
        i["fingerprint"] for i in capped.data["items"][:2]
    ]
    assert capped.data["notify_skipped"] == total - 2
    quiet = run(plugin, orgs=["acme-org"], users=["acme-user"], notify_max=2)
    assert quiet.intents == []
    assert quiet.data["notify_skipped"] == 0


@pytest.mark.parametrize("value", [0, 101, -1])
def test_a_bad_notify_max_fails(plugin: Plugin, value: Any) -> None:
    result = run(plugin, orgs=["acme-org"], notify_max=value)
    assert result.error is not None
    assert "notify_max is 1 to 100" in result.error


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
