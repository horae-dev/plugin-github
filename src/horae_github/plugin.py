# SPDX-License-Identifier: Apache-2.0
"""The plugin: three sources, one per kind of GitHub work waiting for you.

- `github.review_requests`: open pull requests whose review is requested from you;
- `github.assigned`: open issues and pull requests assigned to you;
- `github.mentions`: open issues and pull requests that mention you.

Each item becomes a `task.create` intent (title escaped by `intents.task_create`, a due date
from the kind's policy, a fingerprint from GitHub's node id) and an entry of the returned data
with the item's link. Titles, like everything GitHub returns, are **untrusted**: anyone who can
open a pull request chooses its title.

**One task per item.** The fingerprint is `github:pr:<node id>` or `github:issue:<node id>`:
the item's identity, not the kind that found it, so a pull request that is both assigned and
waiting for your review is the same task wherever it comes from. With `skip_covered` (off by
default) `github.assigned` also leaves out what `github.review_requests` returns and
`github.mentions` leaves out both (search qualifiers `-review-requested:@me`,
`-assignee:@me`), so one run proposes each item once, with the strongest kind's title.

**Filters.** Each `orgs`, `users` and `repos` value is its own search (at most
`MAX_SEARCHES` per call), and the results are merged, each node once: an item matches when it
matches any of the values. `exclude_repos` applies to every search.

- `github.notifications`: unread notifications you participate in (REST, read only, a classic
  token in its own secret `GITHUB_NOTIFICATIONS_TOKEN`; see `notifications.py`). The API gives
  no node id, so its fingerprint is the repository-and-number fallback and does **not** collapse
  with the node-id fingerprint of the search sources.

The tokens are declared secrets, read at call time; they never appear in data, intents, errors
or logs.
"""

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from horae_sdk import Call, Output, Permissions, Plugin, PluginError, intents, tasks
from horae_sdk.tasks import is_task_link

from horae_github import due
from horae_github.client import Client, GitHubError
from horae_github.http import HOST, Transport
from horae_github.notifications import SECRET as NOTIFICATIONS_SECRET
from horae_github.notifications import NotificationsClient

NAME = "github"
"""The plugin's name, also the namespace of its refs (`github.review_requests`) and
fingerprints (`github:pr:<node id>`)."""
VERSION = "0.2.0"
COMMAND = ["python3", "-m", "horae_github"]
"""`runtime.command` of `plugin.yaml`. When installing, pin an absolute interpreter that has
this package (the host's `PATH` is fixed and its working directory empty)."""
SECRET = "GITHUB_TOKEN"  # noqa: S105 (the secret's name, not its value)
PERMISSIONS = Permissions(
    intents=["task.create"],
    network=[HOST],
    secrets=[SECRET],
    optional_secrets=[NOTIFICATIONS_SECRET],
)

MAX_ITEMS = 200
MAX_FILTERS = 20
"""`exclude_repos` values."""
MAX_SEARCHES = 8
"""`orgs`, `users` and `repos` values together: one search each."""
LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}")
"""A user or organisation login; `_` for Enterprise Managed Users (`ada_acme`)."""
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}/[A-Za-z0-9._-]{1,100}")
"""An `owner/name` filter value."""
RETURNED_REPO_RE = re.compile(r"[^\s/?#%\\\x00-\x1f\x7f]{1,100}/[^\s/?#%\\\x00-\x1f\x7f]{1,100}")
"""A repository GitHub returned: kept unless it could not be one (whitespace, control
characters, a second `/`, or `?#%\\` that would change the link built from it); GitHub's own
naming rules are not applied, so a valid item is never dropped by a stricter pattern."""
NODE_ID_RE = re.compile(r"[A-Za-z0-9_=-]{1,100}")
"""All four are used with `fullmatch` only (`$` would accept a trailing line break)."""
TYPES = {"PullRequest": ("pr", "pull"), "Issue": ("issue", "issues")}

