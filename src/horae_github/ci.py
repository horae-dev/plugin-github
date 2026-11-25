# SPDX-License-Identifier: Apache-2.0
"""`github.ci_failures`: which of your repositories has a failing CI, without opening each one.

Two reads, all GraphQL, within one call deadline (`Client.deadline`):

- **Default branches.** For each owner (`orgs`, `users`), the owner's own repositories, most
  recently pushed first (`repositoryOwner.repositories`, `ownerAffiliations: [OWNER]`,
  `isArchived: false`, `isFork: false` unless `include_forks`), 50 per page, with the head
  commit of the default branch and its `statusCheckRollup`. Paging **stops** at the first
  repository pushed before the window (`pushed_within_days` before the run's day): the order
  is by push date, so every later one is older too. At most `MAX_OWNER_PAGES` pages per owner.
  Each `repos` value is one `repository(owner, name)` read.
- **Your pull requests** (`include_my_prs`): `is:pr is:open author:@me archived:false`, one
  search per owner or repository filter (the same `search_queries` as the other sources),
  with each pull request's head commit and its rollup.

A **failure** is a head commit whose rollup state is `FAILURE` or `ERROR`. `PENDING` (still
running, or expected) and `SUCCESS` are not; a commit without checks has no rollup. The
failing checks are listed by name: a check run's workflow and job (`Nightly / build`), or a
commit status's context; conclusions `FAILURE`, `TIMED_OUT`, `STARTUP_FAILURE`,
`ACTION_REQUIRED` and `CANCELLED`, states `FAILURE` and `ERROR`.

**Identity.** The fingerprint is the repository, the ref (the branch, or the pull request
number) and the head commit's full SHA. A new commit that still fails is a **new** failure;
the same failing commit is the same one in every run, so a `notify.push` keyed on it (`notify`)
is sent once by the core (schemas decision 57, amended: a keyed push is never resent).

**Untrusted.** Repository, branch, check and workflow names come from GitHub and anyone with
write access chooses them (a workflow's `name:`, a branch name, a commit status context posted
by any integration). They reach data and push text only through `tasks.escape_title` then
`tasks.plain_text` (control, bidi and zero-width characters removed, bounded), and a note
through `obsidian.render_note`, which escapes them again. Links are never taken from GitHub
as they are: a workflow run URL is used only when it is exactly
`https://github.com/<this repository>/actions/runs/<digits>`; otherwise the link is built
from the repository name and the validated SHA (`/commit/<sha>`) or pull request number
(`/pull/<n>`). A commit status's `targetUrl` (any host) is never used.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from horae_sdk import PluginError, intents, tasks
from horae_sdk.tasks import is_task_link

from horae_github.client import (
    MIN_PAGE_S,
    RETURNED_REPO_RE,
    Client,
    GitHubError,
    _OutOfTime,
)

NAME = "github"
OWNER_PAGE_SIZE = 50
MAX_OWNER_PAGES = 4
"""200 repositories per owner, most recently pushed first."""
MAX_PR_RESULTS = 100
"""Open pull requests read per search (two pages)."""
MAX_ITEMS = 100
MAX_WINDOW_DAYS = 365
MAX_CHECKS_SHOWN = 10
MAX_CHECK_NAME = 100
MAX_BRANCH = 100
MAX_REPO = 201
FAILED_ROLLUP = frozenset({"FAILURE", "ERROR"})
FAILED_CONCLUSIONS = frozenset(
    {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "CANCELLED"}
)
FAILED_STATES = frozenset({"FAILURE", "ERROR"})
SHA_RE = re.compile(r"[0-9a-f]{40}")
TIMESTAMP_RE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
RUN_URL_RE = re.compile(
    r"https://github\.com/([^/?#%\\\s]{1,100}/[^/?#%\\\s]{1,100})/actions/runs/([0-9]{1,20})"
)
"""A workflow run page; group 1 must equal the item's repository exactly."""

