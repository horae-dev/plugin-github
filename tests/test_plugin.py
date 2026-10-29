# SPDX-License-Identifier: Apache-2.0
"""What the recordings do not show: the search strings, dedupe across kinds, determinism,
parameter checks and the token's handling."""

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
from horae_github.plugin import ASSIGNED, MENTIONED, REVIEW, Kind, parse, search_query


def run(plugin: Plugin, tool: str, **params: Any) -> Replayed:
    async def go() -> Replayed:
        return await call_tool(plugin, tool, params)

    return anyio.run(go)


def query(kind: Kind, skip_covered: bool = True, **filters: list[str]) -> str:
    empty: dict[str, list[str]] = {"orgs": [], "users": [], "repos": [], "exclude_repos": []}
    return search_query(kind, skip_covered=skip_covered, **{**empty, **filters})


def test_search_strings() -> None:
    assert query(REVIEW) == "is:open is:pr archived:false review-requested:@me"
    assert query(ASSIGNED) == "is:open archived:false assignee:@me -review-requested:@me"
    assert query(ASSIGNED, skip_covered=False) == "is:open archived:false assignee:@me"
    assert query(MENTIONED) == (
        "is:open archived:false mentions:@me -assignee:@me -review-requested:@me"
    )
    assert query(
        REVIEW,
        orgs=["acme-org", "acme-org"],
        users=["ada"],
        repos=["acme-org/api"],
        exclude_repos=["acme-org/legacy"],
    ) == (
        "is:open is:pr archived:false review-requested:@me"
        " org:acme-org user:ada repo:acme-org/api -repo:acme-org/legacy"
    )


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        ({"orgs": ["acme is:closed"]}, "invalid org filter"),
        ({"users": ["-ada"]}, "invalid user filter"),
        ({"repos": ["acme-org"]}, "invalid repo filter"),
        ({"exclude_repos": ["a/b c"]}, "invalid repo filter"),
        ({"orgs": [f"o{i}" for i in range(21)]}, "at most 20"),
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
    # with skip_covered (the default) the assigned search leaves review requests out
    default = run(plugin, "github.assigned", today="2026-10-30")
    assert "github:pr:PR_kwDOSYNTH0042" not in {i["fingerprint"] for i in default.intents}


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
        {**good, "__typename": "Commit"},
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