REASONS = frozenset(
    {
        "approval_requested",
        "assign",
        "author",
        "ci_activity",
        "comment",
        "invitation",
        "manual",
        "member_feature_requested",
        "mention",
        "review_requested",
        "security_advisory_credit",
        "security_alert",
        "state_change",
        "subscribed",
        "team_mention",
    }
)
"""GitHub's documented notification reasons; `participating=true` only returns some of them."""
DEFAULT_REASONS = ("review_requested", "assign", "mention", "team_mention")
"""Work waiting for you. `author`, `comment` and `state_change` (activity on your own threads)
are opt-in: they are noisy and other sources already cover the open items."""
VERBS = {
    "review_requested": "Review",
    "assign": "Work on",
    "mention": "Answer mention in",
    "team_mention": "Answer mention in",
}
"""Any other reason: `Follow up on`."""
SUBJECT_URL_RE = re.compile(
    r"https://api\.github\.com/repos/([^/?#%\\\s]{1,100})/([^/?#%\\\s]{1,100})"
    r"/(pulls|issues)/([0-9]{1,10})"
)
SINCE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")

ClientFactory = Callable[[str], Client]
NotificationsFactory = Callable[[str], NotificationsClient]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class Kind:
    name: str
    tool: str
    query: str
    """The search qualifiers, `@me` included."""
    covered: str
    """Qualifiers that leave out what a stronger kind returns (`skip_covered`)."""
    verb: str


REVIEW = Kind(
    "review_requested",
    "review_requests",
    "is:open is:pr archived:false review-requested:@me",
    "",
    "Review",
)
ASSIGNED = Kind(
    "assigned",
    "assigned",
    "is:open archived:false assignee:@me",
    "-review-requested:@me",
    "Work on",
)
MENTIONED = Kind(
    "mentioned",
    "mentions",
    "is:open archived:false mentions:@me",
    "-assignee:@me -review-requested:@me",
    "Answer mention in",
)


def default_client(token: str) -> Client:
    return Client(token, Transport())


def default_notifications_client(token: str) -> NotificationsClient:
    return NotificationsClient(token, Transport())


def search_queries(
    kind: Kind,
    *,
    orgs: Sequence[str],
    users: Sequence[str],
    repos: Sequence[str],
    exclude_repos: Sequence[str],
    skip_covered: bool,
    direct_only: bool = False,
) -> list[str]:
    """The search strings: the kind's qualifiers, then one `org:`, `user:` or `repo:` value
    per search (one search without any), then every `-repo:` exclusion.

    One value per search because GitHub ANDs qualifiers of different kinds (`org:a repo:b/c`
    matches nothing outside `a`): searches run separately and merged mean "any of them"."""
    query = kind.query
    if direct_only:
        query = query.replace("review-requested:@me", "user-review-requested:@me")
    head = [query, kind.covered] if skip_covered and kind.covered else [query]
    if len(exclude_repos) > MAX_FILTERS:
        raise PluginError(f"github: at most {MAX_FILTERS} exclude_repos values")
    excluded = [
        "-repo:" + _checked(value, REPO_RE, "exclude_repos")
        for value in dict.fromkeys(exclude_repos)
    ]
    scopes: list[str] = []
    for qualifier, values, pattern, name in (
        ("org:", orgs, LOGIN_RE, "org"),
        ("user:", users, LOGIN_RE, "user"),
        ("repo:", repos, REPO_RE, "repo"),
    ):
        scopes += [qualifier + _checked(value, pattern, name) for value in dict.fromkeys(values)]
    if len(scopes) > MAX_SEARCHES:
        raise PluginError(
            f"github: at most {MAX_SEARCHES} orgs, users and repos values in all (one search each)"
        )
    return [" ".join([*head, *scope, *excluded]) for scope in ([[s] for s in scopes] or [[]])]


