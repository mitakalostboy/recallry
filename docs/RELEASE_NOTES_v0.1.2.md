# Recallry v0.1.2 — leaner automatic context retrieval

- Automatic agent context now uses deterministic Fix F2 Variant A: script-aware
  lexical evidence, eligibility checks, a +1 Project ranking boost, and top five.
  All eligible top-five candidates are retained; no post-selection scope filter
  is applied. Results may contain 0–5 items without padding weak matches.
- Verified-only automatic retrieval, manual search/context, compact JSON, and
  existing rendering budgets are preserved.
- No embeddings, models, cloud service, external API or runtime dependency added.
- On the frozen sample of 366 real internal router tasks, automatic-context
  payload estimates were 34.13% smaller with o200k_base and 33.75% smaller with
  cl100k_base (`tiktoken==0.14.0`). Knowledge-only reductions were 36.25% and
  35.85%. These measurements were collected on the internal implementation before
  this equivalent port; raw tasks and Knowledge are not published.
- These estimates cover only Recallry-added context, excluding other system
  prompts, conversation history and tool output. They are not exact billing-token
  counts, universal savings, or reductions in total Claude/Codex token use.
- No database migration or action on existing Knowledge stores is required.
- Router templates are unchanged from v0.1.1; no refresh is needed for this update.
  Users upgrading from older releases can follow the existing compact-JSON
  instructions in the README.
