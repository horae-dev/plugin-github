<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-github

Horae plugin: GitHub review requests, assigned issues, mentions and participating notifications as tasks, which of your repositories has a failing CI, and what is new for you since your last action.

> 🚧 **Early.** Review requests, assignments, mentions, participating notifications, CI failures and updates work.

## What it does

Six sources for a pipeline's `collect` step. Three search GitHub (the fourth, notifications, has [its own section](#participating-notifications)) for open items and returns them as data, and proposes one `task.create` intent per item. The fifth, `github.ci_failures`, proposes no task: it lists the repositories and pull requests whose CI fails, and can propose one push per new failure ([CI failures](#ci-failures)). The sixth, `github.updates`, is a to-do of issues and pull requests where someone else did something after your last action ([Updates](#updates-new-for-you)); no task either. The Horae core validates and applies the intents; the plugin never writes anything.

| Source | Finds | Task title | Default due date |
|---|---|---|---|
| `github.review_requests` | open pull requests whose review is requested from you or from a team you are in (`review-requested:@me`; `direct_only: true` for you only, `user-review-requested:@me`) | `Review owner/repo#42: <title>` | the run's day + 1 business day |
| `github.assigned` | open issues and pull requests assigned to you | `Work on owner/repo#42: <title>` | the milestone's due date, else none |
| `github.mentions` | open issues and pull requests that mention you | `Answer mention in owner/repo#42: <title>` | the run's day + 2 days |
| `github.notifications` | unread notifications you participate in, for pull requests and issues | `Review` / `Work on` / `Answer mention in` / `Follow up on owner/repo#42: <title>` by reason | the run's day + 2 days |
| `github.ci_failures` | default branches and your own open pull requests whose head commit fails its checks | no task (data, and optionally `notify.push`) | none |
| `github.updates` | issues and pull requests with activity by someone else since your last action on them | no task (data, and optionally `notify.push`) | none |

```yaml
# pipelines/daily-brief.yaml
steps:
  - collect:
      - github.review_requests: { today: "{{date}}" }
      - github.assigned: { today: "{{date}}", orgs: [acme-org] }
      - github.mentions: { today: "{{date}}", due_in_days: 3 }
```

### Routing the tasks: `link_prefix`

The core routes a task that has a link only to a `mode: tasks` entry whose trusted `link_prefix` covers it; without one the step fails with `intent_unrouted`. Set it to the GitHub owners of the items you collect, copied exactly as they appear in github.com URLs (canonical case, for example `Acme-Org`), not the `orgs:` value:

```yaml
steps:
  - collect:
      - github.review_requests: { today: "{{date}}", orgs: [acme-org] }
  - act:
      - tasks:
          mode: tasks
          link_prefix: https://github.com/Acme-Org/
```

The prefix needs at least one path segment (`https://github.com/Acme-Org/`, not `https://github.com/`). The host is compared case-insensitively, `:443` and a trailing dot are ignored, and the path is case-sensitive: `https://github.com/acme-org/` does not cover `https://github.com/Acme-Org/api/pull/1`.

- One entry (and prefix) per owner; the prefixes must not overlap, or the step fails with `intent_ambiguous`.
- An item no entry covers fails the whole step with `intent_unrouted`. Set `orgs` / `users` to match the prefixes: `review_requests` and `mentions` default to every owner, personal repositories included, so without a filter they need a prefix for each owner you appear in.

### Parameters

The three search sources take:

| Parameter | Default | Meaning |
|---|---|---|
| `orgs` | none | only repositories of these organisations (`org:` qualifier) |
| `users` | none | only repositories of these users (`user:`) |
| `repos` | none | only these repositories, `owner/name` (`repo:`) |
| `exclude_repos` | none | never these repositories (`-repo:`), at most 20 |
| `max_items` | 50 | at most this many items, 1 to 200 (one request per 50) |
| `today` | the clock | the run's day, `YYYY-MM-DD`. Pass `"{{date}}"`: the due dates then depend only on the run, so a replay gives the same tasks |
| `timezone` | the instance's | an IANA time zone, used only when `today` is not given |

**Filters are "any of".** GitHub ANDs qualifiers of different kinds (`org:acme repo:other/api` matches nothing), so the plugin runs **one search per `orgs`, `users` or `repos` value** and merges the results, each item once (by node id): an item is returned when it matches any value. `exclude_repos` is added to every search. At most 8 `orgs`, `users` and `repos` values in all (16 searches at most, see below); more is refused with `github: at most 8 orgs, users and repos values in all (one search each)`. Without any of them there is one search. **Issues and pull requests are searched apart:** GitHub's search returns nothing, silently, for a fine-grained token when a query has neither `is:issue` nor `is:pr` (REST answers 422), so `github.assigned`, `github.mentions` and `github.updates` run each search twice, once with `is:issue` and once with `is:pr`, merged by node id; `github.review_requests` and the pull request search of `github.ci_failures` are pull-request-only (`is:pr`) and stay one search per value. `max_items` and `truncated` apply to the merged result. Each value is checked, whole, against GitHub's name rules (logins: letters, digits, `-` and `_` for Enterprise Managed Users), so a filter cannot add a qualifier of its own.

Draft pull requests are included (no `draft:` qualifier): a draft assigned to you or with a review requested is still work.

Per source:

| Source | Parameter | Default | Meaning |
|---|---|---|---|
| `review_requests` | `due_in_business_days` | 1 | working days (Monday to Friday, no holidays) after the run's day; 0 is that day, even a Saturday or Sunday; `null` no due date |
| `review_requests` | `direct_only` | false | only requests made to you by name, not to one of your teams (`user-review-requested:@me`) |
| `assigned` | `milestone_due` | true | due on the milestone's due date when there is one |
| `assigned` | `due_in_days` | `null` | otherwise, this many days after the run's day (`null`: no due date) |
| `assigned`, `mentions` | `skip_covered` | false | leave out what a stronger kind already returns (below) |
| `mentions` | `due_in_days` | 2 | days after the run's day (`null`: no due date) |

