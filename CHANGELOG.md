# Changelog

## 0.5.0

- New source `github.updates`: a to-do of the issues and pull requests where someone else did
  something after your last action on them. One search per `orgs` / `users` / `repos` value,
  plus `involves:@me` anywhere (`involves_me`, default), `updated:>=` the window
  (`since_days`, default 14), open only unless `include_closed`; each item's last 25 timeline
  events decide: your last action (comment, review, commit, push, close, merge, review request,
  or opening it) against the latest event by someone else. Bots (type `Bot`, `[bot]` logins,
  commits whose author is no GitHub account) are left out unless `include_bots`; a review
  request counts only when it asks you. No plugin state. Data: repository, number, type,
  sanitised title, `event` (closed set), `by`, `at`, `your_last_action`, link rebuilt from
  validated parts, fingerprints. Parameters `orgs`, `users`, `repos`, `exclude_repos`,
  `involves_me`, `since_days`, `include_closed`, `include_bots`, `notify`, `max_items`,
  `today`, `timezone`.
- With `notify: true`, one `notify.push` per item keyed on the item and its latest event
  (the existing `notify.push` permission, no new one): the core pushes each event once.
- `GITHUB_TOKEN`, for this source: `Metadata`, `Issues`, `Pull requests` and `Contents`
  read-only (README, "The token"). No new permission, host or secret.
- Example pipeline `examples/updates.yaml`.

## 0.4.0

- New source `github.ci_failures`: which repositories have a failing CI. For each `orgs` /
  `users` owner, the default branch head of the owner's own repositories pushed in the last
  `pushed_within_days` (default 30; paging stops at the first older repository, at most 200
  per owner), plus `repos` read one by one and, with `include_my_prs` (default), your own open
  pull requests. A failure is a combined check state of `FAILURE` or `ERROR` (`PENDING` is
  not); the failing check runs and commit statuses are named. Archived repositories are never
  read, forks only with `include_forks`. Fingerprint: repository, ref and head SHA. Links are
  the workflow run page only when its URL is exactly this repository's
  `/actions/runs/<digits>`, else the commit or pull request page built from validated parts.
  Parameters `orgs`, `users`, `repos`, `exclude_repos`, `pushed_within_days`,
  `include_my_prs`, `include_forks`, `notify`, `max_items`, `today`, `timezone`.
- **New permission, intent `notify.push`** (to approve at install): with `notify: true`,
  `github.ci_failures` proposes one push per failure, `key` = its fingerprint, so the core
  sends each failing commit once (schemas decision 57, amended). Off by default: a pipeline
  without a `notify` entry would leave the pushes unrouted.
- `GITHUB_TOKEN` needs, for this source only, `Contents`, `Commit statuses` and `Actions`
  read-only on top of the existing permissions (README, "The token").
- Example pipeline `examples/ci-watch.yaml`: a note of the current failures
  (`obsidian.render_note`) and a push per new failure (plugin-ntfy).
- Internal: `Client.graphql` (one request, partial errors counted) and `Client.search` taking
  a query document and a shared deadline.

## 0.3.0

- **Installs with `horae plugin add`** (horae-dev/horae#199): `horae-sdk` now comes from PyPI
  (`>=0.9,<0.10`) instead of the core's `main` branch (the installer refuses a git source: no
  hash), and `exclude-newer` is gone from `pyproject.toml` (`horae plugin add` runs
  `uv sync --locked --no-config`, which ignores `[tool.uv]`, so the lock never matched).
  Renovate's `minimumReleaseAge` keeps the 7-day cooldown. No behaviour change; no SDK API
  change was needed between 0.2 and 0.9.

## 0.2.0

- **Optional secrets** (core decision 56): `GITHUB_NOTIFICATIONS_TOKEN` moves to
  `permissions.optional_secrets`, so no placeholder token is needed when `github.notifications`
  is unused; without it that source fails with `secret_missing: GITHUB_NOTIFICATIONS_TOKEN is
  not set`. `GITHUB_TOKEN` stays required. **Requires a core with decision 56** (unreleased
  0.0.x); the `horae:` range stays `>=0,<0.2`.
- New source `github.notifications` (#3): unread participating notifications
  (`GET /notifications?participating=true`, GET only, read state never modified) for pull
  requests and issues as `task.create` intents; `reasons` filter, `since`, `X-Poll-Interval`
  and rate-limit remaining reported, bounded pages under one deadline. New secret
  `GITHUB_NOTIFICATIONS_TOKEN` (classic token, `notifications` scope; fine-grained tokens cannot
  read notifications). Its fingerprints are the repository-and-number fallback (the API has
  no node id), so they do not collapse with the other sources'.

- Every task carries a link to its item (#2): the `task.create` intent now has `link`, the
  item's `https://github.com/<owner>/<repo>/pull|issues/<number>` URL (built from GitHub's
  repository name and the integer number, host fixed to github.com, never from its `url`
  field; also still in the data as `url`). An item whose link the SDK refuses (format, bidi or
  surrogate characters, `.`/`..` segments) is skipped and counted in `skipped`.
  Requires horae-sdk 0.2.0 and a core with decision 52 (horae main >= 0f5531a). **Upgrading:** a pipeline that routes
  these tasks to a `mode: tasks` entry must set `link_prefix` per owner (for example
  `https://github.com/<Owner>/`, canonical case, non-overlapping), or the step fails with `intent_unrouted`.

## 0.1.0 (unreleased)

- New sources `github.review_requests`, `github.assigned` and `github.mentions` (#2): open
  review requests, assignments and mentions as `task.create` intents, with due policies
  (`due_in_business_days`, `milestone_due`, `due_in_days`), filters (`orgs`, `users`, `repos`,
  `exclude_repos`), `max_items`, `today`, `timezone`, `skip_covered` (off by default) and `direct_only`. Each
  `orgs` / `users` / `repos` value is its own search (at most 8), merged; one 25 s deadline per
  call; `partial` data when GitHub hides results behind `FORBIDDEN` / `NOT_FOUND` errors.
- **Permissions** (new, to approve at install): intent `task.create`, network
  `api.github.com`, secret `GITHUB_TOKEN`.
