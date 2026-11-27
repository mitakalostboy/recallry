# Candidate Review Policy

Operating policy for reviewing Recallry `candidate` Knowledge before promotion. Derived from the outcomes of an early real-world candidate review pass.

## Operating principle

Normal project development takes priority over continuous Recallry feature work:

```
normal project development
→ automatic candidate collection (router, max 2/task)
→ committed-evidence check or human review
→ verified knowledge reuse
```

Recallry exists to support project work, not the other way around. Do not let candidate review or Recallry tooling development crowd out project development.

## Review criteria

**A — Transient bug/status wording is not durable knowledge.**
Phrasing like "function X currently raises incorrectly," "feature Y is not implemented yet," or "as of checkpoint Z there are no callers" goes stale the moment the described state changes. Reject or merge these away. If the incident revealed a durable rule, verify that rule instead (see B).

**B — Preserve the contract, not the incident.**
When a bug or incident reveals a stable invariant, store the invariant/lesson, not the incident narrative. Keep both only if each is independently useful on its own.

**C — Global scope requires genuine generalization.**
Promote to `global` only when the content has no accidental project-specific assumptions, stays correct across unrelated projects, uses generalized wording, and offers reusable behavior or methodology. If the concrete rule depends on project-specific classes, reason codes, table names, API names, or strategy semantics, keep it `project`-scoped. When both a general principle and a concrete project rule are useful, store both — but avoid redundant double injection of the same content at two scopes.

**D — Uncommitted implementation evidence is not sufficient for verification.**
If a candidate's truth depends on currently uncommitted working-tree code, keep it as `candidate` until the implementation is committed, relevant tests pass, and no later review invalidates the rule. Specification-only decisions may still be verified when the spec is explicitly authoritative and stable — a temporary working-tree implementation is not that.

**E — Consolidate duplicates before verifying.**
Before promoting a candidate, search existing `verified`, `candidate`, `deprecated`, and `rejected` Knowledge for semantic overlap (`recallry search`, `recallry list`). If multiple items teach effectively the same lesson, merge into the strongest representation rather than promoting near-duplicates. Repeated review passes must not produce multiple injected versions of the same knowledge.

**F — Verification requires evidence.**
Normally require at least one strong evidence category: authoritative specification, committed implementation, regression test, independent review, official documentation, or repeated validation across projects. Required strength may vary by category (e.g. `bug`/`architecture` vs `lesson`). Do not build a numeric scoring framework for this.

## Review cadence

Review when pending candidates reach roughly **10**, or after a meaningful project checkpoint/milestone — whichever comes first. This is a practical trigger, not a hard invariant: don't review on every single new candidate, and don't let candidates accumulate indefinitely.

## Evidence-gated learning

`recallry learn --project-root . --input proposal.json` accepts a local proposal
with a title, claim text, category, scope, concrete usefulness and evidence.
For example, a project might have already committed this line to its specification:

```json
{
  "title": "Retry budget",
  "content": "Retry at most three times.",
  "category": "rule",
  "scope": "project",
  "usefulness": "Keep independent implementations within the agreed retry budget.",
  "evidence": [{
    "kind": "artifact",
    "path": "docs/retries.md",
    "sha256": "<SHA-256 of the whole committed file>",
    "quote": "Retry at most three times."
  }]
}
```

The CLI checks the working file, commit and exact claim. A recorded user decision
must also match the scope, project, category and content. Only a genuinely global
committed decision can verify a global claim. A new independent evidence event
can reconsider a matching candidate. Repeated evidence alone does not approve it.
Conflicts and sensitive claims remain for a person to review. Optional AI review
can advise on ordinary uncertain items. Manual `add` still creates a candidate.

## Signals for human reviewers

A candidate is **stronger** when it:
- has been confirmed more than once (independently arrived at the same principle),
- is backed by committed code/test evidence,
- is supported by authoritative spec/docs,
- does not conflict with existing verified knowledge,
- survived a prior review without reversal,
- has an unambiguous scope,
- is worded as a durable contract rather than a status report.

A candidate warrants **caution** when it:
- depends on uncommitted code,
- describes current temporary state,
- duplicates existing knowledge,
- came from a single speculative review pass,
- uses project-specific names while claiming global scope,
- may be invalidated by known upcoming work.

These are review heuristics for a human (or a future AI-assisted classification step) to weigh — not a scoring engine.