**The run's day** is `today` when given, else the current date in `timezone`, else in the instance time zone (the `TZ` the plugin host sets), else UTC. Due dates are computed from that day only, so the same day gives the same tasks. A run on a Saturday with `due_in_business_days: 1` is due Monday. A milestone's due date is the calendar date set on GitHub: the API reports it as a UTC timestamp (`2026-11-06T07:00:00Z` for 6 November) and its **UTC date** is used, so it never moves a day earlier west of UTC. A milestone already past gives a past due date: the task is **overdue** at once. Offsets are 0 to 365. These due defaults (1 business day, milestone or none, 2 days) are provisional and may change after dogfooding.

### Data

```json
{
  "kind": "review_requested",
  "day": "2026-10-30",
  "items": [
    {
      "fingerprint": "github:pr:PR_kwDOSYNTH0042",
      "type": "pr",
      "repo": "acme-org/widgets",
      "number": 42,
      "title": "Add the widget cache",
      "url": "https://github.com/acme-org/widgets/pull/42",
      "due": "2026-11-02"
    }
  ],
  "truncated": false,
  "skipped": 0,
  "partial": false,
  "errors": 0
}
```

- **Links.** Every task carries the item's link (`task.create`'s `link`, horae-sdk 0.2.0, schemas decision 52), and the data keeps it as `url`. A URL in the title is escaped so it is never a link; the link is a field of its own. It is built from GitHub's repository name and the integer number (`https://github.com/<owner>/<repo>/pull|issues/<number>`), host fixed to github.com, never from its `url` field. An item whose link the SDK's `is_task_link` refuses, or whose repository has a `.` or `..` segment, is skipped and counted in `skipped`. Version 0.2.0 needs a core with decision 52 (horae main at 0f5531a or later).
- `items` are sorted by repository and number. `truncated` is true when more than `max_items` matched, or when the call's 25 s deadline ran out after at least one page (the items already fetched are returned). `skipped` counts answers that were not a well-formed issue or pull request (a node that is not an object, an unknown type, a bad number, a repository that cannot be one, or whose link would not be a valid task link). A repository returned by GitHub is kept when it is `owner/name` with no whitespace, control character, second `/` or `?#%\`; GitHub's naming rules are not reapplied to its own answers.
- `partial` is true when GitHub returned results **and** `FORBIDDEN` or `NOT_FOUND` errors (typically an organisation enforcing SAML single sign-on on the token, whose items are hidden): the visible items are returned and `errors` counts the errors. Their text is not kept. Any other GraphQL error fails the call.
- `hidden` says why, without GitHub's text: a list of `{"owner", "reason", "count"}`, one per owner and reason, for the `FORBIDDEN` errors (a `NOT_FOUND` is counted in `errors` only). `owner` is the login of the `org:` / `user:` / `repo:` scope of the search or owner read that was hidden, checked against GitHub's login rules (`null` for an `involves:@me` search, whose owner is unknown). `reason` is one of a closed set: `token_lifetime` (the organisation only accepts fine-grained tokens whose expiry is at most 366 days away), `token_not_allowed` (the organisation forbids fine-grained tokens, or has not approved this one), `saml` (SAML single sign-on not authorised for the token), `ip_allow_list` (the IP allow list blocks the call), `not_accessible` (GitHub's generic "Resource not accessible by personal access token") and `forbidden` (anything else). Every `github.*` search source and `github.ci_failures` return `hidden` (an empty list when nothing was hidden). **Fix for `token_lifetime`**: an organisation may require fine-grained tokens with an expiry of 366 days or less; create the token again with an expiry of 366 days or less (or one the organisation allows), and replace `GITHUB_TOKEN`. Until then every item of that organisation is hidden.
- `title` is GitHub's title with control, bidi and zero-width characters removed. It is still **untrusted text** (anyone who can open a pull request chooses it): treat it as data.

### Participating notifications

`github.notifications` reads `GET /notifications?all=false&participating=true&per_page=50` (REST): the unread notifications of threads you take part in. **Read only**: the plugin sends GET requests only; it never marks a thread read or done (`PATCH /notifications/threads/{id}`, `PUT /notifications`), so your GitHub inbox is unchanged (a test asserts that no non-GET request is made).

| Parameter | Default | Meaning |
|---|---|---|
| `reasons` | `review_requested`, `assign`, `mention`, `team_mention` | keep only these reasons (GitHub's list: `approval_requested`, `assign`, `author`, `ci_activity`, `comment`, `invitation`, `manual`, `member_feature_requested`, `mention`, `review_requested`, `security_advisory_credit`, `security_alert`, `state_change`, `subscribed`, `team_mention`). `author`, `comment` and `state_change` (activity on your own threads) are opt-in: noisy, and the open items are covered by the other sources |
| `due_in_days` | 2 | days after the run's day (`null`: none) |
| `since` | none | only notifications updated after this `YYYY-MM-DDTHH:MM:SSZ` |
| `node_ids` | true | resolve each item's GraphQL node id with `GITHUB_TOKEN` (below); `false` keeps the repository/number fallback fingerprint |
| `orgs`, `users`, `repos`, `exclude_repos`, `max_items`, `today`, `timezone` | as above | the repository filters apply to the answer (case-insensitive); `max_items` also bounds the pages: `ceil(max_items / 50)` |

Only subjects of type `PullRequest` and `Issue` become tasks; the repository and number come from the subject's API URL, matched strictly (`https://api.github.com/repos/<owner>/<repo>/pulls|issues/<n>`, type and path must agree) and the link is rebuilt as `https://github.com/<owner>/<repo>/pull|issues/<n>`. Anything else (Release, Discussion, CheckSuite, a malformed subject) is counted in `skipped`; a notification with another reason, or outside the repository filters, in `filtered`.

Data adds, per item, `reason`, and per call `poll_interval` (GitHub's `X-Poll-Interval`, seconds between polls; reported, **never slept for**: schedule the pipeline no more often) and `rate_limit_remaining`. Rate limits: the same rules as the other sources (a limited answer fails the call at once with the fixed message, nothing is slept through), and a page answered with `X-RateLimit-Remaining: 0` ends the walk with `truncated: true` instead of asking for the next one. `If-Modified-Since` is not used: the plugin keeps no state between calls (use `since`).

**Token.** The notifications API cannot be read with a fine-grained personal access token or a GitHub App token; it needs a **classic** personal access token with the **`notifications`** scope. That token goes in its own secret, `GITHUB_NOTIFICATIONS_TOKEN`, not in `GITHUB_TOKEN`: it is the least-privilege choice, because the read-only fine-grained search token never has to become a broader classic one, and the classic token is sent only to `GET /notifications`. Give it the `notifications` scope alone (no `repo`; the plugin does not fetch the subjects), and an expiry. Classic tokens cannot be limited to a repository; treat it as sensitive.

**Fingerprints and node ids.** The notifications API returns no GraphQL node id, so the plugin asks for it. When `GITHUB_TOKEN` (the search token) is set, one batched GraphQL query per call (aliases, **at most 50 items per query**, so at most 4 for `max_items: 200`, within its own 5 s deadline) resolves each item's repository and number to its node id, and the item gets the fingerprint the search sources give, `github:pr:<node id>` or `github:issue:<node id>`: an item in your notifications that `review_requests`, `assigned` or `mentions` already proposed is **one** task. The owner, name and number travel as GraphQL variables, never in the query text. The lookup never fails the call: without the token, with `node_ids: false`, for a repository the token cannot see (GitHub answers `NOT_FOUND` for that item only), for an answer of the other type (an issue where a pull request was expected), or when a query fails (rate limit, timeout, any error) the item keeps the fallback `github:pr:<owner>-<repo>:<number>::<digest>`. The data reports `node_ids: {"resolved", "fallback"}` (items by fingerprint kind). **Upgrading from 0.5**: an item that only your notifications propose (no search source covers it) changes fingerprint the first time it is resolved, so the core, which keys tasks on it, creates its task once more; set `node_ids: false` to keep the old keys. The classic `GITHUB_NOTIFICATIONS_TOKEN` is never sent to GraphQL.

### CI failures

`github.ci_failures` answers "which of my repositories has a red CI?" without opening each one. It reads, with GraphQL, within one 25 s deadline:

- **Default branches**: for each `orgs` / `users` owner, the owner's own repositories (not those it only collaborates on), most recently pushed first, 50 per request, with the default branch's head commit and its combined check state (`statusCheckRollup`). Archived repositories are never read; forks only with `include_forks`. Paging **stops at the first repository pushed before the window** (`pushed_within_days` before the run's day): the list is in push order, so all later ones are older too. At most 4 pages (200 repositories) per owner; more inside the window sets `truncated`. Each `repos` value is one direct read of that repository.
- **Your open pull requests** (`include_my_prs`, on by default): `is:pr is:open author:@me archived:false`, one search per `orgs` / `users` / `repos` value (as for the other sources), with each pull request's head commit and its checks. A pull request whose head commit is older than the window is left out (an abandoned branch).

A **failure** is a head commit whose combined state is `FAILURE` or `ERROR`. `PENDING` (running or expected) is not a failure, nor is a commit without any check. The failing checks are named: a check run as `<workflow> / <job>` (conclusion `FAILURE`, `TIMED_OUT`, `STARTUP_FAILURE`, `ACTION_REQUIRED` or `CANCELLED`), a commit status by its context (`FAILURE`, `ERROR`). Only the first 50 checks of a commit are read; more are reported as `unread_checks`.

| Parameter | Default | Meaning |
|---|---|---|
| `orgs`, `users` | none | owners whose repositories are read (`org:` / `user:` for the pull request search, so put an organisation in `orgs` and an account in `users`) |
| `repos` | none | also these repositories, `owner/name`, read one by one |
| `exclude_repos` | none | never these repositories (at most 20) |
| `pushed_within_days` | 30 | only repositories pushed (and pull requests whose head was committed) in the last N days before the run's day, 0 to 365: a stale repository with a red badge is not news |
| `include_my_prs` | true | also your own open pull requests whose head fails |
| `include_forks` | false | also forks the owner has |
| `notify` | false | propose one `notify.push` per failure (below); the pipeline then needs a `notify` entry |
| `notify_max` | none (every failure) | at most this many pushes per call, 1 to 100; see "Pushes" |
| `max_items` | 50 | at most this many failures, 1 to 100 (`truncated` past it) |
| `today`, `timezone` | as above | the run's day, for the window |

At least one of `orgs`, `users` or `repos` is required (`github: ci_failures needs orgs, users or repos`); at most 8 values in all, checked like the other sources' filters. Archived repositories are always skipped: there is no `include_archived`, nobody fixes the CI of an archived repository.

**Data.** One item per failure, sorted by repository, then the default branch before pull requests:

```json
{
  "kind": "ci_failures",
  "day": "2026-10-30",
  "items": [
    {
      "fingerprint": "github:ci:acme-org-api:branch-main:9c13c49…::9effecc7dadaa78a",
      "repo": "acme-org/api",
      "branch": "main",
      "number": null,
      "sha": "9c13c49",
      "checks": ["Nightly / build", "Nightly / test"],
      "more_checks": 0,
      "unread_checks": 0,
      "since": "2026-10-30",
      "url": "https://github.com/acme-org/api/actions/runs/9001",
      "title": "main: Nightly / build, Nightly / test failing, head commit of 2026-10-30 (9c13c49)"
    }
  ],
  "checked": 9,
  "truncated": false,
  "filtered": 7,
  "skipped": 0,
  "partial": false,
  "errors": 0,
  "hidden": [],
  "notify_skipped": 0
}
```

- `number` is the pull request's number, `null` for a default branch; `since` is the **head commit's date** (UTC), not when the failure started: GitHub's check state carries no such time, so a branch red for weeks shows its latest commit's date (the title and the push say `head commit of <date>`, and the field keeps its name for existing notes). `notify_skipped` is how many failures `notify_max` left without a push (below); `title` is a one-line summary for a note (`obsidian.render_note` prints `<repo> <number> <title>`).
- `checked` counts the head commits looked at; `filtered` what was left out on purpose (older than the window, fork, archived, excluded, another owner's, an empty repository); `skipped` malformed answers; `partial` and `errors` an owner or repository GitHub hid (`FORBIDDEN`, `NOT_FOUND`, typically SAML single sign-on) or did not find. **Checks hidden.** On a **private** repository a fine-grained token reads the commit's combined state but not its check runs (no permission covers them): the failure is still reported, with `checks: []` and `checks_hidden: true`, and its title says `checks hidden from this token`. That is not an error and not `partial`; with some checks readable (commit statuses, say) they are listed and `checks_hidden` is still true. A repository or owner that is really missing still counts in `errors`. A classic token with the `repo` scope reads the check runs (see "The token"). When **no** owner or repository could be read at all the call fails (`github: no owner or repository could be read`) instead of returning an empty list.
- **Identity**: the fingerprint is the repository, the ref (`branch-<name>` or `pr-<number>`) and the head commit's full SHA. A new commit that still fails is a **new** failure; the same failing commit has the same fingerprint in every run. A failing commit that is re-run green and then fails again keeps its fingerprint. The ref uses the branch name as shown (sanitised, 100 characters at most): two branch names that are identical after that on the same commit collapse into one failure (the same commit failing, one push), and renaming a branch gives its failing head a new key, hence one new push; both are accepted, since a key built on the branch name is what makes a push per branch and commit.
- **Links** are never GitHub's URLs as they are: the workflow run page is used only when its URL is exactly `https://github.com/<this repository>/actions/runs/<digits>` (no job suffix, query, fragment, other host or other repository); otherwise the link is built from the repository name and the validated SHA (`https://github.com/<repo>/commit/<sha>`) or pull request number (`…/pull/<n>`). A commit status's `targetUrl` (any host an integration chose) is never used. An item whose link the SDK's `is_task_link` refuses is skipped.
- **Untrusted text**: branch, workflow, job and status names are chosen by anyone who can push a branch or a workflow, or post a commit status. They reach the data and the pushes through the SDK's `escape_title` then `plain_text` (control, bidi and zero-width characters removed, 100 characters at most each, 10 names at most, then `and N more`). They are data: `obsidian.render_note` escapes them again for a note, plugin-ntfy sends them as plain text (never Markdown). `tests/recordings/ci-failures-hostile.json` shows hostile names and links.

**Pushes** (`notify: true`). One `notify.push` per failure, titled `CI failing: <repo> <branch>` (or `#<number> (<branch>)`), with the failing checks and the short SHA as body, the link above, and `key` set to the failure's fingerprint. The core sends a keyed push **once for good** (schemas decision 57, amended: the dedupe key is the pipeline, the notify entry and the key, never pruned), so a pipeline that runs every 30 minutes pushes each failing commit once, when it first appears, and again only for a new failing commit. Neither the plugin nor plugin-ntfy keeps state for this (plugin-ntfy keeps none for a keyed push, whatever its `dedupe`). On the first run every current failure is pushed once: **`notify_max`** bounds that burst (the first `notify_max` failures in the data's order, by repository, are pushed; the others stay in the data, counted in `notify_skipped`). The plugin keeps no state, so it cannot tell a first run from the next: failures already pushed in earlier runs still take part of the bound, and a failure past it is not pushed while the bound is full. Use it to tame a first run (then remove it or raise it), not as a permanent setting. A push's link is routed only to a `notify` entry whose `link_prefix` covers it, so give each owner its own `ntfy.push` entry with `link_prefix: https://github.com/<Owner>/` (canonical case, not overlapping); a push no entry covers is skipped by the core (`intent_skipped`), not sent. `notify.push` is a new permission in 0.4.0.

**A note** of the current failures: `obsidian.render_note` (plugin-obsidian) reads any `github.*` source's `items` and prints each as `<repo> <number> <title>` in its `${github}` section, escaped; give it a template of its own and an owned block. The example below rewrites the block on every run, so the note always lists the failures of the last run (`_None._` when all is green). What the note does not show: links (render_note writes plain escaped bullets) and this source's `truncated` / `partial` flags (render_note turns only the task source's flags into alerts; its `expect` alert does fire when the source failed).

Example, [`examples/ci-watch.yaml`](examples/ci-watch.yaml) (every 30 minutes on weekdays, 08:00 to 20:30, no LLM):

```yaml
name: ci-watch
trigger: { cron: "*/30 8-20 * * 1-5" }
steps:
  - collect:
      - github.ci_failures: { orgs: [Acme-Org], users: [ada], today: "{{date}}", notify: true }
  - transform:
      - obsidian.render_note:
          today: "{{date}}"
          block: ci-failures
          expect: [github.ci_failures]
          template: |
            # CI failures

            Updated ${date}.

            ## Alerts

            ${alerts}

            ## Failing

            ${github}
  - act:
      - obsidian.write_note: { path: "Horae/CI.assistant.md", mode: replace-owned }
  - notify:
      - ntfy.push: { server: https://ntfy.invalid, topic: CHANGE-ME, link_prefix: "https://github.com/Acme-Org/", dedupe: "off" }
      - ntfy.push: { server: https://ntfy.invalid, topic: CHANGE-ME, link_prefix: "https://github.com/ada/", dedupe: "off" }
```

**Cost.** One request per 50 repositories inside the window and per `repos` value, plus one per 50 open pull requests per owner: a few requests every run, well inside GitHub's 5,000 points an hour. A rate limit fails the call at once, with the usual message, never slept through.

### Updates: new for you

`github.updates` answers "what moved since I last looked, or since I last did something?" as a to-do: the issues and pull requests of the owners you follow (and, with `involves_me`, any that involve you) where **someone else** did something **after your last action** on them. Reply, review or push and the item drops out on the next run. It reads, with GraphQL, within one 25 s deadline:

- **You**: `viewer { login }`, one request (the token's account).
- **Candidates**: one search per `orgs` / `users` / `repos` value, `is:open archived:false sort:updated-desc updated:>=<day − since_days>` (no `is:open` with `include_closed`), and with `involves_me` one more, `involves:@me` with the same qualifiers, anywhere. At most 100 per search (two requests), newest first, merged by node id. `exclude_repos` is added to every search.
- **The last 25 timeline events** of each: comments, reviews, commits, force pushes, closes, reopens, merges and review requests, with the item's author and creation time.

**New for you.** Your last action is your latest event that GitHub timestamps and attributes to the token's account (a comment, a submitted review, a force push, a close, a merge, a reopen, a review request you made), or the item's creation when you opened it. A **commit is never your action**: its date and author are whatever the committer wrote (anyone can author a commit with your e-mail and a date in 2099), so a commit in your name is ignored, and a plain push of yours does not count as acting (GitHub gives no server time for it; a force push does). Any time after the end of the run's day (UTC) is clamped to it. The latest **other** event is the latest one by anyone else, with:

- **bots** left out unless `include_bots`: an actor of type `Bot` (dependabot, renovate, github-actions, any GitHub App) or a login ending in `[bot]`, and a **commit whose author is no GitHub account** (an automation's e-mail, mostly; a person committing from an e-mail not on their account counts as one too);
- a **review request** counted only when it asks **you** (by name; a request to one of your teams is not counted);
- a pending (unsubmitted) review never counted; a merge preferred to its close at the same second.

The item is listed when that event is **after** your last action (or you never acted) and on or after the window's first day. So an issue opened in the window by someone else in a followed owner is listed until you act on it; your own pull request is listed when someone reviews, comments or pushes, not before. No state is kept by the plugin: everything comes from GitHub's timeline. When your last action is older than the 25 events read, every other event read is after it, which is what "new" means anyway.

Limits of a timeline-only reading:

- **Backdated plain pushes stay unseen.** A pushed commit carries the date its committer wrote, GitHub gives no server time for a plain (non-force) push, and a commit's date is capped by the item's `updatedAt` (below). A commit dated before your last action therefore does not count as news, though it was pushed after it. Force pushes have GitHub's own time and are not affected. Nothing in the timeline lets the plugin do better without keeping state.
- **Same second.** Times have a resolution of one second. An event in the same second as your action counts as new when it comes **after** yours in the timeline (GitHub's order), and as old when it comes before.
- **Fail closed on commits.** A commit's date is bounded by the item's `updatedAt`; an item without one (GitHub always sends it) has its commits ignored rather than trusted.

| Parameter | Default | Meaning |
|---|---|---|
| `orgs`, `users`, `repos` | none | owners and repositories you follow, one search each (as for the other sources); at least one is required |
| `exclude_repos` | none | never these repositories (at most 20) |
| `involves_me` | true | also items anywhere that involve you (`involves:@me`: you opened, were assigned, mentioned or commented) |
| `since_days` | 14 | look-back window, 0 to 90: only items updated, and events, on or after the run's day minus this |
| `include_closed` | false | also closed and merged items (a merge or close by someone else is then news) |
| `include_bots` | false | count bot activity too (above) |
| `notify` | false | propose one `notify.push` per item (below); the pipeline then needs a `notify` entry |
| `notify_max` | none (every item) | at most this many pushes per call, 1 to 100, newest first; see "Since the last check" |
| `max_items` | 50 | at most this many items, 1 to 100, newest first (`truncated` past it) |
| `today`, `timezone` | as above | the run's day, for the window |

**Data.** One item per issue or pull request, newest event first:

```json
{
  "kind": "updates",
  "day": "2026-10-30",
  "since": "2026-10-16",
  "items": [
    {
      "fingerprint": "github:update:PR_up4:PRR_8",
      "item_fingerprint": "github:pr:PR_up4",
      "type": "pr",
      "repo": "acme-up/app",
      "number": 4,
      "item_title": "Add retries",
      "event": "approved",
      "by": "carol",
      "at": "2026-10-26T10:00:00Z",
      "your_last_action": "2026-10-18T10:00:00Z",
      "url": "https://github.com/acme-up/app/pull/4",
      "title": "Add retries — carol approved"
    }
  ],
  "checked": 11,
  "truncated": false,
  "filtered": 4,
  "skipped": 1,
  "partial": false,
  "errors": 0,
  "hidden": [],
  "notify_skipped": 0
}
```

- `event` is one of `opened`, `commented`, `reviewed`, `approved`, `changes_requested`, `pushed`, `merged`, `closed`, `reopened`, `review_requested`; `title` says it in words (`requested changes`, `requested your review`) for a note: `obsidian.render_note` prints `<repo> <number> <title>`, so a line reads `acme-up/app 4 Add retries — carol approved`. `by` is `someone` for a deleted account or a commit without one. `your_last_action` is `null` when you never acted (in the events read).
- `checked` counts the well-formed items read; `filtered` those with nothing new for you (or excluded); `skipped` malformed answers (not an issue or pull request, a bad number, a repository that cannot be one, a link `is_task_link` refuses). A malformed timeline event is ignored. `truncated`: more than 100 candidates in a search, more than `max_items` new items, or the deadline cut the reads short (the items read so far are returned). `partial` and `errors`: results GitHub hid (`FORBIDDEN`, `NOT_FOUND`, typically an organisation enforcing SAML single sign-on on the token).
- **Identity**: `fingerprint` is the item's node id and the latest other event's id; `item_fingerprint` is the item's fingerprint in the other search sources (`github:pr:<node id>`), to match a task.
- **Untrusted text**: titles and logins are chosen by others. They reach the data and the pushes through the SDK's `escape_title` then `plain_text` (control, bidi and zero-width characters removed; titles 150 characters at most, logins 60). They are data: `obsidian.render_note` escapes them again, plugin-ntfy sends plain text. Links are built from the validated repository and the integer number (`https://github.com/<repo>/pull|issues/<n>`), never taken from GitHub. `tests/recordings/updates-hostile.json` shows them neutralised.

**Since the last check** (`notify: true`). One `notify.push` per item, titled `<repo>#<n>: <who> <what>`, the item's title as body, its link, and `key` set to its fingerprint. The core sends a keyed push **once for good** (schemas decision 57, amended), and the key holds the latest other event, so each event is pushed once and newer activity on the same item is pushed again. On the first run every current item is pushed once: **`notify_max`** bounds that burst (the `notify_max` newest items are pushed, the others stay in the data, counted in `notify_skipped`). The plugin keeps no state, so it cannot tell a first run from the next: items already pushed in earlier runs still take part of the bound. Use it to tame a first run (then remove it or raise it), not as a permanent setting; it is the same parameter as in [CI failures](#ci-failures). Route the pushes as for [CI failures](#ci-failures): one `ntfy.push` entry per owner with its `link_prefix`; an `involves_me` item of an owner no entry covers is not pushed (`intent_skipped`) but stays in the note.

**A note** of what is new: `obsidian.render_note` with a block of its own, rewritten every run (`_None._` when you are up to date). What the note does not show: links (plain escaped bullets), the order (render_note sorts the lines alphabetically, so by repository and number, not by time), the time of the event, and this source's `truncated` / `partial` flags (only the task source's flags become alerts; its `expect` alert fires when the source failed).

Example, [`examples/updates.yaml`](examples/updates.yaml) (every 30 minutes on weekdays, 08:00 to 20:30, no LLM):

```yaml
name: github-updates
trigger: { cron: "*/30 8-20 * * 1-5" }
steps:
  - collect:
      - github.updates: { orgs: [Acme-Org], users: [ada], today: "{{date}}", notify: true }
  - transform:
      - obsidian.render_note:
          today: "{{date}}"
          block: github-updates
          expect: [github.updates]
          template: |
            # GitHub: new for you

            Updated ${date}.

            ## Alerts

            ${alerts}

            ## New since your last action

            ${github}
  - act:
      - obsidian.write_note: { path: "Horae/GitHub.assistant.md", mode: replace-owned }
  - notify:
      - ntfy.push: { server: https://ntfy.invalid, topic: CHANGE-ME, link_prefix: "https://github.com/Acme-Org/", dedupe: "off" }
      - ntfy.push: { server: https://ntfy.invalid, topic: CHANGE-ME, link_prefix: "https://github.com/ada/", dedupe: "off" }
```

**Cost.** One request for you, then one per 50 candidates per search (two at most per search, eighteen searches at most: eight values and `involves:@me`, each as an `is:issue` and an `is:pr` search), each a search with 25 timeline events per item: a few points per run, well inside GitHub's 5,000 points an hour. A rate limit fails the call at once, never slept through.

### One task per item

The fingerprint is the item's identity, not the kind that found it: `github:pr:<node id>` or `github:issue:<node id>`, from GitHub's global node id, which does not change when the item is renamed or edited. When the node id is missing or malformed, it falls back to the repository and number, `github:pr:<owner>-<repo>:<number>::<digest>` (for example `github:pr:acme-org-api:42::f93d83b33a08d274`; the SDK's `intents.fingerprint` replaces `/` and adds a digest of the raw parts). An issue **transferred** to another repository gets a new node id on GitHub, so it becomes a new task; the old one is not closed by the plugin.

So a pull request that is both assigned to you and waiting for your review is **one task**, whichever sources a pipeline uses: the fingerprint is per item. An item seen twice in one call (search pages can shift, two filter searches can overlap) is kept once.

**The first proposal wins.** The fingerprint does not say which kind found the item, so when several kinds (or several runs) propose the same item with different titles or due dates, which one ends up as the task is the tasks sink's choice, not the plugin's. What the core does today (horae `main`): `task.create.fingerprint` is documented as the identity "used to deduplicate tasks across runs and sources", but the apply planner (`horae.apply.plan.describe`) previews every `task.create` as a `create` without looking for an existing task, and no sink that applies `task.create` ships yet; the store's task index (`Store.upsert_task`) would replace the title and due date of an existing fingerprint, but nothing on the apply path calls it. So **assume the first task created for an item keeps its title and due date**, and that later runs do not update it, unless and until the tasks sink updates duplicates.

`skip_covered` (off by default) makes one run propose each item once even before the core dedupes: `github.assigned` adds `-review-requested:@me` to its search and `github.mentions` adds `-assignee:@me -review-requested:@me`, so the strongest kind gives the title (review request, then assignment, then mention). It is off by default because the fingerprint already collapses duplicates and because a pipeline that does not collect the stronger kind would lose items; turn it on when a pipeline collects all three and you want the stronger kind's title.

### Untrusted titles

Titles reach a task only through the SDK's `intents.task_create`, which escapes Obsidian Tasks signifiers (a title holding `📅 2020-01-01 🔁 every day` does not schedule anything), Markdown, wikilinks and bare links, and removes line breaks, control and bidi characters (threat model T02). `tests/recordings/review-requests-hostile.json` shows a hostile title neutralised.

## Install and configure

```
horae plugin add github@0.5.2
```

Pin `runtime.command` to an absolute interpreter that has this package (the host's `PATH` is fixed and its working directory is empty), for example `/opt/plugins/github/.venv/bin/python -m horae_github`.

### The token

Create a **fine-grained personal access token** (GitHub, Settings, Developer settings):

- **Resource owner**: your account, or the organisation whose repositories you want (a fine-grained token sees one owner; the organisation may have to approve it).
- **Repository access**: all repositories, or the ones you want.
- **Repository permissions**, all **read-only**: `Pull requests: Read`, `Issues: Read`, `Metadata: Read` (mandatory). Nothing else, no account permissions, no write.
- An expiry date.

Store it as the instance secret `GITHUB_TOKEN` (`/run/secrets/GITHUB_TOKEN` by default). The plugin host injects it only into this plugin. The plugin reads it at each call and sends it only in the `Authorization` header to `api.github.com`; it never appears in data, intents, errors, logs or recordings.

`github.notifications` uses a second secret, see below.

**For `github.ci_failures`**, add these repository permissions, all **read-only**:

| Permission | Why |
|---|---|
| `Metadata: Read` | the repository list, push dates, default branch (mandatory anyway) |
| `Contents: Read` | the default branch's head commit (private repositories) |
| `Commit statuses: Read` | commit statuses (`StatusContext`) in the combined state |
| `Actions: Read` | optional: the workflow run behind a check run (its workflow name and run link). GitHub's documentation lists it for the REST workflow run endpoints only; whether GraphQL `workflowRun` needs it is **not documented**, and it only matters on private repositories, where a fine-grained token cannot read check runs at all (below). Without it, or without the run, a check is named by its job alone and the link falls back to the commit or pull request |
| `Pull requests: Read` | the search for your open pull requests (already needed) |

**For `github.updates`**: `Metadata`, `Issues` and `Pull requests` read-only (as the search sources), plus `Contents: Read` for the commits of a pull request's timeline (their author) on private repositories. Nothing else, no write; `viewer { login }` needs no extra permission. The same `GITHUB_TOKEN` serves every source.

Check runs are a gap in GitHub's documentation: the `Checks` permission exists only for GitHub Apps, and no fine-grained personal access token permission is documented for reading check runs. On **public** repositories every token reads them. On a **private** repository a fine-grained token **cannot** read them (verified: GitHub answers `FORBIDDEN`, "Resource not accessible by personal access token"): a failing commit is still detected from its combined state, but reported as `checks_hidden: true` without the names. To see the names, a classic token with the `repo` scope can, but it grants write access to every repository, so prefer keeping such repositories public or watching them another way. A fine-grained token sees the private repositories of **one** resource owner (your account, or one organisation that approved it) and every public repository; `GITHUB_TOKEN` is one secret, so watch public repositories of other owners, or the private ones of the token's owner.

## Permissions

Matches `plugin.yaml` exactly.

| Kind | Value | Why |
|---|---|---|
| intent | `task.create` | one task per item |
| intent | `notify.push` | `github.ci_failures` and `github.updates` with `notify: true`: one push per new failure or update |
| network | `api.github.com` | the GraphQL API (`POST /graphql`) and `GET /notifications`, the only host contacted |
| secret | `GITHUB_TOKEN` | read-only fine-grained token, above |
| secret | `GITHUB_NOTIFICATIONS_TOKEN` | classic token, `notifications` scope only, for `github.notifications` |
| paths | none | no filesystem access |

### Which token each source uses

| Source | Secret | Token type | Scopes / permissions | Requests |
|---|---|---|---|---|
| `github.review_requests` | `GITHUB_TOKEN` | fine-grained PAT | `Pull requests: Read`, `Issues: Read`, `Metadata: Read` | `POST /graphql` (search) |
| `github.assigned` | `GITHUB_TOKEN` | fine-grained PAT | same | `POST /graphql` (search) |
| `github.mentions` | `GITHUB_TOKEN` | fine-grained PAT | same | `POST /graphql` (search) |
| `github.notifications` | `GITHUB_NOTIFICATIONS_TOKEN` (and `GITHUB_TOKEN` for node ids, optional in effect) | classic PAT (and the fine-grained one) | `notifications` only (node ids: `Issues`, `Pull requests`, `Metadata`: Read) | `GET /notifications` (never marks anything read); `POST /graphql` (`repository.issueOrPullRequest`, batched) |
| `github.ci_failures` | `GITHUB_TOKEN` | fine-grained PAT | `Metadata`, `Contents`, `Commit statuses`, `Pull requests`: Read (and `Actions`, optional) | `POST /graphql` (`repositoryOwner`, `repository`, search) |
| `github.updates` | `GITHUB_TOKEN` | fine-grained PAT | `Metadata`, `Issues`, `Pull requests`, `Contents`: Read | `POST /graphql` (`viewer`, search with timelines) |

**`GITHUB_NOTIFICATIONS_TOKEN` is optional.** Declared in `permissions.optional_secrets`, it is injected only when it resolves, so you create the classic `notifications` token only if you use `github.notifications`; without it that source fails with `secret_missing: GITHUB_NOTIFICATIONS_TOKEN is not set`. The search sources always need `GITHUB_TOKEN`. Optional secrets need a core with decision 56 (unreleased 0.0.x); the manifest's `horae:` range stays `>=0,<0.2`, but an older core rejects `optional_secrets`.

## How it talks to GitHub

**GraphQL search, not REST.** Each kind is a search qualifier (`review-requested:@me`, `assignee:@me`, `mentions:@me`), and one GraphQL `search` returns exactly the fields a task needs (node id, number, title, repository, milestone due date) for issues and pull requests, 50 per request. REST `/search/issues` needs as many calls, has its own 30-per-minute search budget, and returns whole user and label objects to drop.

- **Bounded**: at most `ceil(max_items / 50)` requests per search (4 at most) and 16 searches per call (8 values, each an `is:issue` and an `is:pr` search; 8 for `github.review_requests`); **one 25 s deadline per call** (under the host's 30 s call timeout) across every page and retry; 10 s per socket operation, never more than the time left; 4 MiB per answer. The body is read with `read1` (one `recv` at a time) and a watchdog shuts the socket down at the deadline, so an answer dripped byte by byte, headers or body, cannot keep the call open. The host name is resolved in a thread waited for at most the time left; if resolution hangs, that thread is abandoned (it holds no socket) and the call fails with `request timed out`. Out of time after at least one page: the items fetched so far, `truncated: true`.
- **No ambient proxy, https only, no redirect**: `http.client` with a verifying TLS context, which never reads `HTTPS_PROXY` or `.netrc`. A 3xx is refused, never followed.
- **No long waits**: a rate limit is reported at once, never slept through. A gateway error (502, 503, 504) is retried once after 1 s, when the deadline leaves room.
- **Direct connections today.** The plugin connects to `api.github.com` itself. From milestone M5 the core's egress proxy becomes the network for isolated plugins; this plugin will need to use the proxy the host provides once M5 defines how (a follow-up, not done here).

## Failures

Each source either returns its data (flagged `truncated` or `partial` when some items are missing, above) or fails with a short, fixed message (never an upstream body or the token). In a `collect` step with other sources, a failing source makes the step and the run **degraded**: the pipeline continues on the partial data, `act` steps are skipped, `notify` still runs. So when one kind fails (say mentions are rate limited), the other kinds' items are still there and the failure is flagged on the run.

| Message | Cause |
|---|---|
| `github: GITHUB_TOKEN is not set` (`GITHUB_NOTIFICATIONS_TOKEN` for notifications) | the secret is missing |
| `github: authentication failed (GITHUB_TOKEN refused)` (`GITHUB_NOTIFICATIONS_TOKEN` for notifications) | HTTP 401: token wrong, expired or revoked |
| `github: access denied (HTTP 403)` | the token lacks a permission or the organisation refuses it |
| `github: rate limited; retry after N s` | HTTP 403/429 with `Retry-After` (seconds or an HTTP date), or `X-RateLimit-Remaining: 0` with `X-RateLimit-Reset`, or a GraphQL `RATE_LIMITED` error |
| `github: rate limited; retry later` | HTTP 429, or 403 with `X-RateLimit-Remaining: 0`, without a time to wait |
| `github: possibly rate limited; retry later` | HTTP 403 whose body names GitHub's secondary rate limit |
| `github: query failed (INTERNAL)` | a GraphQL error other than `FORBIDDEN` / `NOT_FOUND` next to results (those give `partial: true` instead), or any error without results |
| `github: service unavailable (HTTP 503)` | a gateway error, twice |
| `github: request timed out`, `github: connection failed (...)`, `github: response too large`, `github: malformed response`, `github: unexpected ...` | network or protocol trouble |
| `github: no owner or repository could be read` | `github.ci_failures`: GitHub returned nothing for every `orgs`, `users` and `repos` value (misspelt, or hidden by SAML) |
| `github: updates needs orgs, users or repos`, `github: since_days is 0 to 90` | a bad `github.updates` parameter |
| `github: ci_failures needs orgs, users or repos`, `github: pushed_within_days is 0 to 365`, `github: max_items is 1 to 100` | a bad `github.ci_failures` parameter |
| `github: invalid reasons filter`, `github: since must be YYYY-MM-DDTHH:MM:SSZ`, `github: invalid org filter`, `github: at most 8 orgs, users and repos values in all (one search each)`, `github: max_items is 1 to 200`, `github: due offsets are 0 to 365 days`, `github: today must be YYYY-MM-DD`, `github: unknown time zone` | a bad pipeline parameter |

## Development

See [AGENTS.md](AGENTS.md) for the commands. Tests use synthetic GraphQL answers only (`tests/fixtures/graphql.json`, repositories under `acme-org/`): a fake transport for the recordings and a fake server on 127.0.0.1 for the HTTP client. No test touches the network.

## Planned work

| Milestone | Epic |
|---|---|
| M3 Dogfood | **GitHub source** — Review requests, assignments, mentions and notifications as tasks. |

Stories, estimates and acceptance criteria are in this repository's [issues](../../issues) and on the [roadmap project](https://github.com/orgs/horae-dev/projects/1).

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
