# Changelog

## 0.1.0 (unreleased)

- New sources `github.review_requests`, `github.assigned` and `github.mentions` (#2): open
  review requests, assignments and mentions as `task.create` intents, with due policies
  (`due_in_business_days`, `milestone_due`, `due_in_days`), filters (`orgs`, `users`, `repos`,
  `exclude_repos`), `max_items`, `today`, `timezone` and `skip_covered`.
- **Permissions** (new, to approve at install): intent `task.create`, network
  `api.github.com`, secret `GITHUB_TOKEN`.
