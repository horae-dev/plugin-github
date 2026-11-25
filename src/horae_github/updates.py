# SPDX-License-Identifier: Apache-2.0
"""`github.updates`: issues and pull requests with something new for you, as a to-do.

All GraphQL, within one call deadline (`Client.deadline`):

- **Who you are**: `viewer { login }` (one request).
- **Candidates**: one search per `orgs` / `users` / `repos` value (`search_queries`), plus one
  `involves:@me` search anywhere (`involves_me`), each run as an `is:issue` and an `is:pr`
  search (GitHub returns nothing for a fine-grained token without one of them: up to 18
  searches, 36 requests, within the call deadline), each `archived:false updated:>=<cutoff>
  sort:updated-desc` (and `is:open` unless `include_closed`), at most `MAX_SEARCH_RESULTS`
  per search, merged by node id. Each node comes with its author, creation time and the last
  `TIMELINE_ITEMS` events of its timeline: comments, reviews, commits, force pushes, closes,
  reopens, merges and review requests.

**New for you** is decided from the timeline alone, so the plugin keeps no state. Your last
action is the latest event GitHub timestamps and attributes to you (a comment, a submitted
review, a force push, a close, a merge, a reopen, a review request you made), or the item's
creation when you opened it. A commit is never your action: its date and author are whatever
the committer wrote, so a forged commit "by you" in the future would hide real activity. Any
time after the end of the day following the run's day is clamped to it, and a commit's date to
the item's `updatedAt`.

The latest **other** event is the latest event by anyone else, bots left out unless
`include_bots` (an actor of type `Bot`, a login ending in `[bot]`, or, on a bot's own item, a
commit whose author is no GitHub account), and a review request
counted only when it asks you. The item is new when that event is after your last action (or
you never acted) and inside the window (`since_days` before the run's day). Reply, review or
push and it drops out on the next run. When your last action is older than the events read
(`TIMELINE_ITEMS`), every other event read is after it, which is what "new" means anyway.

**Since the last check** is the core's: with `notify`, one `notify.push` per new item, `key` =
its fingerprint, which holds the latest other event's id, so the core pushes each event once
and newer activity is a new key (schemas decision 57, amended). Nothing is stored here.

**Untrusted.** Titles and logins come from GitHub: anyone who can open an issue chooses its
title, and a login is shown as given. They reach data and push text only through
`ci.text` (`escape_title` then `plain_text`: control, bidi and zero-width characters removed,
bounded), and a note through `obsidian.render_note`, which escapes them again. Links are
rebuilt from the validated repository and the integer number
(`https://github.com/<repo>/pull|issues/<n>`), never taken from GitHub.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from horae_sdk import PluginError, intents
from horae_sdk.tasks import is_task_link

from horae_github.ci import _repo_name, text
from horae_github.client import LOGIN_RE, MIN_PAGE_S, Client, GitHubError, Hidden, _OutOfTime, merge

NAME = "github"
MAX_ITEMS = 100
MAX_WINDOW_DAYS = 90
MAX_SEARCH_RESULTS = 100
"""Candidates read per search (two pages), most recently updated first."""
TIMELINE_ITEMS = 25
MAX_TITLE = 150
MAX_LOGIN = 60
NODE_ID_RE = re.compile(r"[A-Za-z0-9_=-]{1,100}")
TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
TYPES = {"PullRequest": ("pr", "pull"), "Issue": ("issue", "issues")}
VERBS = {
    "opened": "opened",
    "commented": "commented",
    "reviewed": "reviewed",
    "approved": "approved",
    "changes_requested": "requested changes",
    "pushed": "pushed",
    "merged": "merged",
    "closed": "closed",
    "reopened": "reopened",
    "review_requested": "requested your review",
}
"""The closed set of `event` values, and how the title says them."""
REVIEW_EVENTS = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes_requested",
    "COMMENTED": "reviewed",
    "DISMISSED": "reviewed",
}
"""`PENDING` (a review not submitted yet) is not an event."""
ACTOR_EVENTS = {
    "IssueComment": "commented",
    "HeadRefForcePushedEvent": "pushed",
    "ClosedEvent": "closed",
    "ReopenedEvent": "reopened",
    "MergedEvent": "merged",
    "ReviewRequestedEvent": "review_requested",
}

_ACTOR = "{ __typename login }"
_EVENTS = f"""\
__typename
... on IssueComment {{ id createdAt author {_ACTOR} }}
... on ClosedEvent {{ id createdAt actor {_ACTOR} }}
... on ReopenedEvent {{ id createdAt actor {_ACTOR} }}"""
_PR_EVENTS = f"""\
{_EVENTS}
... on PullRequestReview {{ id state submittedAt author {_ACTOR} }}
... on PullRequestCommit {{ id commit {{ committedDate author {{ user {{ login }} }} }} }}
... on HeadRefForcePushedEvent {{ id createdAt actor {_ACTOR} }}
... on MergedEvent {{ id createdAt actor {_ACTOR} }}
... on ReviewRequestedEvent {{
  id createdAt actor {_ACTOR} requestedReviewer {{ __typename ... on User {{ login }} }}
}}"""
_COMMON = f"id number title createdAt updatedAt author {_ACTOR} repository {{ nameWithOwner }}"
QUERY = f"""\
query($q: String!, $first: Int!, $after: String) {{
  search(type: ISSUE, query: $q, first: $first, after: $after) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      __typename
      ... on Issue {{
        {_COMMON}
        issueTimeline: timelineItems(last: {TIMELINE_ITEMS},
            itemTypes: [ISSUE_COMMENT, CLOSED_EVENT, REOPENED_EVENT]) {{
          nodes {{ {_EVENTS} }}
        }}
      }}
      ... on PullRequest {{
        {_COMMON}
        prTimeline: timelineItems(last: {TIMELINE_ITEMS},
            itemTypes: [ISSUE_COMMENT, PULL_REQUEST_REVIEW, PULL_REQUEST_COMMIT,
                        HEAD_REF_FORCE_PUSHED_EVENT, CLOSED_EVENT, REOPENED_EVENT,
                        MERGED_EVENT, REVIEW_REQUESTED_EVENT]) {{
          nodes {{ {_PR_EVENTS} }}
        }}
      }}
    }}
  }}
}}"""
VIEWER_QUERY = "query { viewer { login } }"
LATEST = "9999-12-31T23:59:59Z"
"""No clamp (tests); a call clamps to the end of the day after the run's day."""