_ROLLUP = """\
statusCheckRollup {
  state
  contexts(first: 50) {
    totalCount
    nodes {
      __typename
      ... on CheckRun { name conclusion checkSuite { workflowRun { url workflow { name } } } }
      ... on StatusContext { context state }
    }
  }
}"""
_REPOSITORY = f"""\
nameWithOwner isArchived isFork pushedAt
defaultBranchRef {{ name target {{ ... on Commit {{ oid committedDate {_ROLLUP} }} }} }}"""
OWNER_QUERY = f"""\
query($login: String!, $first: Int!, $after: String, $isFork: Boolean) {{
  repositoryOwner(login: $login) {{
    repositories(first: $first, after: $after, ownerAffiliations: [OWNER], isArchived: false,
                 isFork: $isFork, orderBy: {{field: PUSHED_AT, direction: DESC}}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{ {_REPOSITORY} }}
    }}
  }}
}}"""
REPOSITORY_QUERY = f"""\
query($owner: String!, $name: String!) {{
  repository(owner: $owner, name: $name) {{ {_REPOSITORY} }}
}}"""
PR_QUERY = f"""\
query($q: String!, $first: Int!, $after: String) {{
  search(type: ISSUE, query: $q, first: $first, after: $after) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      __typename
      ... on PullRequest {{
        id number headRefName
        repository {{ nameWithOwner isArchived isFork }}
        commits(last: 1) {{ nodes {{ commit {{ oid committedDate {_ROLLUP} }} }} }}
      }}
    }}
  }}
}}"""
PR_SEARCH = "is:pr is:open author:@me archived:false"


def text(value: str, limit: int) -> str:
    """Untrusted GitHub text for data and push bodies: inert, one line, bounded."""
    return tasks.plain_text(tasks.escape_title(value, max_length=limit))


@dataclass(frozen=True)
class Failure:
    repo: str
    branch: str
    """Sanitised branch name (display only)."""
    pr: int | None
    sha: str
    since: str
    """The head commit's date, `YYYY-MM-DD` (UTC)."""
    checks: tuple[str, ...]
    """Sanitised failing check names, sorted, at most `MAX_CHECKS_SHOWN`."""
    more_checks: int
    """Failing checks past `MAX_CHECKS_SHOWN`."""
    unread_checks: int
    """Checks past the first 50 the query reads (failing or not: unknown)."""
    run_url: str | None

    @property
    def ref(self) -> str:
        return f"pr-{self.pr}" if self.pr is not None else f"branch-{self.branch}"

    @property
    def fingerprint(self) -> str:
        return intents.fingerprint(NAME, "ci", self.repo, self.ref, self.sha)

    @property
    def url(self) -> str:
        if self.run_url is not None:
            return self.run_url
        if self.pr is not None:
            return f"https://github.com/{self.repo}/pull/{self.pr}"
        return f"https://github.com/{self.repo}/commit/{self.sha}"

    @property
    def what(self) -> str:
        names = ", ".join(self.checks) or "checks"
        if self.more_checks:
            names += f" and {self.more_checks} more"
        if self.unread_checks:
            names += f" ({self.unread_checks} checks not read)"
        return names

    @property
    def title(self) -> str:
        """For `obsidian.render_note`, which prefixes the repository and the number."""
        where = self.branch if self.pr is None else f"pull request on {self.branch}"
        return f"{where}: {self.what} failing since {self.since} ({self.sha[:7]})"

    def data(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "repo": self.repo,
            "branch": self.branch,
            "number": self.pr,
            "sha": self.sha[:7],
            "checks": list(self.checks),
            "more_checks": self.more_checks,
            "unread_checks": self.unread_checks,
            "since": self.since,
            "url": self.url,
            "title": self.title,
        }

    def push(self) -> dict[str, Any]:
        where = self.branch if self.pr is None else f"#{self.pr} ({self.branch})"
        # at most 12 + 201 + 15 + 100 characters: under the SDK's 500
        title = f"CI failing: {text(self.repo, MAX_REPO)} {where}"
        return intents.notify_push(
            f"{self.what} failed on {self.sha[:7]} ({self.since}).",
            title=title,
            link=self.url,
            key=self.fingerprint,
        )


