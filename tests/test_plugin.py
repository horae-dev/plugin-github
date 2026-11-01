# SPDX-License-Identifier: Apache-2.0
"""What the recordings do not show: the search strings, dedupe across kinds, determinism,
parameter checks and the token's handling."""

import json
import logging
from typing import Any

import anyio
import pytest
from horae_sdk import Plugin
from horae_sdk.testing import Replayed
from horae_sdk.testing import call as call_tool

from conftest import NOW, TOKEN, WALL, FakeTransport, load_fixtures
from horae_github import build
from horae_github.client import Client
from horae_github.plugin import ASSIGNED, MENTIONED, REVIEW, Kind, parse, search_queries


def run(plugin: Plugin, tool: str, **params: Any) -> Replayed:
    async def go() -> Replayed:
        return await call_tool(plugin, tool, params)

    return anyio.run(go)


def queries(
    kind: Kind, skip_covered: bool = False, direct_only: bool = False, **filters: list[str]
) -> list[str]:
    empty: dict[str, list[str]] = {"orgs": [], "users": [], "repos": [], "exclude_repos": []}
    return search_queries(
        kind, skip_covered=skip_covered, direct_only=direct_only, **{**empty, **filters}
    )


def test_search_strings() -> None:
    assert queries(REVIEW) == ["is:open is:pr archived:false review-requested:@me"]
    assert queries(REVIEW, direct_only=True) == [
        "is:open is:pr archived:false user-review-requested:@me"
    ]
    assert queries(ASSIGNED, skip_covered=True) == [
        "is:open archived:false assignee:@me -review-requested:@me"
    ]
    assert queries(ASSIGNED) == ["is:open archived:false assignee:@me"]
    assert queries(MENTIONED, skip_covered=True) == [
        "is:open archived:false mentions:@me -assignee:@me -review-requested:@me"
    ]
    # draft pull requests are not left out: no `draft:` qualifier
    assert not any("draft" in q for kind in (REVIEW, ASSIGNED, MENTIONED) for q in queries(kind))


def test_mixed_filters_are_one_search_each() -> None:
    """GitHub ANDs qualifiers of different kinds, so each value is its own search, and every
    search carries every exclusion."""
    assert queries(
        REVIEW,
        orgs=["acme-org", "acme-org"],
        users=["ada_acme"],
        repos=["other-org/api"],
        exclude_repos=["acme-org/legacy", "acme-org/old"],
    ) == [
        "is:open is:pr archived:false review-requested:@me org:acme-org"
        " -repo:acme-org/legacy -repo:acme-org/old",
        "is:open is:pr archived:false review-requested:@me user:ada_acme"
        " -repo:acme-org/legacy -repo:acme-org/old",
        "is:open is:pr archived:false review-requested:@me repo:other-org/api"
        " -repo:acme-org/legacy -repo:acme-org/old",
    ]


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        ({"orgs": ["acme is:closed"]}, "invalid org filter"),
        ({"users": ["-ada"]}, "invalid user filter"),
        ({"repos": ["acme-org"]}, "invalid repo filter"),
        ({"exclude_repos": ["a/b c"]}, "invalid exclude_repos filter"),
        ({"orgs": ["acme\n"]}, "invalid org filter"),
        ({"orgs": ["acme\nis:closed"]}, "invalid org filter"),
        ({"orgs": ['acme"']}, "invalid org filter"),
        ({"orgs": ["'acme'"]}, "invalid org filter"),
        ({"orgs": ["acme٣"]}, "invalid org filter"),
        ({"orgs": ["_acme"]}, "invalid org filter"),
        ({"repos": ["acme-org/api\n"]}, "invalid repo filter"),
        ({"repos": ['acme-org/a"pi']}, "invalid repo filter"),
        ({"exclude_repos": [f"acme-org/r{i}" for i in range(21)]}, "at most 20"),
        (
            {"orgs": [f"o{i}" for i in range(5)], "repos": [f"o/r{i}" for i in range(4)]},
            "at most 8",
        ),
    ],
)
def test_filters_cannot_inject_qualifiers(
    plugin: Plugin, filters: dict[str, Any], message: str
) -> None:
    replayed = run(plugin, "github.review_requests", **filters)
    assert replayed.error is not None
    assert message in replayed.error


@pytest.mark.parametrize(
    ("tool", "params", "message"),
    [
        ("github.review_requests", {"max_items": 0}, "max_items is 1 to 200"),
        ("github.assigned", {"max_items": 201}, "max_items is 1 to 200"),
        ("github.review_requests", {"due_in_business_days": -1}, "due offsets"),
        ("github.mentions", {"due_in_days": 400}, "due offsets"),
        ("github.assigned", {"due_in_days": -2}, "due offsets"),
        ("github.mentions", {"timezone": "Nowhere/City"}, "unknown time zone"),
        ("github.mentions", {"today": "tomorrow"}, "YYYY-MM-DD"),
    ],
)
def test_bad_parameters(plugin: Plugin, tool: str, params: dict[str, Any], message: str) -> None:
    replayed = run(plugin, tool, **params)
    assert replayed.error is not None
    assert message in replayed.error


