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
import re
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
LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}")
SCOPE_RE = re.compile(r"(?:^|\s)(?:org|user|repo):([A-Za-z0-9][A-Za-z0-9_-]{0,38})(?=/|\s|$)")
"""The first `org:`, `user:` or `repo:` qualifier of a search (a `-repo:` exclusion does not
match: its `-` is not a space); group 1 is the owner."""
REASONS = (
    "token_lifetime",
    "token_not_allowed",
    "saml",
    "ip_allow_list",
    "not_accessible",
    "forbidden",
)
"""The closed set of reasons a `FORBIDDEN` error is reduced to; GitHub's text is never kept."""
MAX_MESSAGE = 2000
RETURNED_REPO_RE = re.compile(r"[^\s/?#%\\\x00-\x1f\x7f]{1,100}/[^\s/?#%\\\x00-\x1f\x7f]{1,100}")
"""A repository GitHub returned: kept unless it could not be one (whitespace, control
characters, a second `/`, or `?#%\\` that would change the link built from it); GitHub's own
naming rules are not applied, so a valid item is never dropped by a stricter pattern."""

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
class Hidden:
    """`count` results GitHub hid for one reason (from `REASONS`), under one owner."""

    owner: str | None
    """A login that passed `LOGIN_RE`, taken from the query scope we sent (never from
    GitHub's text); `None` when unknown (an `involves:@me` search)."""
    reason: str
    count: int

    def data(self) -> dict[str, Any]:
        return {"owner": self.owner, "reason": self.reason, "count": self.count}


@dataclass(frozen=True)
class Hiding:
    """What one GraphQL request hid: `count` errors (results missing), `hidden` those of
    `FORBIDDEN` by owner and reason (a `NOT_FOUND` is counted but has no reason)."""

    count: int = 0
    hidden: tuple[Hidden, ...] = ()


def merge(*groups: Sequence[Hidden]) -> tuple[Hidden, ...]:
    """The groups summed per (owner, reason), sorted by owner (unknown first) then reason."""
    totals: dict[tuple[str | None, str], int] = {}
    for group in groups:
        for item in group:
            key = (item.owner, item.reason)
            totals[key] = totals.get(key, 0) + item.count
    return tuple(
        Hidden(owner, reason, count)
        for (owner, reason), count in sorted(totals.items(), key=lambda t: (t[0][0] or "", t[0][1]))
    )


def classify(error: Any) -> str:
    """The reason of one `FORBIDDEN` error: a member of `REASONS`, chosen by matching the
    message shapes GitHub is known to use and its `extensions` flags. The text itself is
    only compared, never returned."""
    extensions = error.get("extensions") if isinstance(error, dict) else None
    if isinstance(extensions, dict):
        if extensions.get("saml_failure") is True:
            return "saml"
        if extensions.get("ip_allow_list_failure") is True:
            return "ip_allow_list"
    message = error.get("message") if isinstance(error, dict) else None
    low = message[:MAX_MESSAGE].lower() if isinstance(message, str) else ""
    if "saml" in low:
        return "saml"
    if "ip allow list" in low:
        return "ip_allow_list"
    if "forbids access via a fine-grained personal access token" in low:
        return "token_lifetime" if "lifetime" in low else "token_not_allowed"
    if "forbids access via" in low and "personal access token" in low:
        return "token_not_allowed"
    if "has not approved" in low or "not been approved" in low:
        return "token_not_allowed"
    if "resource not accessible by personal access token" in low:
        return "not_accessible"
    return "forbidden"


def query_owner(text: str) -> str | None:
    """The owner a search string is scoped to (`org:`, `user:` or `repo:`), `None` without
    one (`involves:@me`)."""
    found = SCOPE_RE.search(text)
    return found.group(1) if found else None


def _check_context(data: Any, error: Any) -> bool:
    """A `FORBIDDEN` error on a check run or status of a commit whose rollup `state` was
    read: the checks are hidden, no result is missing."""
    path = error.get("path") if isinstance(error, dict) else None
    if not isinstance(path, list) or error.get("type") != "FORBIDDEN":
        return False
    if "statusCheckRollup" not in path:
        return False
    at = path.index("statusCheckRollup")
    if path[at + 1 : at + 2] != ["contexts"]:
        return False
    node = data
    for key in path[: at + 1]:
        if isinstance(key, str) and isinstance(node, dict):
            node = node.get(key)
        elif type(key) is int and isinstance(node, list) and 0 <= key < len(node):
            node = node[key]
        else:
            return False
    return isinstance(node, dict) and isinstance(node.get("state"), str)


@dataclass(frozen=True)
class Search:
    nodes: list[Any]
    """GitHub's nodes, as returned (not checked), one per node id."""
    truncated: bool
    """More results exist past `max_items`, or the deadline cut the search short."""
    errors: int = 0
    """GraphQL `FORBIDDEN` / `NOT_FOUND` errors that came with data: results are missing."""
    hidden: tuple[Hidden, ...] = ()
    """The `FORBIDDEN` ones, by owner and reason."""


@dataclass(frozen=True)
class _Page:
    nodes: list[Any]
    has_next: bool
    cursor: str | None
    errors: int
    hidden: tuple[Hidden, ...] = ()


