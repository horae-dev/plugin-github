# SPDX-License-Identifier: Apache-2.0
"""GitHub's REST notifications: `GET /notifications?participating=true`, read only.

Why REST: the notifications API exists only in REST, and only for a **classic** personal
access token with the `notifications` scope. Fine-grained tokens (and GitHub App tokens) cannot
read the user's notifications at all, which is why this source has its own secret,
`GITHUB_NOTIFICATIONS_TOKEN`, rather than reusing the read-only fine-grained `GITHUB_TOKEN`:
the broader classic token is never sent with the search queries, and the search token never
needs the classic scope. The `notifications` scope alone is enough (no `repo`): the plugin
reads each notification's subject from the list and never fetches it.

**Read state is never modified.** The only method used is GET. Marking a thread read
(`PATCH /notifications/threads/{id}`, `PUT /notifications`) is never sent, and `Transport.get`
is the only entry point of this module.

**Bounded**: the same rules as the search (one deadline per call across pages and retries,
at most `ceil(max_items / 50)` pages of 50, no sleeping through a rate limit). A page whose
answer says `X-RateLimit-Remaining: 0` ends the walk (`truncated`) so the next call is not
the one that hits the limit. `X-Poll-Interval` is reported, never slept for.
"""

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from horae_github.client import MIN_PAGE_S, USER_AGENT, Client, GitHubError, _OutOfTime
from horae_github.http import Response, digits

PATH = "/notifications"
PAGE_SIZE = 50
SECRET = "GITHUB_NOTIFICATIONS_TOKEN"  # noqa: S105 (the secret's name, not its value)
NEXT_RE = re.compile(r'<[^>]*>\s*;[^,]*\brel="?next"?')
"""Only the *presence* of a `rel="next"` link is used; the next page number is ours, so a
link from the answer is never followed."""
MAX_POLL_INTERVAL_S = 86400


@dataclass(frozen=True)
class Notifications:
    threads: list[Any]
    """GitHub's thread objects, as returned (not checked)."""
    truncated: bool
    poll_interval: int | None
    """`X-Poll-Interval` (seconds GitHub asks to wait between polls), reported only."""
    rate_limit_remaining: int | None


class NotificationsClient(Client):
    secret = SECRET

    def __repr__(self) -> str:
        return "NotificationsClient(<token hidden>)"

    def participating(self, *, max_items: int, since: str | None = None) -> Notifications:
        """Unread notifications you participate in, at most `ceil(max_items / 50)` pages."""
        deadline = self._clock() + self._budget
        threads: list[Any] = []
        poll: int | None = None
        remaining: int | None = None
        truncated = False
        pages = -(-max_items // PAGE_SIZE)
        for number in range(1, pages + 1):
            if number > 1 and deadline - self._clock() < MIN_PAGE_S:
                return Notifications(threads, True, poll, remaining)
            query = {"all": "false", "participating": "true", "per_page": PAGE_SIZE}
            query["page"] = number
            if since is not None:
                query["since"] = since
            path = f"{PATH}?{urlencode(query)}"
            try:
                answer = self._fetch(path, deadline)
            except _OutOfTime:
                if number > 1:
                    return Notifications(threads, True, poll, remaining)
                raise GitHubError("github: request timed out") from None
            try:
                document = json.loads(answer.body)
            except (UnicodeDecodeError, ValueError, RecursionError):
                raise GitHubError("github: malformed response") from None
            if not isinstance(document, list):
                raise GitHubError("github: malformed response")
            threads += document
            interval = _number(answer.headers.get("x-poll-interval"))
            poll = min(interval, MAX_POLL_INTERVAL_S) if interval is not None else poll
            remaining = _number(answer.headers.get("x-ratelimit-remaining"))
            more = NEXT_RE.search(answer.headers.get("link", "")) is not None
            if not more or not document:
                break
            if number == pages or remaining == 0:
                truncated = True
                break
        if len(threads) > max_items:
            truncated = True
        return Notifications(threads[:max_items], truncated, poll, remaining)

    def _fetch(self, path: str, deadline: float) -> Response:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        return self._request(
            lambda: self._transport.get(path, headers, deadline=deadline), deadline
        )


def _number(value: str | None) -> int | None:
    return digits(value)
