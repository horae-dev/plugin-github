<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-github

Horae plugin: GitHub review requests, assigned issues, mentions and participating notifications as tasks.

> 🚧 **Early.** Review requests, assignments and mentions work (0.1.0). Participating notifications are planned (#3).

## What it does

Three sources for a pipeline's `collect` step. Each one searches GitHub for open items and returns them as data, and proposes one `task.create` intent per item. The Horae core validates and applies the intents; the plugin never writes anything.

| Source | Finds | Task title | Default due date |
|---|---|---|---|
| `github.review_requests` | open pull requests whose review is requested from you or from a team you are in (`review-requested:@me`; `direct_only: true` for you only, `user-review-requested:@me`) | `Review owner/repo#42: <title>` | the run's day + 1 business day |
| `github.assigned` | open issues and pull requests assigned to you | `Work on owner/repo#42: <title>` | the milestone's due date, else none |
| `github.mentions` | open issues and pull requests that mention you | `Answer mention in owner/repo#42: <title>` | the run's day + 2 days |

```yaml
# pipelines/daily-brief.yaml
steps:
  - collect:
      - github.review_requests: { today: "{{date}}" }
      - github.assigned: { today: "{{date}}", orgs: [acme-org] }
      - github.mentions: { today: "{{date}}", due_in_days: 3 }
```

### Parameters

All three take:

| Parameter | Default | Meaning |
|---|---|---|
| `orgs` | none | only repositories of these organisations (`org:` qualifier) |
| `users` | none | only repositories of these users (`user:`) |
| `repos` | none | only these repositories, `owner/name` (`repo:`) |
| `exclude_repos` | none | never these repositories (`-repo:`), at most 20 |
| `max_items` | 50 | at most this many items, 1 to 200 (one request per 50) |
| `today` | the clock | the run's day, `YYYY-MM-DD`. Pass `"{{date}}"`: the due dates then depend only on the run, so a replay gives the same tasks |
| `timezone` | the instance's | an IANA time zone, used only when `today` is not given |

**Filters are "any of".** GitHub ANDs qualifiers of different kinds (`org:acme repo:other/api` matches nothing), so the plugin runs **one search per `orgs`, `users` or `repos` value** and merges the results, each item once (by node id): an item is returned when it matches any value. `exclude_repos` is added to every search. At most 8 `orgs`, `users` and `repos` values in all (8 searches); more is refused with `github: at most 8 orgs, users and repos values in all (one search each)`. Without any of them there is one search. Each value is checked, whole, against GitHub's name rules (logins: letters, digits, `-` and `_` for Enterprise Managed Users), so a filter cannot add a qualifier of its own.

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

- **Links.** The `task.create` intent has no link field yet (its schema allows only `title`, `fingerprint`, `due` and `time`), and a URL in the title is escaped so it is not a link. The item's link is therefore in the data, `url`, for the pipeline's later steps. It is built from the repository and number (`https://github.com/<owner>/<repo>/pull|issues/<number>`), never copied from the answer. The core follow-up [horae#122](https://github.com/horae-dev/horae/issues/122) adds a link to `task.create`; the plugin will then pass it on the task as well.
- `items` are sorted by repository and number. `truncated` is true when more than `max_items` matched, or when the call's 25 s deadline ran out after at least one page (the items already fetched are returned). `skipped` counts answers that were not a well-formed issue or pull request (a node that is not an object, an unknown type, a bad number, a repository that cannot be one). A repository returned by GitHub is kept when it is `owner/name` with no whitespace, control character, second `/` or `?#%\`; GitHub's naming rules are not reapplied to its own answers.
- `partial` is true when GitHub returned results **and** `FORBIDDEN` or `NOT_FOUND` errors (typically an organisation enforcing SAML single sign-on on the token, whose items are hidden): the visible items are returned and `errors` counts the errors. Their text is not kept. Any other GraphQL error fails the call.
- `title` is GitHub's title with control, bidi and zero-width characters removed. It is still **untrusted text** (anyone who can open a pull request chooses it): treat it as data.

### One task per item

The fingerprint is the item's identity, not the kind that found it: `github:pr:<node id>` or `github:issue:<node id>`, from GitHub's global node id, which does not change when the item is renamed or edited. When the node id is missing or malformed, it falls back to the repository and number, `github:pr:<owner>-<repo>:<number>::<digest>` (for example `github:pr:acme-org-api:42::f93d83b33a08d274`; the SDK's `intents.fingerprint` replaces `/` and adds a digest of the raw parts). An issue **transferred** to another repository gets a new node id on GitHub, so it becomes a new task; the old one is not closed by the plugin.

So a pull request that is both assigned to you and waiting for your review is **one task**, whichever sources a pipeline uses: the fingerprint is per item. An item seen twice in one call (search pages can shift, two filter searches can overlap) is kept once.

**The first proposal wins.** The fingerprint does not say which kind found the item, so when several kinds (or several runs) propose the same item with different titles or due dates, which one ends up as the task is the tasks sink's choice, not the plugin's. What the core does today (horae `main`): `task.create.fingerprint` is documented as the identity "used to deduplicate tasks across runs and sources", but the apply planner (`horae.apply.plan.describe`) previews every `task.create` as a `create` without looking for an existing task, and no sink that applies `task.create` ships yet; the store's task index (`Store.upsert_task`) would replace the title and due date of an existing fingerprint, but nothing on the apply path calls it. So **assume the first task created for an item keeps its title and due date**, and that later runs do not update it, unless and until the tasks sink updates duplicates.

`skip_covered` (off by default) makes one run propose each item once even before the core dedupes: `github.assigned` adds `-review-requested:@me` to its search and `github.mentions` adds `-assignee:@me -review-requested:@me`, so the strongest kind gives the title (review request, then assignment, then mention). It is off by default because the fingerprint already collapses duplicates and because a pipeline that does not collect the stronger kind would lose items; turn it on when a pipeline collects all three and you want the stronger kind's title.

### Untrusted titles

Titles reach a task only through the SDK's `intents.task_create`, which escapes Obsidian Tasks signifiers (a title holding `📅 2020-01-01 🔁 every day` does not schedule anything), Markdown, wikilinks and bare links, and removes line breaks, control and bidi characters (threat model T02). `tests/recordings/review-requests-hostile.json` shows a hostile title neutralised.

## Install and configure

```
horae plugin add github@0.1.0
```

Pin `runtime.command` to an absolute interpreter that has this package (the host's `PATH` is fixed and its working directory is empty), for example `/opt/plugins/github/.venv/bin/python -m horae_github`.

### The token

Create a **fine-grained personal access token** (GitHub, Settings, Developer settings):

- **Resource owner**: your account, or the organisation whose repositories you want (a fine-grained token sees one owner; the organisation may have to approve it).
- **Repository access**: all repositories, or the ones you want.
- **Repository permissions**, all **read-only**: `Pull requests: Read`, `Issues: Read`, `Metadata: Read` (mandatory). Nothing else, no account permissions, no write.
- An expiry date.

Store it as the instance secret `GITHUB_TOKEN` (`/run/secrets/GITHUB_TOKEN` by default). The plugin host injects it only into this plugin. The plugin reads it at each call and sends it only in the `Authorization` header to `api.github.com`; it never appears in data, intents, errors, logs or recordings.

Participating notifications (#3) will need a classic token with the `notifications` scope; this release does not use one.

## Permissions

Matches `plugin.yaml` exactly.

| Kind | Value | Why |
|---|---|---|
| intent | `task.create` | one task per item |
| network | `api.github.com` | the GraphQL API (`POST /graphql`), the only host contacted |
| secret | `GITHUB_TOKEN` | read-only fine-grained token, above |
| paths | none | no filesystem access |

## How it talks to GitHub

**GraphQL search, not REST.** Each kind is a search qualifier (`review-requested:@me`, `assignee:@me`, `mentions:@me`), and one GraphQL `search` returns exactly the fields a task needs (node id, number, title, repository, milestone due date) for issues and pull requests, 50 per request. REST `/search/issues` needs as many calls, has its own 30-per-minute search budget, and returns whole user and label objects to drop.

- **Bounded**: at most `ceil(max_items / 50)` requests per search (4 at most) and 8 searches per call; **one 25 s deadline per call** (under the host's 30 s call timeout) across every page and retry; 10 s per socket operation, never more than the time left; 4 MiB per answer. The body is read with `read1` (one `recv` at a time) and a watchdog shuts the socket down at the deadline, so an answer dripped byte by byte, headers or body, cannot keep the call open. The host name is resolved in a thread waited for at most the time left; if resolution hangs, that thread is abandoned (it holds no socket) and the call fails with `request timed out`. Out of time after at least one page: the items fetched so far, `truncated: true`.
- **No ambient proxy, https only, no redirect**: `http.client` with a verifying TLS context, which never reads `HTTPS_PROXY` or `.netrc`. A 3xx is refused, never followed.
- **No long waits**: a rate limit is reported at once, never slept through. A gateway error (502, 503, 504) is retried once after 1 s, when the deadline leaves room.
- **Direct connections today.** The plugin connects to `api.github.com` itself. From milestone M5 the core's egress proxy becomes the network for isolated plugins; this plugin will need to use the proxy the host provides once M5 defines how (a follow-up, not done here).

## Failures

Each source either returns its data (flagged `truncated` or `partial` when some items are missing, above) or fails with a short, fixed message (never an upstream body or the token). In a `collect` step with other sources, a failing source makes the step and the run **degraded**: the pipeline continues on the partial data, `act` steps are skipped, `notify` still runs. So when one kind fails (say mentions are rate limited), the other kinds' items are still there and the failure is flagged on the run.

| Message | Cause |
|---|---|
| `github: GITHUB_TOKEN is not set` | the secret is missing |
| `github: authentication failed (GITHUB_TOKEN refused)` | HTTP 401: token wrong, expired or revoked |
| `github: access denied (HTTP 403)` | the token lacks a permission or the organisation refuses it |
| `github: rate limited; retry after N s` | HTTP 403/429 with `Retry-After` (seconds or an HTTP date), or `X-RateLimit-Remaining: 0` with `X-RateLimit-Reset`, or a GraphQL `RATE_LIMITED` error |
| `github: rate limited; retry later` | HTTP 429, or 403 with `X-RateLimit-Remaining: 0`, without a time to wait |
| `github: possibly rate limited; retry later` | HTTP 403 whose body names GitHub's secondary rate limit |
| `github: query failed (INTERNAL)` | a GraphQL error other than `FORBIDDEN` / `NOT_FOUND` next to results (those give `partial: true` instead), or any error without results |
| `github: service unavailable (HTTP 503)` | a gateway error, twice |
| `github: request timed out`, `github: connection failed (...)`, `github: response too large`, `github: malformed response`, `github: unexpected ...` | network or protocol trouble |
| `github: invalid org filter`, `github: at most 8 orgs, users and repos values in all (one search each)`, `github: max_items is 1 to 200`, `github: due offsets are 0 to 365 days`, `github: today must be YYYY-MM-DD`, `github: unknown time zone` | a bad pipeline parameter |

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