@dataclass
class Walk:
    failures: dict[str, Failure] = field(default_factory=dict)
    repos: int = 0
    """Repositories (and pull requests) whose head commit was looked at."""
    filtered: int = 0
    """Left out on purpose: stale, fork, archived, excluded, another owner."""
    skipped: int = 0
    """Malformed answers."""
    errors: int = 0
    """Owners or repositories GitHub hid (`FORBIDDEN`, `NOT_FOUND`, SAML) or did not find."""
    truncated: bool = False
    pages: int = 0
    missing: int = 0
    """`orgs`, `users` and `repos` values GitHub returned nothing for."""


def day_of(value: object) -> date | None:
    found = TIMESTAMP_RE.fullmatch(value) if isinstance(value, str) else None
    if found is None:
        return None
    try:
        return date.fromisoformat(found.group(1))
    except ValueError:
        return None


def run_link(url: str, repo: str) -> bool:
    """`url` is a workflow run page of `repo` itself, nothing more (no query, fragment, job
    suffix, other host or other repository) and a valid task link."""
    found = RUN_URL_RE.fullmatch(url)
    return found is not None and found.group(1) == repo and is_task_link(url)


def failing_checks(rollup: dict[str, Any], repo: str) -> tuple[list[str], int, str | None]:
    """`(names, unread, run url)`: the failing contexts' names (sanitised, unique, sorted),
    how many contexts past the first 50 were not read (failing or not), and the first
    (sorted) valid workflow run URL of this repository."""
    contexts = rollup.get("contexts")
    nodes = contexts.get("nodes") if isinstance(contexts, dict) else None
    names: set[str] = set()
    run_urls: list[str] = []
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        name: object = None
        if node.get("__typename") == "CheckRun" and node.get("conclusion") in FAILED_CONCLUSIONS:
            name = node.get("name")
            suite = node.get("checkSuite")
            run = suite.get("workflowRun") if isinstance(suite, dict) else None
            if isinstance(run, dict):
                workflow = run.get("workflow")
                flow = workflow.get("name") if isinstance(workflow, dict) else None
                if isinstance(flow, str) and isinstance(name, str) and flow != name:
                    name = f"{flow} / {name}"
                url = run.get("url")
                if isinstance(url, str) and run_link(url, repo):
                    run_urls.append(url)
        elif node.get("__typename") == "StatusContext" and node.get("state") in FAILED_STATES:
            name = node.get("context")
        if isinstance(name, str):
            shown = text(name, MAX_CHECK_NAME)
            if shown:
                names.add(shown)
    total = contexts.get("totalCount") if isinstance(contexts, dict) else None
    listed = len(nodes) if isinstance(nodes, list) else 0
    unlisted = total - listed if type(total) is int and total > listed else 0
    return sorted(names), unlisted, (sorted(run_urls)[0] if run_urls else None)


def failure(repo: str, branch: object, pr: int | None, commit: object) -> Failure | str:
    """The failure of this head commit, or why there is none: `passing`, `pending` (also no
    checks at all) or `malformed`."""
    if not isinstance(commit, dict) or not isinstance(branch, str) or not branch:
        return "malformed"
    sha = commit.get("oid")
    since = day_of(commit.get("committedDate"))
    rollup = commit.get("statusCheckRollup")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha) or since is None:
        return "malformed"
    if rollup is None:
        return "pending"
    if not isinstance(rollup, dict):
        return "malformed"
    state = rollup.get("state")
    if state not in FAILED_ROLLUP:
        return "passing" if state == "SUCCESS" else "pending"
    names, unlisted, run_url = failing_checks(rollup, repo)
    shown_branch = text(branch, MAX_BRANCH) or "?"
    found = Failure(
        repo=repo,
        branch=shown_branch,
        pr=pr,
        sha=sha,
        since=since.isoformat(),
        checks=tuple(names[:MAX_CHECKS_SHOWN]),
        more_checks=max(0, len(names) - MAX_CHECKS_SHOWN),
        unread_checks=unlisted,
        run_url=run_url,
    )
    # the link must be one the SDK (and the core) accepts: a repository name with format,
    # bidi or surrogate characters is skipped, never raised on
    return found if is_task_link(found.url) else "malformed"


