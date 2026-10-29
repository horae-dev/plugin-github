<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-github

Horae plugin: GitHub review requests, assigned issues, mentions and participating notifications as tasks.

> 🚧 **Early.** Review requests, assignments and mentions work (0.1.0). Participating notifications are planned (#3).

## What it does

Three sources for a pipeline's `collect` step. Each one searches GitHub for open items and returns them as data, and proposes one `task.create` intent per item. The Horae core validates and applies the intents; the plugin never writes anything.

| Source | Finds | Task title | Default due date |
|---|---|---|---|
| `github.review_requests` | open pull requests whose review is requested from you (`review-requested:@me`) | `Review owner/repo#42: <title>` | the run's day + 1 business day |
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
| `exclude_repos` | none | never these repositories (`-repo:`) |
| `max_items` | 50 | at most this many items, 1 to 200 (one request per 50) |
| `today` | the clock | the run's day, `YYYY-MM-DD`. Pass `"{{date}}"`: the due dates then depend only on the run, so a replay gives the same tasks |
| `timezone` | the instance's | an IANA time zone, used only when `today` is not given |

Several values of one filter match any of them (GitHub ORs a repeated qualifier). Each value is checked against GitHub's name rules, so a filter cannot add a qualifier of its own.

Per source:

| Source | Parameter | Default | Meaning |
|---|---|---|---|
| `review_requests` | `due_in_business_days` | 1 | working days (Monday to Friday, no holidays) after the run's day; 0 is that day, `null` no due date |
| `assigned` | `milestone_due` | true | due on the milestone's due date when there is one |
| `assigned` | `due_in_days` | `null` | otherwise, this many days after the run's day (`null`: no due date) |
| `assigned`, `mentions` | `skip_covered` | true | leave out what a stronger kind already returns (below) |
| `mentions` | `due_in_days` | 2 | days after the run's day (`null`: no due date) |

**The run's day** is `today` when given, else the current date in `timezone`, else in the instance time zone (the `TZ` the plugin host sets), else UTC. Due dates are computed from that day only, so the same day gives the same tasks. A task is created once per fingerprint, so its due date is fixed by the first run that sees the item. A milestone's due date is the calendar date set on GitHub (the API reports it as a UTC timestamp, and its UTC date is used, so it never moves a day earlier west of UTC). Offsets are 0 to 365.

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
  "skipped": 0
}
```

- **Links.** The `task.create` intent has no link field (its schema allows only `title`, `fingerprint`, `due` and `time`), and a URL in the title is escaped so it is not a link. The item's link is therefore in the data, `url`, for the pipeline's later steps. It is built from the validated repository and number (`https://github.com/<owner>/<repo>/pull|issues/<number>`), never copied from the answer.
- `items` are sorted by repository and number. `truncated` is true when more than `max_items` matched. `skipped` counts answers that were not a well-formed issue or pull request.
- `title` is GitHub's title with control, bidi and zero-width characters removed. It is still **untrusted text** (anyone who can open a pull request chooses it): treat it as data.

### One task per item

The fingerprint is the item's identity, not the kind that found it: `github:pr:<node id>` or `github:issue:<node id>`, from GitHub's global node id, which never changes when the item is renamed, moved or edited. When the node id is missing, it falls back to the repository and number (`github:pr:<owner>-<repo>:<number>::<digest>`).

So a pull request that is both assigned to you and waiting for your review is **one task**, whichever sources a pipeline uses. On top of that, `skip_covered` makes one run propose each item once: `github.assigned` adds `-review-requested:@me` to its search and `github.mentions` adds `-assignee:@me -review-requested:@me`, so the strongest kind gives the title and the due date (review request, then assignment, then mention). Set `skip_covered: false` when the pipeline does not collect the stronger kinds. An item seen twice in one answer (search pages can shift) is kept once.

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

- **Bounded**: at most `ceil(max_items / 50)` requests per call (4 at most), 10 s per socket operation, 20 s per request, 4 MiB per answer.
- **No ambient proxy, https only, no redirect**: `http.client` with a verifying TLS context, which never reads `HTTPS_PROXY` or `.netrc`. A 3xx is refused, never followed.
- **No long waits**: a rate limit is reported at once, never slept through. A gateway error (502, 503, 504) is retried once after 1 s.

## Failures

Each source either returns complete data or fails with a short, fixed message (never an upstream body or the token). In a `collect` step with other sources, a failing source makes the step and the run **degraded**: the pipeline continues on the partial data, `act` steps are skipped, `notify` still runs. So when one kind fails (say mentions are rate limited), the other kinds' items are still there and the failure is flagged on the run.

| Message | Cause |
|---|---|
| `github: GITHUB_TOKEN is not set` | the secret is missing |
| `github: authentication failed (GITHUB_TOKEN refused)` | HTTP 401: token wrong, expired or revoked |
| `github: access denied (HTTP 403)` | the token lacks a permission or the organisation refuses it |
| `github: rate limited; retry after N s` | HTTP 403/429 with `Retry-After`, or `X-RateLimit-Remaining: 0` with `X-RateLimit-Reset`, or a GraphQL `RATE_LIMITED` error |
| `github: query failed (FORBIDDEN)` | a GraphQL error (for example an organisation enforcing SAML single sign-on on the token) |
| `github: service unavailable (HTTP 503)` | a gateway error, twice |
| `github: request timed out`, `github: connection failed (...)`, `github: response too large`, `github: malformed response`, `github: unexpected ...` | network or protocol trouble |
| `github: invalid org filter`, `github: max_items is 1 to 200`, `github: due offsets are 0 to 365 days`, `github: today must be YYYY-MM-DD`, `github: unknown time zone` | a bad pipeline parameter |

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