class Client:
    """Searches with one token. The token goes only into the `Authorization` header."""

    secret = "GITHUB_TOKEN"  # noqa: S105 (the secret's name, shown in the 401 message)

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

    def deadline(self) -> float:
        """The deadline of a call that starts now: `budget` seconds on the client's clock."""
        return self._clock() + self._budget

    def time_left(self, deadline: float) -> float:
        return deadline - self._clock()

    def search(
        self,
        queries: Sequence[str],
        *,
        max_items: int,
        query: str = QUERY,
        deadline: float | None = None,
        started: bool = False,
    ) -> Search:
        """Up to `max_items` issues and pull requests matching any of `queries`, in GitHub's
        order, query after query, each node id once; at most `ceil(max_items / 50)` requests
        per query, all within one deadline (`deadline`, shared with the caller's other
        requests, or `budget` seconds from now). `query` is the GraphQL document, whose
        `search` selection decides the nodes' fields. `started`: the caller already has data
        from this call, so running out of time before the first page is `truncated`, not an
        error."""
        if deadline is None:
            deadline = self.deadline()
        nodes: list[Any] = []
        seen: set[str] = set()
        errors = 0
        hidden: list[Hidden] = []
        pages = 1 if started else 0
        truncated = False

        def keep(node: Any) -> None:
            node_id = node.get("id") if isinstance(node, dict) else None
            if isinstance(node_id, str):
                if node_id in seen:
                    return
                seen.add(node_id)
            nodes.append(node)

        for text in queries:
            after: str | None = None
            fetched = 0
            while True:
                if pages and deadline - self._clock() < MIN_PAGE_S:
                    return Search(nodes[:max_items], True, errors, merge(hidden))
                try:
                    page = self._page(
                        query, text, min(PAGE_SIZE, max_items - fetched), after, deadline
                    )
                except _OutOfTime:
                    if pages:
                        return Search(nodes[:max_items], True, errors, merge(hidden))
                    raise GitHubError("github: request timed out") from None
                pages += 1
                errors += page.errors
                hidden += page.hidden
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
            return Search(nodes[:max_items], True, errors, merge(hidden))
        return Search(nodes, truncated, errors, merge(hidden))

    def _page(self, query: str, text: str, first: int, after: str | None, deadline: float) -> _Page:
        variables = {"q": text, "first": first, "after": after}
        search, hiding = self.graphql(query, variables, "search", deadline, owner=query_owner(text))
        page_info = _get(search, "pageInfo")
        nodes = _get(search, "nodes")
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise GitHubError("github: malformed response")
        cursor = page_info.get("endCursor")
        return _Page(
            nodes,
            page_info.get("hasNextPage") is True,
            cursor if isinstance(cursor, str) else None,
            hiding.count,
            hiding.hidden,
        )

    def graphql(
        self,
        query: str,
        variables: Mapping[str, Any],
        root: str,
        deadline: float,
        *,
        allow_missing: bool = False,
        owner: str | None = None,
    ) -> tuple[Any, Hiding]:
        """`data.<root>` of one GraphQL request, and what came hidden with it: how many
        `FORBIDDEN` / `NOT_FOUND` errors (results hidden, the rest kept), the `FORBIDDEN` ones
        by reason under `owner` (the owner the request was scoped to, if it was; checked
        against `LOGIN_RE`). A `FORBIDDEN` error on the checks of a commit whose rollup state
        was read is not counted: the checks are hidden, no result is missing. A `RATE_LIMITED`
        error, any other error,
        or errors without a `data.<root>` object raise `GitHubError` (fixed message);
        `allow_missing` accepts a null `data.<root>` with only those two kinds of errors (an
        owner the token may not see: the caller counts it and goes on)."""
        body = json.dumps({"query": query, "variables": dict(variables)}).encode()
        answer = self._post(body, deadline)
        try:
            document = json.loads(answer.body)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise GitHubError("github: malformed response") from None
        if not isinstance(document, dict):
            raise GitHubError("github: malformed response")
        found = _get(document, "data", root)
        errors = document.get("errors")
        hiding = Hiding()
        if errors:
            types = _types(errors)
            if "RATE_LIMITED" in types:
                raise RateLimited(self._retry_after(answer.headers))
            if (
                not (isinstance(found, dict) or (allow_missing and found is None))
                or not isinstance(errors, list)
                or len(types) != len(errors)
                or not set(types) <= PARTIAL_ERROR_TYPES
            ):
                known = sorted({kind for kind in types if kind in KNOWN_ERROR_TYPES})
                raise GitHubError(f"github: query failed ({', '.join(known) or 'unknown error'})")
            scope = owner if owner is not None and LOGIN_RE.fullmatch(owner) else None
            data = document.get("data")
            real = [error for error in errors if not _check_context(data, error)]
            reasons = [classify(error) for error in real if error.get("type") == "FORBIDDEN"]
            hiding = Hiding(len(real), merge([Hidden(scope, reason, 1) for reason in reasons]))
        return found, hiding

    def _post(self, body: bytes, deadline: float) -> Response:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        }
        return self._request(
            lambda: self._transport.post(PATH, body, headers, deadline=deadline), deadline
        )

    def _request(self, send: Callable[[], Response], deadline: float) -> Response:
        """`send()` with one retry of a gateway error, then the status check."""
        for attempt in range(2):
            try:
                answer = send()
            except DeadlineExceeded:
                raise _OutOfTime("github: request timed out") from None
            except TransportError as error:
                raise GitHubError(str(error)) from None
            room = deadline - self._clock() > RETRY_PAUSE_S + MIN_PAGE_S
            if answer.status in RETRY_STATUSES and attempt == 0 and room:
                self._sleep(RETRY_PAUSE_S)
                continue
            break
        self._check(answer)
        return answer

    def _check(self, answer: Response) -> None:
        """Raise the fixed error for any status but 200."""
        status = answer.status
        if status == 200:
            return
        if status == 401:
            raise GitHubError(f"github: authentication failed ({self.secret} refused)")
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
