# SPDX-License-Identifier: Apache-2.0
"""GitHub's GraphQL `search`: one query per page, bounded pages, one deadline, no long sleeps.

Why GraphQL rather than REST: the three kinds are plain search qualifiers
(`review-requested:@me`, `assignee:@me`, `mentions:@me`), and one GraphQL `search` returns
exactly the fields a task needs (node id, number, title, repository, milestone due date) for
issues and pull requests alike, 50 per request. REST `/search/issues` would need the same
number of calls, its own 30-requests-per-minute search budget, and a second call per item for
the milestone's due date when only its URL is given; it also returns whole user and label
objects the plugin would have to drop. GraphQL works with a fine-grained personal access
token (read-only `Pull requests`, `Issues` and `Metadata` repository permissions).

**One deadline per call.** `Client.search` runs every query and page of one source call
within `CALL_DEADLINE_S` (25 s, under the host's 30 s call timeout), retries included. When
the time runs out after at least one page, it returns what it has with `truncated` set.

Every failure is a `GitHubError` with a fixed message: the token, a header or the body of an
answer never reaches it.
"""

import email.utils
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from horae_github.http import (
    CALL_DEADLINE_S,
    DeadlineExceeded,
    Response,
    Transport,
    TransportError,
    digits,
)

PATH = "/graphql"
USER_AGENT = "horae-plugin-github"
PAGE_SIZE = 50
MAX_RETRY_AFTER_S = 3600
RETRY_STATUSES = frozenset({502, 503, 504})
RETRY_PAUSE_S = 1.0
"""One retry, after this pause, for a gateway error (when the deadline leaves room). A rate
limit is never waited for."""
MIN_PAGE_S = 0.5
"""A further page is not started with less time than this left."""
KNOWN_ERROR_TYPES = frozenset(
    {"FORBIDDEN", "NOT_FOUND", "UNAUTHORIZED", "INTERNAL", "SERVICE_UNAVAILABLE", "TIMEOUT"}
)
PARTIAL_ERROR_TYPES = frozenset({"FORBIDDEN", "NOT_FOUND"})
"""GraphQL errors that hide some results (an organisation enforcing SAML on the token, a
repository the token cannot see) while `data` still holds the visible ones."""
SECONDARY_LIMIT = b"secondary rate limit"

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

    def __init__(self, retry_after: int | None, *, certain: bool = True) -> None:
        self.retry_after = retry_after
        if retry_after is not None:
            message = f"github: rate limited; retry after {retry_after} s"
        elif certain:
            message = "github: rate limited; retry later"
        else:
            message = "github: possibly rate limited; retry later"
        super().__init__(message)


class _OutOfTime(GitHubError):
    """The call's deadline passed during a page."""


@dataclass(frozen=True)
class Search:
    nodes: list[Any]
    """GitHub's nodes, as returned (not checked), one per node id."""
    truncated: bool
    """More results exist past `max_items`, or the deadline cut the search short."""
    errors: int = 0
    """GraphQL `FORBIDDEN` / `NOT_FOUND` errors that came with data: results are missing."""


@dataclass(frozen=True)
class _Page:
    nodes: list[Any]
    has_next: bool
    cursor: str | None
    errors: int