@dataclass(frozen=True)
class Event:
    kind: str
    """A key of `VERBS`."""
    at: str
    """`YYYY-MM-DDTHH:MM:SSZ`, compared as text."""
    login: str | None
    """GitHub's login as given (untrusted), `None` when unknown (a deleted account)."""
    bot: bool
    key: str
    """The event's node id, or `<kind>-<at>` without one: part of the fingerprint."""
    asks_viewer: bool = False
    """A review request whose reviewer is the viewer."""
    commit: bool = False
    """A pull request commit: its date and author are the committer's claim, not GitHub's."""


@dataclass(frozen=True)
class Update:
    type: str
    url_part: str
    node_id: str | None
    repo: str
    number: int
    title: str
    """Sanitised."""
    last: Event
    since: str | None
    """Your last action, or `None`."""

    @property
    def fingerprint(self) -> str:
        identity = self.node_id or f"{self.repo}#{self.number}"
        return intents.fingerprint(NAME, "update", identity, self.last.key)

    @property
    def item_fingerprint(self) -> str:
        """The item's fingerprint in the other search sources (`github:pr:<node id>`)."""
        if self.node_id is not None:
            return intents.fingerprint(NAME, self.type, self.node_id)
        return intents.fingerprint(NAME, self.type, self.repo, str(self.number))

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/{self.url_part}/{self.number}"

    @property
    def by(self) -> str:
        return text(self.last.login, MAX_LOGIN) if self.last.login else "someone"

    @property
    def line(self) -> str:
        """For `obsidian.render_note`, which prefixes the repository and the number."""
        return f"{self.title} — {self.by} {VERBS[self.last.kind]}"

    def data(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "item_fingerprint": self.item_fingerprint,
            "type": self.type,
            "repo": self.repo,
            "number": self.number,
            "item_title": self.title,
            "event": self.last.kind,
            "by": self.by,
            "at": self.last.at,
            "your_last_action": self.since,
            "url": self.url,
            "title": self.line,
        }

    def push(self) -> dict[str, Any]:
        # at most 201 + 11 + 60 + 22 characters: under the SDK's 500
        where = f"{text(self.repo, 201)}#{self.number}"
        return intents.notify_push(
            self.title or "(no title)",
            title=f"{where}: {self.by} {VERBS[self.last.kind]}",
            link=self.url,
            key=self.fingerprint,
        )