def test_an_item_both_assigned_and_review_requested_is_one_task(plugin: Plugin) -> None:
    review = run(plugin, "github.review_requests", today="2026-10-30")
    assigned = run(
        plugin, "github.assigned", today="2026-10-30", skip_covered=False, orgs=["acme-org"]
    )
    shared = {i["fingerprint"] for i in review.intents} & {
        i["fingerprint"] for i in assigned.intents
    }
    assert shared == {"github:pr:PR_kwDOSYNTH0042"}
    # by default the assigned search returns it too (same fingerprint: one task)
    default = run(plugin, "github.assigned", today="2026-10-30")
    assert "github:pr:PR_kwDOSYNTH0042" in {i["fingerprint"] for i in default.intents}
    # with skip_covered the assigned search leaves review requests out
    covered = run(plugin, "github.assigned", today="2026-10-30", skip_covered=True)
    assert "github:pr:PR_kwDOSYNTH0042" not in {i["fingerprint"] for i in covered.intents}


def test_a_node_seen_twice_is_one_task(plugin: Plugin) -> None:
    replayed = run(plugin, "github.review_requests", today="2026-10-30")
    fingerprints = [i["fingerprint"] for i in replayed.intents]
    assert len(fingerprints) == len(set(fingerprints)) == 3


def test_fingerprints_are_stable_across_runs_and_days(plugin: Plugin) -> None:
    friday = run(plugin, "github.review_requests", today="2026-10-30")
    again = run(plugin, "github.review_requests", today="2026-10-30")
    monday = run(plugin, "github.review_requests", today="2026-11-02")
    assert friday.expectation() == again.expectation()
    assert [i["fingerprint"] for i in friday.intents] == [i["fingerprint"] for i in monday.intents]
    assert {i["due"] for i in monday.intents} == {"2026-11-03"}


def test_without_today_the_clock_is_read_in_the_instance_time_zone(
    transport: FakeTransport,
) -> None:
    def client(token: str) -> Client:
        return Client(token, transport, wall=lambda: WALL)  # type: ignore[arg-type]

    tokyo = build(client, clock=lambda: NOW, environ={"GITHUB_TOKEN": TOKEN, "TZ": "Asia/Tokyo"})
    replayed = run(tokyo, "github.mentions")
    assert replayed.data["day"] == "2026-10-31"  # Saturday in Tokyo, Friday in UTC
    assert replayed.intents[0]["due"] == "2026-11-02"
    paris = run(tokyo, "github.mentions", timezone="Europe/Paris")
    assert paris.data["day"] == "2026-10-30"


def test_a_missing_token_is_a_tool_error(transport: FakeTransport) -> None:
    plugin = build(lambda token: Client(token, transport), environ={})  # type: ignore[arg-type]
    replayed = run(plugin, "github.review_requests")
    assert replayed.error == "github: GITHUB_TOKEN is not set"
    assert transport.requests == []


def test_the_token_never_leaves_the_header(
    plugin: Plugin, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    outputs = [
        run(plugin, "github.review_requests", today="2026-10-30", repos=[repo])
        for repo in ("acme-org/hostile", "acme-org/denied", "acme-org/limited", "acme-org/saml")
    ]
    for replayed in outputs:
        assert TOKEN not in repr(replayed.expectation())
    assert TOKEN not in caplog.text


def test_parse_refuses_malformed_nodes() -> None:
    good = {
        "__typename": "Issue",
        "id": "I_1",
        "number": 5,
        "title": "t",
        "repository": {"nameWithOwner": "acme-org/api"},
    }
    assert parse(good) is not None
    for bad in (
        {**good, "number": True},
        {**good, "number": "5"},
        {**good, "number": 0},
        {**good, "repository": "acme-org/api"},
        {**good, "repository": {"nameWithOwner": "../../x"}},
        {**good, "repository": {"nameWithOwner": "acme-org/api\n"}},
        {**good, "repository": {"nameWithOwner": "acme-org/a pi"}},
        {**good, "repository": {"nameWithOwner": "acme-org/api?x=1"}},
        {**good, "repository": {"nameWithOwner": "acme-org"}},
        {**good, "__typename": "Commit"},
        {**good, "__typename": ["Issue"]},
        {**good, "__typename": None},
        "Issue",
        None,
    ):
        assert parse(bad) is None
    odd = parse({**good, "id": "I_ with space", "title": None, "milestone": {"dueOn": 5}})
    assert odd is not None
    assert odd.node_id is None
    assert odd.title == ""
    assert odd.milestone_due is None
    assert odd.url == "https://github.com/acme-org/api/issues/5"


def test_fixtures_are_synthetic() -> None:
    for fixture in load_fixtures():
        nodes = (((fixture["body"] or {}).get("data") or {}).get("search") or {}).get("nodes", [])
        for node in nodes:
            if isinstance(node, dict):
                repo = node["repository"]["nameWithOwner"]
                assert repo.startswith("acme-org/") or repo == "not a repo"


def test_returned_repositories_are_not_held_to_the_filter_rules() -> None:
    """GitHub's own answers are kept unless they could not be a repository: an Enterprise
    Managed User's `_`, a leading dot or a long name are GitHub's business."""
    for repo in ("ada_acme/notes", "acme-org/.github", "_odd/name", "a" * 60 + "/b"):
        node = {"__typename": "PullRequest", "number": 1, "repository": {"nameWithOwner": repo}}
        item = parse(node)
        assert item is not None
        assert item.url == f"https://github.com/{repo}/pull/1"


class ByQuery:
    """Answers each search string from `pages` (one page each) and records the strings."""

    def __init__(self, pages: dict[str, list[Any]], errors: int = 0) -> None:
        self.pages, self.errors = pages, errors
        self.queries: list[str] = []

    def post(self, path: str, body: bytes, headers: Any, *, deadline: float) -> Any:
        from horae_github.http import Response

        q = json.loads(body)["variables"]["q"]
        self.queries.append(q)
        document: dict[str, Any] = {
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": self.pages[q],
                }
            }
        }
        if self.errors:
            document["errors"] = [{"type": "FORBIDDEN", "message": "x"}] * self.errors
        return Response(200, {}, json.dumps(document).encode())