class Client:
    """Searches with one token. The token goes only into the `Authorization` header."""

    def __init__(
        self,
        token: str,
        transport: Transport,
        *,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
        clock: Callable[[], float] = time.monotonic,
        budget: float = CALL_DEADLINE_S,
    ) -> None:
        self._token = token
        self._transport = transport
        self._sleep, self._wall, self._clock, self._budget = sleep, wall, clock, budget

    def __repr__(self) -> str:
        return "Client(<token hidden>)"

    def search(self, queries: Sequence[str], *, max_items: int) -> Search:
        """Up to `max_items` issues and pull requests matching any of `queries`, in GitHub's
        order, query after query, each node id once; at most `ceil(max_items / 50)` requests
        per query, all within one deadline."""
        deadline = self._clock() + self._budget
        nodes: list[Any] = []
        seen: set[str] = set()
        errors = 0
        pages = 0
        truncated = False

        def keep(node: Any) -> None:
            node_id = node.get("id") if isinstance(node, dict) else None
            if isinstance(node_id, str):
                if node_id in seen:
                    return
                seen.add(node_id)
            nodes.append(node)

        for query in queries:
            after: str | None = None
            fetched = 0
            while True:
                if pages and deadline - self._clock() < MIN_PAGE_S:
                    return Search(nodes[:max_items], True, errors)
                try:
                    page = self._page(query, min(PAGE_SIZE, max_items - fetched), after, deadline)
                except _OutOfTime:
                    if pages:
                        return Search(nodes[:max_items], True, errors)
                    raise GitHubError("github: request timed out") from None
                pages += 1
                errors += page.errors
                fetched += len(page.nodes)
                for node in page.nodes:
                    keep(node)
                if not page.has_next:
                    break
                if fetched >= max_items or page.cursor is None or not page.nodes:
                    truncated = True
                    break
                after = page.cursor
        if len(nodes) > max_items:
            return Search(nodes[:max_items], True, errors)
        return Search(nodes, truncated, errors)

    def _page(self, query: str, first: int, after: str | None, deadline: float) -> _Page:
        variables = {"q": query, "first": first, "after": after}
        body = json.dumps({"query": QUERY, "variables": variables}).encode()
        answer = self._post(body, deadline)
        try:
            document = json.loads(answer.body)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise GitHubError("github: malformed response") from None
        if not isinstance(document, dict):
            raise GitHubError("github: malformed response")
        search = _get(document, "data", "search")
        errors = document.get("errors")
        partial = 0
        if errors:
            types = _types(errors)
            if "RATE_LIMITED" in types:
                raise RateLimited(self._retry_after(answer.headers))
            if (
                not isinstance(search, dict)
                or not isinstance(errors, list)
                or len(types) != len(errors)
                or not set(types) <= PARTIAL_ERROR_TYPES
            ):
                known = sorted({kind for kind in types if kind in KNOWN_ERROR_TYPES})
                raise GitHubError(f"github: query failed ({', '.join(known) or 'unknown error'})")
            partial = len(errors)
        page_info = _get(search, "pageInfo")
        nodes = _get(search, "nodes")
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise GitHubError("github: malformed response")
        cursor = page_info.get("endCursor")
        return _Page(
            nodes,
            page_info.get("hasNextPage") is True,
            cursor if isinstance(cursor, str) else None,
            partial,
        )

    def _post(self, body: bytes, deadline: float) -> Response:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        }
        for attempt in range(2):
            try:
                answer = self._transport.post(PATH, body, headers, deadline=deadline)
            except DeadlineExceeded:
                raise _OutOfTime("github: request timed out") from None
            except TransportError as error:
                raise GitHubError(str(error)) from None
            room = deadline - self._clock() > RETRY_PAUSE_S + MIN_PAGE_S
            if answer.status in RETRY_STATUSES and attempt == 0 and room:
                self._sleep(RETRY_PAUSE_S)
                continue
            break
        status = answer.status
        if status == 200:
            return answer
        if status == 401:
            raise GitHubError("github: authentication failed (GITHUB_TOKEN refused)")
        if status in (403, 429):
            limited = self._rate_limited(status, answer)
            if limited is not None:
                raise limited
            raise GitHubError("github: access denied (HTTP 403)")
        if 300 <= status < 400:
            raise GitHubError(f"github: unexpected redirect (HTTP {status}), not followed")
        if status in RETRY_STATUSES:
            raise GitHubError(f"github: service unavailable (HTTP {status})")
        raise GitHubError(f"github: unexpected answer (HTTP {status})")

    def _rate_limited(self, status: int, answer: Response) -> RateLimited | None:
        """A 429, or a 403 with `Retry-After`, `X-RateLimit-Remaining: 0` or GitHub's
        secondary rate limit message; `None` for any other 403."""
        retry_after = self._retry_after(answer.headers)
        exhausted = answer.headers.get("x-ratelimit-remaining", "").strip() == "0"
        if status == 429 or retry_after is not None or exhausted:
            return RateLimited(retry_after, certain=status == 429 or exhausted)
        if SECONDARY_LIMIT in answer.body[:65536].lower():
            return RateLimited(None, certain=False)
        return None

    def _retry_after(self, headers: Mapping[str, str]) -> int | None:
        """Seconds to wait from `Retry-After` (seconds or an HTTP date) or `X-RateLimit-Reset`
        (when the remaining budget is 0), capped; `None` when the answer says neither."""
        retry_after = headers.get("retry-after", "").strip()
        seconds = digits(retry_after)
        if seconds is not None:
            return min(seconds, MAX_RETRY_AFTER_S)
        if retry_after:
            try:
                when = email.utils.parsedate_to_datetime(retry_after)
            except (TypeError, ValueError, IndexError, OverflowError):
                when = None
            if when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                return self._clamp(when.timestamp() - self._wall())
        reset = digits(headers.get("x-ratelimit-reset"))
        if headers.get("x-ratelimit-remaining", "").strip() == "0" and reset is not None:
            return self._clamp(reset - self._wall())
        return None

    @staticmethod
    def _clamp(seconds: float) -> int:
        return max(0, min(int(seconds), MAX_RETRY_AFTER_S))


def _types(errors: Any) -> list[str | None]:
    if not isinstance(errors, list):
        return []
    kinds = [error.get("type") for error in errors if isinstance(error, dict)]
    return [kind if isinstance(kind, str) else None for kind in kinds]


def _get(value: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value