@dataclass
class Found:
    updates: list[Update] = field(default_factory=list)
    checked: int = 0
    """Well-formed items read."""
    filtered: int = 0
    """Read but nothing new for you (or excluded)."""
    skipped: int = 0
    truncated: bool = False
    errors: int = 0
    hidden: tuple[Hidden, ...] = ()


def _stamp(value: object) -> str | None:
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        return None
    try:
        date.fromisoformat(value[:10])
    except ValueError:
        return None
    return value


def _actor(value: object) -> tuple[str | None, bool] | None:
    """`(login, bot)`; `(None, False)` for a deleted account; `None` when malformed."""
    if value is None:
        return None, False
    if not isinstance(value, dict):
        return None
    login = value.get("login")
    if not isinstance(login, str) or not login:
        return None
    return login, value.get("__typename") == "Bot" or login.lower().endswith("[bot]")


def event(node: object, viewer: str) -> Event | None:
    """One timeline node, or `None` (malformed, or not an event that counts)."""
    if not isinstance(node, dict):
        return None
    typename = node.get("__typename")
    node_id = node.get("id")
    kind: str | None
    asks = False
    if typename == "PullRequestReview":
        kind = REVIEW_EVENTS.get(str(node.get("state")))
        at = _stamp(node.get("submittedAt"))
        actor = _actor(node.get("author"))
    elif typename == "PullRequestCommit":
        commit = node.get("commit")
        if not isinstance(commit, dict):
            return None
        kind = "pushed"
        at = _stamp(commit.get("committedDate"))
        author = commit.get("author")
        user = author.get("user") if isinstance(author, dict) else None
        # a commit whose author is no GitHub account (an automation's e-mail, mostly) counts
        # as a bot's: left out unless `include_bots`
        actor = _actor(user) if isinstance(user, dict) else (None, True)
    elif isinstance(typename, str) and typename in ACTOR_EVENTS:
        kind = ACTOR_EVENTS[typename]
        at = _stamp(node.get("createdAt"))
        actor = _actor(node.get("author" if typename == "IssueComment" else "actor"))
        if typename == "ReviewRequestedEvent":
            reviewer = node.get("requestedReviewer")
            login = reviewer.get("login") if isinstance(reviewer, dict) else None
            asks = isinstance(login, str) and login.lower() == viewer.lower()
    else:
        return None
    if kind is None or at is None or actor is None:
        return None
    key = node_id if isinstance(node_id, str) and NODE_ID_RE.fullmatch(node_id) else None
    commit = typename == "PullRequestCommit"
    return Event(kind, at, actor[0], actor[1], key or f"{kind}-{at}", asks, commit)


def judge(
    node: dict[str, Any], viewer: str, include_bots: bool, latest: str = LATEST
) -> tuple[Event | None, str | None]:
    """`(latest other event, your last action)` of one well-formed item. Times after `latest`
    (the end of the following day: GitHub's times are UTC, the run's day is in its time zone)
    are clamped to it.

    A commit's date and author are chosen by whoever made the commit (any e-mail, any date),
    so a commit is never your action, and its claims are bounded by what GitHub attests:
    its date by the item's `updatedAt` (a forged future date cannot outlive your next
    reply), its "bot" nature only on a bot's own item (an unlinked or bot e-mail is free to
    claim), and a commit in your name counts as someone's push on an item that is not
    yours (anyone can write your e-mail into a commit); on your own item it is ignored.
    Your actions are events GitHub timestamps itself and attributes to the authenticated
    actor."""
    me = viewer.lower()
    events: list[Event] = []
    opened = _stamp(node.get("createdAt"))
    author = _actor(node.get("author"))
    if opened is not None and author is not None:
        node_id = node.get("id")
        key = node_id if isinstance(node_id, str) and NODE_ID_RE.fullmatch(node_id) else None
        events.append(Event("opened", opened, author[0], author[1], key or f"opened-{opened}"))
    timeline = node.get("prTimeline", node.get("issueTimeline"))
    nodes = timeline.get("nodes") if isinstance(timeline, dict) else None
    for raw in nodes if isinstance(nodes, list) else []:
        found = event(raw, viewer)
        if found is not None:
            events.append(found)
    updated = _stamp(node.get("updatedAt"))
    own_item = author is not None and (author[0] or "").lower() == me
    bot_item = author is not None and author[1]
    mine: str | None = None
    other: Event | None = None
    for found in events:  # in timeline order: a later event wins a tie
        if found.commit:
            found = replace(found, bot=found.bot and bot_item)
            if updated is not None and found.at > updated:
                found = replace(found, at=updated)
            if found.login is not None and found.login.lower() == me:
                if own_item:
                    continue
                found = replace(found, login=None)
        if found.at > latest:
            found = replace(found, at=latest)
        if found.login is not None and found.login.lower() == me:
            if found.commit:
                continue
            mine = found.at if mine is None or found.at >= mine else mine
            continue
        if (found.bot and not include_bots) or (
            found.kind == "review_requested" and not found.asks_viewer
        ):
            continue
        # a merge and its close share a time: the merge says more
        if other is None or (found.at, found.kind == "merged") >= (
            other.at,
            other.kind == "merged",
        ):
            other = found
    return other, mine