def node(number: int, repo: str = "acme-org/api", typename: str = "Issue") -> dict[str, Any]:
    return {
        "__typename": typename,
        "id": f"I_{repo.replace('/', '_')}_{number}",
        "number": number,
        "title": f"Item {number}",
        "repository": {"nameWithOwner": repo},
    }


def plugin_over(transport: Any) -> Plugin:
    def client(token: str) -> Client:
        return Client(token, transport, sleep=lambda _s: None, wall=lambda: WALL)

    return build(client, clock=lambda: NOW, environ={"GITHUB_TOKEN": TOKEN})


def test_a_mixed_filter_is_any_of_its_values() -> None:
    base = "is:open archived:false mentions:@me"
    transport = ByQuery(
        {
            f"{base} org:acme-org -repo:acme-org/legacy": [node(1), node(2, "acme-org/web")],
            f"{base} repo:other-org/api -repo:acme-org/legacy": [node(7, "other-org/api")],
            f"{base} user:ada_acme -repo:acme-org/legacy": [node(1), "junk", node(0)],
        }
    )
    replayed = run(
        plugin_over(transport),
        "github.mentions",
        today="2026-10-30",
        orgs=["acme-org"],
        users=["ada_acme"],
        repos=["other-org/api"],
        exclude_repos=["acme-org/legacy"],
    )
    assert len(transport.queries) == 3
    assert [(i["repo"], i["number"]) for i in replayed.data["items"]] == [
        ("acme-org/api", 1),
        ("acme-org/web", 2),
        ("other-org/api", 7),
    ]
    assert replayed.data["skipped"] == 2  # "junk" and number 0, surfaced
    assert replayed.data["partial"] is False


def test_hidden_results_make_the_data_partial() -> None:
    transport = ByQuery({"is:open is:pr archived:false review-requested:@me": [node(3)]}, 2)
    replayed = run(plugin_over(transport), "github.review_requests", today="2026-10-30")
    assert replayed.error is None
    assert replayed.data["partial"] is True
    assert replayed.data["errors"] == 2
    assert [i["number"] for i in replayed.data["items"]] == [3]


def test_direct_only_review_requests() -> None:
    transport = ByQuery({"is:open is:pr archived:false user-review-requested:@me": []})
    replayed = run(plugin_over(transport), "github.review_requests", direct_only=True)
    assert replayed.error is None
    assert transport.queries == ["is:open is:pr archived:false user-review-requested:@me"]


@pytest.mark.parametrize(
    ("today", "days", "due"),
    [
        ("2026-10-31", 0, "2026-10-31"),  # Saturday, 0: that Saturday
        ("2026-10-31", 1, "2026-11-02"),  # Saturday, 1: Monday
        ("2026-11-01", 1, "2026-11-02"),  # Sunday, 1: Monday
        ("2026-10-30", 1, "2026-11-02"),  # Friday, 1: Monday
        ("2020-02-28", 1, "2020-03-02"),  # a past day is a day like any other
    ],
)
def test_business_days_from_weekend_and_past_days(
    plugin: Plugin, today: str, days: int, due: str
) -> None:
    replayed = run(plugin, "github.review_requests", today=today, due_in_business_days=days)
    assert {i["due"] for i in replayed.intents} == {due}


def test_a_past_milestone_is_a_past_due_date() -> None:
    late = {**node(4), "milestone": {"dueOn": "2025-01-10T08:00:00Z"}}
    transport = ByQuery({"is:open archived:false assignee:@me": [late]})
    replayed = run(plugin_over(transport), "github.assigned", today="2026-10-30")
    assert replayed.intents[0]["due"] == "2025-01-10"


def test_draft_pull_requests_are_included(plugin: Plugin) -> None:
    replayed = run(plugin, "github.assigned", today="2026-10-30")
    assert any(i["title"].startswith("Draft") for i in replayed.data["items"])
