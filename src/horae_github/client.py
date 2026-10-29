# SPDX-License-Identifier: Apache-2.0
"""GitHub's GraphQL `search`: one query per page, bounded pages, no long sleeps.

Why GraphQL rather than REST: the three kinds are plain search qualifiers
(`review-requested:@me`, `assignee:@me`, `mentions:@me`), and one GraphQL `search` returns
exactly the fields a task needs (node id, number, title, repository, milestone due date) for
issues and pull requests alike, 50 per request. REST `/search/issues` would need the same
number of calls, its own 30-requests-per-minute search budget, and a second call per item for
the milestone's due date when only its URL is given; it also returns whole user and label
objects the plugin would have to drop. GraphQL works with a fine-grained personal access
token (read-only `Pull requests`, `Issues` and `Metadata` repository permissions).

Every failure is a `GitHubError` with a fixed message: the token, a header or the body of an
answer never reaches it.
"""

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from horae_github.http import Response, Transport, TransportError

PATH = "/graphql"
USER_AGENT = "horae-plugin-github"
PAGE_SIZE = 50
MAX_RETRY_AFTER_S = 3600
RETRY_STATUSES = frozenset({502, 503, 504})
RETRY_PAUSE_S = 1.0
"""One retry, after this pause, for a gateway error. A rate limit is never waited for."""
KNOWN_ERROR_TYPES = frozenset(
    {"FORBIDDEN", "NOT_FOUND", "UNAUTHORIZED", "INTERNAL", "SERVICE_UNAVAILABLE", "TIMEOUT"}
)

QUERY = """\
query($q: String!, $first: Int!, $after: String) {
  search(type: ISSUE, query: $q, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      __typename
      ... on PullRequest { id number title repository { nameWithOwner } milestone { dueOn } }
      ... on Issue { id number title repository { nameWithOwner } milestone { dueOn } }
    }
  }
}"""


class GitHubError(Exception):
    """A failed search (fixed message, safe to show and record)."""


class RateLimited(GitHubError):
    """GitHub asked to wait. Reported at once (the step is degraded), never slept through."""

    def __init__(self, retry_after: int | None) -> None:
        self.retry_after = retry_after
        when = "" if retry_after is None else f"; retry after {retry_after} s"
        super().__init__(f"github: rate limited{when}")


@dataclass(frozen=True)
class Search:
    nodes: list[Mapping[str, Any]]
    truncated: bool
    """More results exist past `max_items`."""


class Client:
    """Searches with one token. The token goes only into the `Authorization` header."""

    def __init__(
        self,
        token: str,
        transport: Transport,
        *,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._token = token
        self._transport = transport
        self._sleep, self._wall = sleep, wall

    def __repr__(self) -> str:
        return "Client(<token hidden>)"

    def search(self, query: str, *, max_items: int) -> Search:
        """Up to `max_items` issues and pull requests matching `query`, in GitHub's order,
        in at most `ceil(max_items / 50)` requests."""
        nodes: list[Mapping[str, Any]] = []
        after: str | None = None
        while True:
            first = min(PAGE_SIZE, max_items - len(nodes))
            page, has_next, cursor = self._page(query, first, after)
            nodes.extend(page)
            if not has_next:
                return Search(nodes, truncated=False)
            if len(nodes) >= max_items or cursor is None or not page:
                return Search(nodes[:max_items], truncated=True)
            after = cursor

    def _page(
        self, query: str, first: int, after: str | None
    ) -> tuple[list[Mapping[str, Any]], bool, str | None]:
        variables = {"q": query, "first": first, "after": after}
        body = json.dumps({"query": QUERY, "variables": variables}).encode()
        answer = self._post(body)
        try:
            document = json.loads(answer.body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GitHubError("github: malformed response") from None
        if not isinstance(document, dict):
            raise GitHubError("github: malformed response")
        errors = document.get("errors")
        if errors:
            raise self._graphql_error(errors, answer)
        search = _get(document, "data", "search")
        page_info = _get(search, "pageInfo")
        nodes = _get(search, "nodes")
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise GitHubError("github: malformed response")
        cursor = page_info.get("endCursor")
        has_next = page_info.get("hasNextPage") is True
        return (
            [node for node in nodes if isinstance(node, dict)],
            has_next,
            cursor if isinstance(cursor, str) else None,
        )

    def _post(self, body: bytes) -> Response:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        }
        for attempt in range(2):
            try:
                answer = self._transport.post(PATH, body, headers)
            except TransportError as error:
                raise GitHubError(str(error)) from None
            if answer.status in RETRY_STATUSES and attempt == 0:
                self._sleep(RETRY_PAUSE_S)
                continue
            break
        status = answer.status
        if status == 200:
            return answer
        if status == 401:
            raise GitHubError("github: authentication failed (GITHUB_TOKEN refused)")
        if status in (403, 429):
            limited = self._rate_limited(answer.headers)
            if limited is not None or status == 429:
                raise RateLimited(limited)
            raise GitHubError("github: access denied (HTTP 403)")
        if 300 <= status < 400:
            raise GitHubError(f"github: unexpected redirect (HTTP {status}), not followed")
        if status in RETRY_STATUSES:
            raise GitHubError(f"github: service unavailable (HTTP {status})")
        raise GitHubError(f"github: unexpected answer (HTTP {status})")

    def _rate_limited(self, headers: Mapping[str, str]) -> int | None:
        """Seconds to wait from `Retry-After` or `X-RateLimit-Reset` (when the remaining
        budget is 0), capped; `None` when the answer says neither."""
        retry_after = headers.get("retry-after", "").strip()
        if retry_after.isdigit():
            return min(int(retry_after), MAX_RETRY_AFTER_S)
        reset = headers.get("x-ratelimit-reset", "").strip()
        if headers.get("x-ratelimit-remaining", "").strip() == "0" and reset.isdigit():
            return max(0, min(int(reset) - int(self._wall()), MAX_RETRY_AFTER_S))
        return None

    def _graphql_error(self, errors: Any, answer: Response) -> GitHubError:
        types = (
            [error.get("type") for error in errors if isinstance(error, dict)]
            if isinstance(errors, list)
            else []
        )
        if "RATE_LIMITED" in types:
            return RateLimited(self._rate_limited(answer.headers))
        known = sorted({kind for kind in types if kind in KNOWN_ERROR_TYPES})
        return GitHubError(f"github: query failed ({', '.join(known) or 'unknown error'})")


def _get(value: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value
