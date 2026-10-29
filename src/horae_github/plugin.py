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
waiting for your review is the same task wherever it comes from. On top of that, with
`skip_covered` (the default) `github.assigned` leaves out what `github.review_requests`
returns and `github.mentions` leaves out both (search qualifiers `-review-requested:@me`,
`-assignee:@me`), so one run proposes each item once, with the strongest kind's title and due
date: review request, then assignment, then mention.

The token is the declared secret `GITHUB_TOKEN`, read at call time; it never appears in data,
intents, errors or logs.
"""

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from horae_sdk import Call, Output, Permissions, Plugin, PluginError, intents, tasks

from horae_github import due
from horae_github.client import Client, GitHubError
from horae_github.http import HOST, Transport

NAME = "github"
"""The plugin's name, also the namespace of its refs (`github.review_requests`) and
fingerprints (`github:pr:<node id>`)."""
VERSION = "0.1.0"
COMMAND = ["python3", "-m", "horae_github"]
"""`runtime.command` of `plugin.yaml`. When installing, pin an absolute interpreter that has
this package (the host's `PATH` is fixed and its working directory empty)."""
SECRET = "GITHUB_TOKEN"  # noqa: S105 (the secret's name, not its value)
PERMISSIONS = Permissions(intents=["task.create"], network=[HOST], secrets=[SECRET])

MAX_ITEMS = 200
MAX_FILTERS = 20
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_=-]{1,100}$")
TYPES = {"PullRequest": ("pr", "pull"), "Issue": ("issue", "issues")}

ClientFactory = Callable[[str], Client]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class Kind:
    name: str
    tool: str
    query: str
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


def search_query(
    kind: Kind,
    *,
    orgs: Sequence[str],
    users: Sequence[str],
    repos: Sequence[str],
    exclude_repos: Sequence[str],
    skip_covered: bool,
) -> str:
    """The search string: the kind's qualifiers, then the filters. Several `org:`, `user:` or
    `repo:` filters match any of them (GitHub ORs a repeated qualifier)."""
    parts = [kind.query]
    if skip_covered and kind.covered:
        parts.append(kind.covered)
    for qualifier, values, pattern in (
        ("org:", orgs, LOGIN_RE),
        ("user:", users, LOGIN_RE),
        ("repo:", repos, REPO_RE),
        ("-repo:", exclude_repos, REPO_RE),
    ):
        if len(values) > MAX_FILTERS:
            raise PluginError(f"github: at most {MAX_FILTERS} values per filter")
        for value in dict.fromkeys(values):
            if not pattern.match(value):
                raise PluginError(f"github: invalid {qualifier.strip('-:')} filter")
            parts.append(qualifier + value)
    return " ".join(parts)


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
        """Built from the validated repository and number, never taken from the answer."""
        return f"https://github.com/{self.repo}/{self.url_part}/{self.number}"


def parse(node: Mapping[str, Any]) -> Item | None:
    """One search node, or `None` when it is not a well-formed issue or pull request."""
    names = TYPES.get(node.get("__typename"))  # type: ignore[arg-type]
    repository = node.get("repository")
    repo = repository.get("nameWithOwner") if isinstance(repository, dict) else None
    number = node.get("number")
    if (
        names is None
        or not isinstance(repo, str)
        or not REPO_RE.match(repo)
        or type(number) is not int
        or not 0 < number < 2**31
    ):
        return None
    node_id = node.get("id")
    title = node.get("title")
    milestone = node.get("milestone")
    return Item(
        type=names[0],
        url_part=names[1],
        node_id=node_id if isinstance(node_id, str) and NODE_ID_RE.match(node_id) else None,
        repo=repo,
        number=number,
        title=title if isinstance(title, str) else "",
        milestone_due=due.milestone_day(milestone.get("dueOn"))
        if isinstance(milestone, dict)
        else None,
    )


def build(
    client: ClientFactory = default_client,
    *,
    clock: Clock = due.utc_now,
    environ: Mapping[str, str] = os.environ,
) -> Plugin:
    """The plugin. `client`, `clock` and `environ` are replaceable for tests."""
    plugin = Plugin(
        NAME,
        version=VERSION,
        permissions=PERMISSIONS,
        description="GitHub review requests, assignments and mentions as tasks.",
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
    ) -> Output:
        if type(max_items) is not int or not 1 <= max_items <= MAX_ITEMS:
            raise PluginError(f"github: max_items is 1 to {MAX_ITEMS}")
        query = search_query(
            kind,
            orgs=orgs or [],
            users=users or [],
            repos=repos or [],
            exclude_repos=exclude_repos or [],
            skip_covered=skip_covered,
        )
        try:
            day = due.run_day(today, timezone, environ, clock)
        except due.DueError as error:
            raise PluginError(str(error)) from None
        token = environ.get(SECRET)
        if not token:
            raise PluginError(f"github: {SECRET} is not set")
        try:
            found = client(token).search(query, max_items=max_items)
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
            task = intents.task_create(
                f"{kind.verb} {item.repo}#{item.number}: {item.title}",
                key=item.fingerprint,
                due=when,
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
    ) -> Output:
        """Open pull requests whose review is requested from you. Due
        `due_in_business_days` working days after the run's day (0: that day; null: none)."""
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
        )

    @plugin.source(f"{NAME}.{ASSIGNED.tool}")
    def assigned(
        call: Call,
        milestone_due: bool = True,
        due_in_days: int | None = None,
        skip_covered: bool = True,
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
        skip_covered: bool = True,
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
        horae=">=0.1,<0.2",
        license="Apache-2.0",
        homepage="https://github.com/horae-dev/plugin-github",
    )
