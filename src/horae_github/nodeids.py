# SPDX-License-Identifier: Apache-2.0
"""Node ids for the notifications source: one batched GraphQL read per 50 items.

The notifications API (REST) returns no GraphQL node id, so `github.notifications` could only
use the repository/number fallback fingerprint, which differs from the `github:pr:<node id>` of
`review_requests`, `assigned` and `mentions`: an item both proposed was two tasks. With the
search token (`GITHUB_TOKEN`) configured, the repository and number of each notification are
resolved to the node id the other sources use:

    query($o0: String!, $n0: String!, $i0: Int!, ...) {
      r0: repository(owner: $o0, name: $n0) { issueOrPullRequest(number: $i0) { ... } }
      ...
    }

Owner, name and number travel as GraphQL **variables**, never in the document. At most
`BATCH` (50) aliases per query, so `max_items` (at most 200) means at most 4 queries, all in
`BUDGET_S` seconds. Nothing here can fail the notifications call: an error, a timeout, a
repository this token cannot see (`NOT_FOUND` for that alias only) or an answer of the other
type leaves the item with the fallback fingerprint. The search token is used only here and
only in the `Authorization` header; errors are fixed messages and are not even reported.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from horae_github.client import MIN_PAGE_S, Client, GitHubError

BATCH = 50
BUDGET_S = 5.0
"""The lookups' own deadline: the notifications read already used part of the host's 30 s."""
NODE_ID_RE = re.compile(r"[A-Za-z0-9_=-]{1,100}")
TYPENAMES = {"pr": "PullRequest", "issue": "Issue"}

Ref = tuple[str, int, str]
"""`(owner/name, number, "pr" or "issue")`."""


@dataclass
class Lookup:
    ids: dict[Ref, str] = field(default_factory=dict)
    failed: int = 0
    """Refs of batches that failed or were not tried (the fallback applies)."""


def document(count: int) -> str:
    variables = ", ".join(f"$o{n}: String!, $n{n}: String!, $i{n}: Int!" for n in range(count))
    fields = "\n".join(
        f"  r{n}: repository(owner: $o{n}, name: $n{n}) {{ issueOrPullRequest(number: $i{n}) "
        "{ __typename ... on Issue { id } ... on PullRequest { id } } }"
        for n in range(count)
    )
    return f"query({variables}) {{\n{fields}\n}}"


def resolve(client: Client, refs: Sequence[Ref]) -> Lookup:
    """The node id of each (unique) ref the token can see, in batches of `BATCH`."""
    unique = list(dict.fromkeys(refs))
    out = Lookup()
    deadline = client.deadline(BUDGET_S)
    for start in range(0, len(unique), BATCH):
        batch = unique[start : start + BATCH]
        if client.time_left(deadline) < MIN_PAGE_S:
            out.failed += len(unique) - start
            break
        variables: dict[str, object] = {}
        for n, (repo, number, _kind) in enumerate(batch):
            owner, name = repo.split("/", 1)
            variables |= {f"o{n}": owner, f"n{n}": name, f"i{n}": number}
        try:
            data, _hiding = client.graphql(document(len(batch)), variables, "", deadline)
        except GitHubError:
            out.failed += len(batch)
            continue
        if not isinstance(data, dict):
            out.failed += len(batch)
            continue
        for n, ref in enumerate(batch):
            found = data.get(f"r{n}")
            node = found.get("issueOrPullRequest") if isinstance(found, dict) else None
            node_id = node.get("id") if isinstance(node, dict) else None
            if (
                isinstance(node, dict)
                and node.get("__typename") == TYPENAMES[ref[2]]
                and isinstance(node_id, str)
                and NODE_ID_RE.fullmatch(node_id)
            ):
                out.ids[ref] = node_id
    return out