def update(
    node: object, viewer: str, cutoff: str, include_bots: bool, latest: str = LATEST
) -> Update | str:
    """The item's update for you, or why there is none: `old` (nothing new for you, or
    nothing inside the window) or `malformed`."""
    if not isinstance(node, dict):
        return "malformed"
    typename = node.get("__typename")
    names = TYPES.get(typename) if isinstance(typename, str) else None
    repository = node.get("repository")
    repo = _repo_name(repository.get("nameWithOwner")) if isinstance(repository, dict) else None
    number = node.get("number")
    if names is None or repo is None or type(number) is not int or not 0 < number < 2**31:
        return "malformed"
    other, mine = judge(node, viewer, include_bots, latest)
    if other is None or other.at[:10] < cutoff or (mine is not None and other.at <= mine):
        return "old"
    title = node.get("title")
    node_id = node.get("id")
    found = Update(
        type=names[0],
        url_part=names[1],
        node_id=node_id if isinstance(node_id, str) and NODE_ID_RE.fullmatch(node_id) else None,
        repo=repo,
        number=number,
        title=text(title, MAX_TITLE) if isinstance(title, str) else "",
        last=other,
        since=mine,
    )
    # the link must be one the SDK (and the core) accepts; never raised on
    return found if is_task_link(found.url) else "malformed"


def whoami(client: Client, deadline: float) -> str:
    try:
        found, _hidden = client.graphql(VIEWER_QUERY, {}, "viewer", deadline)
    except _OutOfTime:
        raise GitHubError("github: request timed out") from None
    login = found.get("login") if isinstance(found, dict) else None
    if not isinstance(login, str) or not LOGIN_RE.fullmatch(login):
        raise GitHubError("github: malformed response")
    return login


def collect(
    client: Client,
    queries: Sequence[str],
    *,
    excluded: frozenset[str],
    cutoff: date,
    include_bots: bool,
    day: date | None = None,
) -> Found:
    """Every item with something new for you, newest first. Raises `GitHubError` when the
    viewer cannot be read or a search fails (rate limit, error); out of time after the viewer,
    what was read is returned, `truncated`."""
    deadline = client.deadline()
    viewer = whoami(client, deadline)
    nodes: list[Any] = []
    seen: set[str] = set()
    out = Found()
    hidden: list[Hidden] = []
    for query in queries:
        if client.time_left(deadline) < MIN_PAGE_S:
            out.truncated = True
            break
        found = client.search(
            [query], max_items=MAX_SEARCH_RESULTS, query=QUERY, deadline=deadline, started=True
        )
        out.truncated = out.truncated or found.truncated
        out.errors += found.errors
        hidden += found.hidden
        for node in found.nodes:
            node_id = node.get("id") if isinstance(node, dict) else None
            if isinstance(node_id, str):
                if node_id in seen:
                    continue
                seen.add(node_id)
            nodes.append(node)
    # GitHub's times are UTC and the run's day is in its own time zone: the end of the
    # following day is after every real event, whatever the zone
    latest = f"{(day + timedelta(days=1)).isoformat()}T23:59:59Z" if day is not None else LATEST
    for node in nodes:
        result = update(node, viewer, cutoff.isoformat(), include_bots, latest)
        if result == "malformed":
            out.skipped += 1
            continue
        out.checked += 1
        if isinstance(result, str) or result.repo.lower() in excluded:
            out.filtered += 1
            continue
        out.updates.append(result)
    out.hidden = merge(hidden)
    out.updates.sort(key=lambda u: (u.last.at, u.repo.lower(), u.number), reverse=True)
    return out


def check_window(days: object) -> int:
    if type(days) is not int or not 0 <= days <= MAX_WINDOW_DAYS:
        raise PluginError(f"github: since_days is 0 to {MAX_WINDOW_DAYS}")
    return days