def _checked(value: object, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise PluginError(f"github: invalid {name} filter")
    return value


@dataclass(frozen=True)
class Item:
    type: str
    url_part: str
    node_id: str | None
    repo: str
    number: int
    title: str
    milestone_due: date | None

    @property
    def fingerprint(self) -> str:
        if self.node_id is not None:
            return intents.fingerprint(NAME, self.type, self.node_id)
        return intents.fingerprint(NAME, self.type, self.repo, str(self.number))

    @property
    def url(self) -> str:
        """Built from GitHub's repository name and the integer number, host fixed to github.com,
        never from its `url` field."""
        return f"https://github.com/{self.repo}/{self.url_part}/{self.number}"


def parse(node: object) -> Item | None:
    """One search node, or `None` when it is not a well-formed issue or pull request."""
    if not isinstance(node, dict):
        return None
    typename = node.get("__typename")
    names = TYPES.get(typename) if isinstance(typename, str) else None
    repository = node.get("repository")
    repo = repository.get("nameWithOwner") if isinstance(repository, dict) else None
    number = node.get("number")
    if (
        names is None
        or not isinstance(repo, str)
        or not RETURNED_REPO_RE.fullmatch(repo)
        or type(number) is not int
        or not 0 < number < 2**31
    ):
        return None
    node_id = node.get("id")
    title = node.get("title")
    milestone = node.get("milestone")
    if any(segment in (".", "..") for segment in repo.split("/")):
        return None
    item = Item(
        type=names[0],
        url_part=names[1],
        node_id=node_id if isinstance(node_id, str) and NODE_ID_RE.fullmatch(node_id) else None,
        repo=repo,
        number=number,
        title=title if isinstance(title, str) else "",
        milestone_due=due.milestone_day(milestone.get("dueOn"))
        if isinstance(milestone, dict)
        else None,
    )
    # The link must be one the SDK (and the core) accepts: no format, bidi or surrogate
    # characters; an item that fails is skipped, never raised on.
    return item if is_task_link(item.url) else None


def parse_notification(thread: object) -> tuple[Item, str] | None:
    """A notification thread as `(item, reason)`, or `None` when its subject is not a pull
    request or an issue (Release, Discussion, CheckSuite, ...) or is malformed. The repository
    and number come from the subject URL, validated; the link is built from them."""
    if not isinstance(thread, dict):
        return None
    subject = thread.get("subject")
    reason = thread.get("reason")
    if not isinstance(subject, dict) or not isinstance(reason, str):
        return None
    typename = subject.get("type")
    names = TYPES.get(typename) if isinstance(typename, str) else None
    url = subject.get("url")
    found = SUBJECT_URL_RE.fullmatch(url) if isinstance(url, str) else None
    if names is None or found is None:
        return None
    owner, name, segment, number = found.groups()
    repo = f"{owner}/{name}"
    if (
        segment != ("pulls" if names[0] == "pr" else "issues")
        or not RETURNED_REPO_RE.fullmatch(repo)
        or any(part in (".", "..") for part in (owner, name))
        or not 0 < int(number) < 2**31
    ):
        return None
    title = subject.get("title")
    item = Item(
        type=names[0],
        url_part=names[1],
        node_id=None,
        repo=repo,
        number=int(number),
        title=title if isinstance(title, str) else "",
        milestone_due=None,
    )
    return (item, reason) if is_task_link(item.url) else None


def build(
    client: ClientFactory = default_client,
    *,
    notifications_client: NotificationsFactory = default_notifications_client,
    clock: Clock = due.utc_now,
    environ: Mapping[str, str] = os.environ,
) -> Plugin:
    """The plugin. `client`, `clock` and `environ` are replaceable for tests."""
    plugin = Plugin(
        NAME,
        version=VERSION,
        permissions=PERMISSIONS,
        description="GitHub review requests, assignments, mentions and notifications as tasks.",
    )

    def collect(
        kind: Kind,
        due_for: Callable[[Item, date], date | None],
        *,
        orgs: list[str] | None,
        users: list[str] | None,
        repos: list[str] | None,
        exclude_repos: list[str] | None,
        max_items: int,
        today: str | None,
        timezone: str | None,
        skip_covered: bool = False,
        direct_only: bool = False,
    ) -> Output:
        if type(max_items) is not int or not 1 <= max_items <= MAX_ITEMS:
            raise PluginError(f"github: max_items is 1 to {MAX_ITEMS}")
        queries = search_queries(
            kind,
            orgs=orgs or [],
            users=users or [],
            repos=repos or [],
            exclude_repos=exclude_repos or [],
            skip_covered=skip_covered,
            direct_only=direct_only,
        )
        try:
            day = due.run_day(today, timezone, environ, clock)
        except due.DueError as error:
            raise PluginError(str(error)) from None
        token = environ.get(SECRET)
        if not token:
            raise PluginError(f"github: {SECRET} is not set")
        try:
            found = client(token).search(queries, max_items=max_items)
        except GitHubError as error:
            raise PluginError(str(error)) from None
        items: dict[str, Item] = {}
        skipped = 0
        for node in found.nodes:
            item = parse(node)
            if item is None:
                skipped += 1
                continue
            items.setdefault(item.fingerprint, item)
        ordered = sorted(items.values(), key=lambda i: (i.repo.lower(), i.number, i.type))
        data: list[dict[str, Any]] = []
        tasks_: list[dict[str, Any]] = []
        for item in ordered:
            when = due_for(item, day)
            # `item.url` is built from GitHub's repository name and the integer number, host fixed
            # to github.com, never from its `url` field.
            task = intents.task_create(
                f"{kind.verb} {item.repo}#{item.number}: {item.title}",
                key=item.fingerprint,
                due=when,
                link=item.url,
            )
            tasks_.append(task)
            data.append(
                {
                    "fingerprint": item.fingerprint,
                    "type": item.type,
                    "repo": item.repo,
                    "number": item.number,
                    "title": tasks.plain_text(tasks.escape_title(item.title)),
                    "url": item.url,
                    "due": None if when is None else when.isoformat(),
                }
            )
        return Output(
            {
                "kind": kind.name,
                "day": day.isoformat(),
                "items": data,
                "truncated": found.truncated,
                "skipped": skipped,
                "partial": found.errors > 0,
                "errors": found.errors,
            },
            intents=tasks_,
        )

    @plugin.source(f"{NAME}.{REVIEW.tool}")
    def review_requests(
        call: Call,
        due_in_business_days: int | None = 1,
        orgs: list[str] | None = None,
        users: list[str] | None = None,
        repos: list[str] | None = None,
        exclude_repos: list[str] | None = None,
        max_items: int = 50,
        today: str | None = None,
        timezone: str | None = None,
        direct_only: bool = False,
    ) -> Output:
        """Open pull requests whose review is requested from you or a team you are in
        (`direct_only`: from you only). Due `due_in_business_days` working days after the
        run's day (0: that day, even on a weekend; null: none)."""
        _offset(due_in_business_days)
        return collect(
            REVIEW,
            lambda _item, day: due.add_business_days(day, due_in_business_days),
            orgs=orgs,
            users=users,
            repos=repos,
            exclude_repos=exclude_repos,
            max_items=max_items,
            today=today,
            timezone=timezone,
            direct_only=direct_only,
        )

    @plugin.source(f"{NAME}.{ASSIGNED.tool}")
    def assigned(
        call: Call,
        milestone_due: bool = True,
        due_in_days: int | None = None,
        skip_covered: bool = False,
        orgs: list[str] | None = None,
        users: list[str] | None = None,
        repos: list[str] | None = None,
        exclude_repos: list[str] | None = None,
        max_items: int = 50,
        today: str | None = None,
        timezone: str | None = None,
    ) -> Output:
        """Open issues and pull requests assigned to you. Due on the milestone's due date
        (`milestone_due`), else `due_in_days` days after the run's day (null: no due date)."""
        _offset(due_in_days)

        def due_for(item: Item, day: date) -> date | None:
            if milestone_due and item.milestone_due is not None:
                return item.milestone_due
            return due.add_days(day, due_in_days)

        return collect(
            ASSIGNED,
            due_for,
            orgs=orgs,
            users=users,
            repos=repos,
            exclude_repos=exclude_repos,
            max_items=max_items,
            today=today,
            timezone=timezone,
            skip_covered=skip_covered,
        )

    @plugin.source(f"{NAME}.{MENTIONED.tool}")
    def mentions(
        call: Call,
        due_in_days: int | None = 2,
        skip_covered: bool = False,
        orgs: list[str] | None = None,
        users: list[str] | None = None,
        repos: list[str] | None = None,
        exclude_repos: list[str] | None = None,
        max_items: int = 50,
        today: str | None = None,
        timezone: str | None = None,
    ) -> Output:
        """Open issues and pull requests that mention you. Due `due_in_days` days after the
        run's day (null: no due date)."""
        _offset(due_in_days)
        return collect(
            MENTIONED,
            lambda _item, day: due.add_days(day, due_in_days),
            orgs=orgs,
            users=users,
            repos=repos,
            exclude_repos=exclude_repos,
            max_items=max_items,
            today=today,
            timezone=timezone,
            skip_covered=skip_covered,
        )

    @plugin.source(f"{NAME}.notifications")
    def notifications(
        call: Call,
        reasons: list[str] | None = None,
        due_in_days: int | None = 2,
        since: str | None = None,
        orgs: list[str] | None = None,
        users: list[str] | None = None,
        repos: list[str] | None = None,
        exclude_repos: list[str] | None = None,
        max_items: int = 50,
        today: str | None = None,
        timezone: str | None = None,
    ) -> Output:
        """Unread notifications you participate in, for pull requests and issues,
        limited to `reasons` (default: review_requested, assign, mention, team_mention). Read
        only: nothing is marked read. Due `due_in_days` days after the run's day."""
        _offset(due_in_days)
        if type(max_items) is not int or not 1 <= max_items <= MAX_ITEMS:
            raise PluginError(f"github: max_items is 1 to {MAX_ITEMS}")
        wanted: list[object] = list(DEFAULT_REASONS if reasons is None else dict.fromkeys(reasons))
        if not wanted or not all(isinstance(r, str) and r in REASONS for r in wanted):
            raise PluginError("github: invalid reasons filter")
        raw_since: object = since
        if raw_since is not None and not (
            isinstance(raw_since, str) and SINCE_RE.fullmatch(raw_since)
        ):
            raise PluginError("github: since must be YYYY-MM-DDTHH:MM:SSZ")
        # the same validation as the searches (the strings are not used): here the values
        # filter the answer
        search_queries(
            ASSIGNED,
            orgs=orgs or [],
            users=users or [],
            repos=repos or [],
            exclude_repos=exclude_repos or [],
            skip_covered=False,
        )
        owners = {v.lower() for v in [*(orgs or []), *(users or [])]}
        only_repos = {v.lower() for v in repos or []}
        excluded = {v.lower() for v in exclude_repos or []}
        try:
            day = due.run_day(today, timezone, environ, clock)
        except due.DueError as error:
            raise PluginError(str(error)) from None
        token = environ.get(NOTIFICATIONS_SECRET)
        if not token:
            raise PluginError(f"secret_missing: {NOTIFICATIONS_SECRET} is not set")
        try:
            found = notifications_client(token).participating(max_items=max_items, since=since)
        except GitHubError as error:
            raise PluginError(str(error)) from None
        items: dict[str, tuple[Item, str]] = {}
        skipped = filtered = 0
        for thread in found.threads:
            parsed = parse_notification(thread)
            if parsed is None:
                skipped += 1
                continue
            item, reason = parsed
            repo = item.repo.lower()
            outside = bool(owners or only_repos) and not (
                repo.split("/")[0] in owners or repo in only_repos
            )
            if reason not in wanted or repo in excluded or outside:
                filtered += 1
                continue
            items.setdefault(item.fingerprint, (item, reason))
        ordered = sorted(items.values(), key=lambda p: (p[0].repo.lower(), p[0].number, p[0].type))
        data: list[dict[str, Any]] = []
        tasks_: list[dict[str, Any]] = []
        when = due.add_days(day, due_in_days)
        for item, reason in ordered:
            verb = VERBS.get(reason, "Follow up on")
            tasks_.append(
                intents.task_create(
                    f"{verb} {item.repo}#{item.number}: {item.title}",
                    key=item.fingerprint,
                    due=when,
                    link=item.url,
                )
            )
            data.append(
                {
                    "fingerprint": item.fingerprint,
                    "type": item.type,
                    "repo": item.repo,
                    "number": item.number,
                    "reason": reason,
                    "title": tasks.plain_text(tasks.escape_title(item.title)),
                    "url": item.url,
                    "due": None if when is None else when.isoformat(),
                }
            )
        return Output(
            {
                "kind": "notifications",
                "day": day.isoformat(),
                "items": data,
                "truncated": found.truncated,
                "skipped": skipped,
                "filtered": filtered,
                "poll_interval": found.poll_interval,
                "rate_limit_remaining": found.rate_limit_remaining,
            },
            intents=tasks_,
        )

    return plugin


def _offset(days: int | None) -> None:
    try:
        due.check_offset(days)
    except due.DueError as error:
        raise PluginError(str(error)) from None


def manifest() -> dict[str, Any]:
    """The `plugin.yaml` document of this plugin (`python -m horae_github --manifest`)."""
    return build().manifest(
        command=COMMAND,
        horae=">=0,<0.2",
        license="Apache-2.0",
        homepage="https://github.com/horae-dev/plugin-github",
    )
