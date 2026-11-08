# Changelog

## 0.2.0 (unreleased)

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
