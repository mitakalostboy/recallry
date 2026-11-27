# Recallry 2.0.0

The CLI keeps the existing public store and its lexical retrieval interface.
Both the Claude Code and Codex project routers can now propose a small number
of reusable claims with independent local evidence.

- `recallry learn` checks project scope, committed evidence and human-review
  vetoes. Existing manual lifecycle commands still work.
- Optional unattended review is disabled in a fresh or upgraded store.
  An external review can advise on newly enrolled, ordinary candidates only.
  The local evidence gate still controls promotion. There is no automatic
  rejection or deprecation.
- Use `recallry review status` to inspect the store, `recallry review enable`
  to read the disclosure and opt in, and `recallry review disable` to revoke
  consent. Enabling requires typing `ENABLE` in an interactive terminal.
  Revocation stops future sends. It cannot retract content already sent.
- Automatic SQLite backups run locally after committed Knowledge changes.
  The default interval is 24 hours with 30-day retention. No background
  service is needed.
- Upgrade without a database migration. Existing templates and project
  instructions are retained until the operator reviews and refreshes them.

Unattended review supports Claude Code 2.1.303 on macOS with a first-party
claude.ai subscription. The consent flow and review safeguards were tested
with synthetic data. A live review submission to Anthropic was not tested.

The MIT license and public package name are unchanged. The previously
published [pilot report](https://github.com/mitakalostboy/recallry/blob/v2.0.0/docs/benchmarks/2026-10-18-ai-brain-on-off.md) describes
a historical experiment; it does not establish task-level gains for this release.