def _repo_name(value: object) -> str | None:
    if not isinstance(value, str) or not RETURNED_REPO_RE.fullmatch(value):
        return None
    if any(part in (".", "..") for part in value.split("/")):
        return None
    return value


@dataclass(frozen=True)
class Scope:
    owners: Sequence[str]
    repos: Sequence[str]
    excluded: frozenset[str]
    """Lower-case `owner/name`."""
    cutoff: date
    include_forks: bool


def _keep(walk: Walk, found: Failure | str) -> None:
    if isinstance(found, Failure):
        walk.failures.setdefault(found.fingerprint, found)
    elif found == "malformed":
        walk.skipped += 1


def _repository(walk: Walk, node: object, scope: Scope, owner: str | None) -> bool:
    """Look at one repository node; `False` when it was pushed before the window (the owner's
    walk stops there)."""
    if not isinstance(node, dict):
        walk.skipped += 1
        return True
    repo = _repo_name(node.get("nameWithOwner"))
    pushed = day_of(node.get("pushedAt"))
    if repo is None or pushed is None:
        walk.skipped += 1
        return True
    if pushed < scope.cutoff:
        walk.filtered += 1
        return False
    if (
        node.get("isArchived") is not False
        or (node.get("isFork") is not False and not scope.include_forks)
        or repo.lower() in scope.excluded
        or (owner is not None and repo.split("/")[0].lower() != owner.lower())
    ):
        walk.filtered += 1
        return True
    head = node.get("defaultBranchRef")
    if head is None:  # an empty repository
        walk.filtered += 1
        return True
    if not isinstance(head, dict):
        walk.skipped += 1
        return True
    walk.repos += 1
    _keep(walk, failure(repo, head.get("name"), None, head.get("target")))
    return True


def _out_of_time(client: Client, deadline: float, walk: Walk) -> bool:
    return walk.pages > 0 and client.time_left(deadline) < MIN_PAGE_S


def default_branches(client: Client, walk: Walk, scope: Scope, deadline: float) -> None:
    """Walk each owner's repositories, then each `repos` value, until the window or time ends."""
    for owner in scope.owners:
        after: str | None = None
        for _page in range(MAX_OWNER_PAGES):
            if _out_of_time(client, deadline, walk):
                walk.truncated = True
                return
            variables = {
                "login": owner,
                "first": OWNER_PAGE_SIZE,
                "after": after,
                "isFork": None if scope.include_forks else False,
            }
            found, hidden = _read(client, walk, OWNER_QUERY, variables, "repositoryOwner", deadline)
            if found is None:
                walk.missing += 1
                break
            walk.errors += hidden
            repositories = found.get("repositories")
            info = repositories.get("pageInfo") if isinstance(repositories, dict) else None
            nodes = repositories.get("nodes") if isinstance(repositories, dict) else None
            if not isinstance(nodes, list) or not isinstance(info, dict):
                raise GitHubError("github: malformed response")
            inside = all([_repository(walk, node, scope, owner) for node in nodes])
            cursor = info.get("endCursor")
            if not inside or info.get("hasNextPage") is not True or not nodes:
                break
            if not isinstance(cursor, str):
                walk.truncated = True
                break
            after = cursor
        else:
            walk.truncated = True  # more repositories inside the window than the pages allow
    for value in scope.repos:
        if _out_of_time(client, deadline, walk):
            walk.truncated = True
            return
        owner, name = value.split("/", 1)
        variables = {"owner": owner, "name": name}
        found, hidden = _read(client, walk, REPOSITORY_QUERY, variables, "repository", deadline)
        if found is None:
            walk.missing += 1
            continue
        walk.errors += hidden
        _repository(walk, found, scope, None)


