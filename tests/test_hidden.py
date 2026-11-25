# SPDX-License-Identifier: Apache-2.0
"""Results GitHub hides from a token: the reasons (a closed set, never GitHub's text), the
owner (from the query we sent), and checks hidden under a readable rollup state."""

import json
from collections.abc import Mapping
from typing import Any

import pytest

from conftest import TOKEN
from horae_github import ci
from horae_github.client import REASONS, Client, Hidden, classify, merge, query_owner
from horae_github.http import Response

LIFETIME = (
    "The 'acme' organization forbids access via a fine-grained personal access tokens if the "
    "token's lifetime is greater than 366 days. Adjust it at https://github.com/settings/x"
)


class Canned:
    def __init__(self, document: dict[str, Any]) -> None:
        self.body = json.dumps(document).encode()

    def post(
        self, path: str, body: bytes, headers: Mapping[str, str], *, deadline: float
    ) -> Response:
        return Response(200, {}, self.body)


def search(nodes: list[Any], errors: list[Any], query: str) -> Any:
    document = {
        "data": {"search": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}},
        "errors": errors,
    }
    return Client(TOKEN, Canned(document)).search([query], max_items=10)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ({"message": LIFETIME}, "token_lifetime"),
        (
            {
                "message": "The 'a' organization forbids access via a fine-grained personal access "
                "tokens."
            },
            "token_not_allowed",
        ),
        ({"message": "The 'a' organization has not approved this token"}, "token_not_allowed"),
        ({"message": "x", "extensions": {"saml_failure": True}}, "saml"),
        ({"message": "Resource protected by organization SAML enforcement."}, "saml"),
        ({"message": "x", "extensions": {"ip_allow_list_failure": True}}, "ip_allow_list"),
        ({"message": "Resource not accessible by personal access token"}, "not_accessible"),
        ({"message": "something new"}, "forbidden"),
        ({"message": 5, "extensions": "x"}, "forbidden"),
        ({}, "forbidden"),
    ],
)
def test_classify(error: dict[str, Any], reason: str) -> None:
    assert classify(error) == reason
    assert reason in REASONS


@pytest.mark.parametrize(
    ("text", "owner"),
    [
        ("is:pr org:acme -repo:x/y", "acme"),
        ("is:issue user:me-1 is:open", "me-1"),
        ("is:pr repo:acme/api -repo:acme/old", "acme"),
        ("is:issue involves:@me is:open -repo:acme/old", None),
        ("is:pr foo-org:acme", None),
    ],
)
def test_query_owner(text: str, owner: str | None) -> None:
    assert query_owner(text) == owner


def test_hidden_items_are_counted_by_owner_and_reason_without_github_text() -> None:
    errors = [
        {"type": "FORBIDDEN", "path": ["search", "nodes", i], "message": LIFETIME} for i in (0, 1)
    ]
    found = search([None, None], errors, "is:pr org:acme")
    assert found.errors == 2
    assert found.hidden == (Hidden("acme", "token_lifetime", 2),)
    assert "github.com" not in repr(found.hidden)
    assert "lifetime is" not in repr(found)


def test_an_involves_search_has_no_owner() -> None:
    errors = [{"type": "FORBIDDEN", "message": "Resource not accessible by personal access token"}]
    found = search([None], errors, "is:pr involves:@me")
    assert found.hidden == (Hidden(None, "not_accessible", 1),)


def test_not_found_is_counted_but_has_no_reason() -> None:
    found = search([None], [{"type": "NOT_FOUND", "message": "gone"}], "is:pr org:acme")
    assert (found.errors, found.hidden) == (1, ())


def test_merge_sums_and_sorts() -> None:
    merged = merge(
        [Hidden("b", "saml", 1), Hidden(None, "forbidden", 1)],
        [Hidden("b", "saml", 2), Hidden("a", "saml", 1)],
    )
    assert [(h.owner, h.reason, h.count) for h in merged] == [
        (None, "forbidden", 1),
        ("a", "saml", 1),
        ("b", "saml", 3),
    ]


def rollup_document(state: str | None, nodes: list[Any]) -> dict[str, Any]:
    """A search of pull requests whose head commit has a rollup, and an error for each null."""
    rollup = {"state": state, "contexts": {"totalCount": len(nodes), "nodes": nodes}}
    path = ["search", "nodes", 0, "commits", "nodes", 0, "commit", "statusCheckRollup"]
    return {
        "data": {
            "search": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]}}],
            }
        },
        "errors": [
            {"type": "FORBIDDEN", "path": [*path, "contexts", "nodes", i], "message": "x"}
            for i, node in enumerate(nodes)
            if node is None
        ],
    }


def test_hidden_contexts_under_a_readable_state_are_no_error() -> None:
    client = Client(TOKEN, Canned(rollup_document("FAILURE", [None, None])))  # type: ignore[arg-type]
    found = client.search(["is:pr org:acme"], max_items=10)
    assert (found.errors, found.hidden) == (0, ())


def test_hidden_contexts_without_a_readable_state_are_errors() -> None:
    client = Client(TOKEN, Canned(rollup_document(None, [None])))  # type: ignore[arg-type]
    found = client.search(["is:pr org:acme"], max_items=10)
    assert found.errors == 1
    assert found.hidden == (Hidden("acme", "forbidden", 1),)


def test_a_failure_with_some_checks_hidden_lists_the_visible_ones() -> None:
    commit = {
        "oid": "a" * 40,
        "committedDate": "2026-10-29T08:00:00Z",
        "statusCheckRollup": {
            "state": "FAILURE",
            "contexts": {
                "totalCount": 2,
                "nodes": [
                    None,
                    {"__typename": "StatusContext", "context": "lint", "state": "FAILURE"},
                ],
            },
        },
    }
    found = ci.failure("acme/api", "main", None, commit)
    assert isinstance(found, ci.Failure)
    assert found.checks == ("lint",)
    assert found.checks_hidden
    assert found.what == "lint (more checks hidden from this token)"
    assert found.data()["checks_hidden"] is True


def test_a_failure_with_readable_checks_is_not_hidden() -> None:
    commit = {
        "oid": "a" * 40,
        "committedDate": "2026-10-29T08:00:00Z",
        "statusCheckRollup": {
            "state": "FAILURE",
            "contexts": {
                "totalCount": 1,
                "nodes": [{"__typename": "StatusContext", "context": "lint", "state": "FAILURE"}],
            },
        },
    }
    found = ci.failure("acme/api", "main", None, commit)
    assert isinstance(found, ci.Failure)
    assert not found.checks_hidden