def _read(
    client: Client,
    walk: Walk,
    query: str,
    variables: dict[str, Any],
    root: str,
    deadline: float,
) -> tuple[dict[str, Any] | None, int]:
    try:
        found, hidden = client.graphql(query, variables, root, deadline, allow_missing=True)
    except _OutOfTime:
        if walk.pages:
            walk.truncated = True
            raise _Stop() from None
        raise GitHubError("github: request timed out") from None
    walk.pages += 1
    if found is not None and not isinstance(found, dict):
        raise GitHubError("github: malformed response")
    return found, hidden


class _Stop(Exception):
    """Out of time after some data: return what was read, `truncated`."""


def my_pull_requests(
    client: Client, walk: Walk, scope: Scope, queries: Sequence[str], deadline: float
) -> None:
    if client.time_left(deadline) < MIN_PAGE_S and walk.pages:
        walk.truncated = True
        return
    found = client.search(
        queries,
        max_items=MAX_PR_RESULTS,
        query=PR_QUERY,
        deadline=deadline,
        started=walk.pages > 0,
    )
    walk.pages += 1
    walk.truncated = walk.truncated or found.truncated
    walk.errors += found.errors
    for node in found.nodes:
        if not isinstance(node, dict) or node.get("__typename") != "PullRequest":
            walk.skipped += 1
            continue
        repository = node.get("repository")
        repo = _repo_name(repository.get("nameWithOwner")) if isinstance(repository, dict) else None
        number = node.get("number")
        commits = node.get("commits")
        heads = commits.get("nodes") if isinstance(commits, dict) else None
        head = heads[-1] if isinstance(heads, list) and heads else None
        commit = head.get("commit") if isinstance(head, dict) else None
        if (
            repo is None
            or not isinstance(repository, dict)
            or type(number) is not int
            or not 0 < number < 2**31
            or not isinstance(commit, dict)
        ):
            walk.skipped += 1
            continue
        committed = day_of(commit.get("committedDate"))
        if (
            repository.get("isArchived") is not False
            or (repository.get("isFork") is not False and not scope.include_forks)
            or repo.lower() in scope.excluded
            or (committed is not None and committed < scope.cutoff)
        ):
            walk.filtered += 1
            continue
        walk.repos += 1
        _keep(walk, failure(repo, node.get("headRefName"), number, commit))


def collect(
    client: Client,
    *,
    owners: Sequence[str],
    repos: Sequence[str],
    excluded: frozenset[str],
    day: date,
    pushed_within_days: int,
    include_forks: bool,
    pr_queries: Sequence[str] | None,
) -> Walk:
    """Every failure in scope, within one deadline. Raises `GitHubError` when nothing could be
    read at all."""
    scope = Scope(
        owners=owners,
        repos=repos,
        excluded=excluded,
        cutoff=day - timedelta(days=pushed_within_days),
        include_forks=include_forks,
    )
    walk = Walk()
    deadline = client.deadline()
    try:
        default_branches(client, walk, scope, deadline)
        if pr_queries is not None:
            # also after the page cap (`truncated`, time left): your pull requests are a
            # separate read; `my_pull_requests` itself stops when time is short
            my_pull_requests(client, walk, scope, pr_queries, deadline)
    except _Stop:
        walk.truncated = True
    if walk.missing and walk.missing >= len(owners) + len(repos):
        raise GitHubError("github: no owner or repository could be read")
    return walk


def check_window(days: object) -> int:
    if type(days) is not int or not 0 <= days <= MAX_WINDOW_DAYS:
        raise PluginError(f"github: pushed_within_days is 0 to {MAX_WINDOW_DAYS}")
    return days
