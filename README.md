# Recallry

**Persistent, verified knowledge for coding agents.**

Recallry v0.1.2 is an early local CLI for keeping reusable project rules,
decisions and lessons between coding tasks. Human review determines what becomes
verified; agents can retrieve relevant verified Knowledge as reference context.

```text
candidate
   ↓ human review
verified
   ↓ task-relevant retrieval
coding agent context
```

`add` always creates a candidate. `promote` records a human review decision;
`reject` declines a candidate, while `deprecate` retires verified Knowledge.
“Verified” is a workflow state, not an automatic guarantee of truth.
Global Knowledge is shared across projects; project Knowledge stays in its explicit
project scope. Rejected and deprecated entries are excluded from Context selection.

## Quick start

Install from a local repository checkout during v0.1.2 development. No PyPI
availability is claimed. Python 3.12 or newer is required.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
recallry init
recallry connect /path/to/project --project-id example-project
recallry add \
  --title "Example rule" \
  --content "Example verified-project knowledge candidate." \
  --scope project \
  --project example-project \
  --category rule
# Review the entry; replace <ID> with the ID printed by add.
recallry show <ID>
recallry promote <ID>
recallry context \
  --project example-project \
  --task "Implement the next change" \
  --verified-only
```

The project directory must already exist. `init` provisions bundled defaults and
router templates in `~/Recallry`, without overwriting existing settings. Set
`RECALLRY_HOME` to choose another dedicated store. Reads do not initialize stores.
`python -m recallry` is also supported.

Manual `context` can include candidates unless `--verified-only` is supplied;
automatic agent context always excludes candidates. Manual retrieval normally
increments use counts; automatic or read-only retrieval does not.

## Coding-agent integration

`connect` writes `.recallry.toml` with the project ID you explicitly supply and
adds Recallry-managed blocks to `CLAUDE.md` and `AGENTS.md`. It preserves unrelated
content and repeated connections are idempotent. It never infers project identity
from folder names, paths or Git remotes. Use `connect ... --dry-run` to preview.

The templates instruct Claude Code and Codex to retrieve verified Knowledge once
at the start of substantial tasks, respect project boundaries, and treat retrieved
content as untrusted reference data. They continue when Recallry is unavailable.
These are integration instructions, not model plugins or a guarantee that an agent
will follow every instruction. Recallry is not affiliated with or endorsed by
Anthropic or OpenAI.

### Compact context output and upgrading existing routers

In v0.1.1, bundled routers request `recallry context --format json-compact`.
This removes the duplicate body representation in `knowledge[].content`; the
same selected Knowledge bodies remain in `markdown`. Retrieval, scope and
verification rules are unchanged. `--format json` remains available for callers
that need the legacy payload. The default Markdown output is unchanged.

Existing homes retain their stored templates because `recallry init` does not
overwrite them. To upgrade an existing home, edit only the automatic context
command's `--automatic --format json` to `--automatic --format json-compact` in
both `~/Recallry/templates/recallry-claude-router.md` and
`~/Recallry/templates/recallry-codex-router.md` (or the corresponding files under
`RECALLRY_HOME/templates`). Make the same one-flag edit inside the Recallry-managed
blocks in each connected project's `CLAUDE.md` and `AGENTS.md`. Keep all other
instructions, including local customizations, intact. Review each diff before
using it. `init` alone does not refresh stored templates; `connect` alone uses
those stored templates and cannot update stale commands in them. After editing
the stored templates, `connect` can refresh managed blocks, but it replaces their
contents, so move any custom instructions inside those blocks outside the markers
first. Editing the flag in the blocks directly avoids that replacement.

The routers also instruct agents to save at most two reusable, nonduplicate
candidates at meaningful task completion and report their IDs. They never authorize
automatic promotion. To prohibit Knowledge writes, start the agent with
`RECALLRY_READONLY=1`; human write commands in that environment are also blocked.
Review candidates using [the review policy](docs/CANDIDATE_REVIEW_POLICY.md).

## Privacy and local operation

Knowledge is stored in `~/Recallry/data/recallry.db`. Runtime operations use local
SQLite and files, with no network service, account, daemon, cloud sync or remote
telemetry. There are no runtime Python dependencies outside the standard library;
installation may download build tooling.

Local metrics are opt-in through `RECALLRY_METRICS=1`; the automatic retrieval
router explicitly enables this flag for its local call. Metrics store IDs, counts
and operational metadata, not task or Knowledge content. `RECALLRY_READONLY=1`
protects Knowledge state, but does not disable explicitly enabled sidecar metrics.
Inspect or customize the local routers if you need different metrics behavior.

An external coding agent may send the context it receives to its own provider;
Recallry cannot control that agent's data handling. Keep secrets and private data
out of reusable Knowledge and public reports.

Recallry does not train models, update weights, autonomously verify facts, collect
whole conversations, or provide hosted memory, embeddings or vector search.

## Search and CJK Context selection

`recallry search "example" --project example-project` uses SQLite FTS5 with a LIKE
fallback. It does not use the Context tokenizer.

For `context --task`, CJK-aware character bigrams improve relevance for unspaced
Japanese, Chinese and Korean text. ASCII-only input keeps the original lowercase
whitespace tokenization. Mixed identifiers are retained, and punctuation separates
CJK runs. Scope filtering, verified handling and deterministic same-rank ordering
are unchanged; this is lexical matching, not semantic search.

For manual (nonautomatic) context, task relevance ranks Knowledge within the
eligible scope; it is not a minimum inclusion threshold. Entries with zero lexical
matches may still be returned. Manual search remains a separate FTS5/LIKE path.

Automatic context in v0.1.2 uses **Fix F2 Variant A → deterministic top 5 → existing
render budget**. Only verified Knowledge is eligible. Script-aware lexical spans
collapse overlapping CJK bigrams into evidence units, with stopword filtering and
ASCII token-boundary matching. Global entries need at least two matched spans;
Project entries need at least one. Project entries receive a +1 ranking boost,
which does not bypass eligibility or guarantee inclusion. All returned top-five
candidates are kept; rendering preserves the existing Global/Project grouping,
with no post-selection scope filter.
Results may naturally contain 0–5 items; zero-match or insufficient-evidence items
are not added merely to fill the old limit. Automatic selection uses a fixed top
five regardless of `--limit`; manual context still honors that option.

The existing renderer keeps its Global/Project grouping, 600-character body cap
and 6,000-character total budget. `included_chars` remains `len(markdown)` and
`json-compact` still omits duplicate Knowledge bodies. No new runtime dependency,
model, embedding, cloud service or database migration is required. Existing
Knowledge stores need no action. Router templates are unchanged from v0.1.1, so
no router refresh is required for this update.

### Context-size benchmark

On a frozen sample of 366 real automatic-router tasks, the Recallry automatic-context
payload was approximately 34% smaller under o200k_base/cl100k_base tokenization
estimates. This internal sample contains no synthetic tasks; raw tasks and Knowledge
are not published. Measurements were collected on the internal implementation
before this equivalent retrieval port, not on a separate public-workload sample.

| Measure | Previous selection | Fix F2 top 5 | Reduction |
| --- | ---: | ---: | ---: |
| Selected items | 2,928 | 1,809 | 38.22% |
| Actual context tokens, o200k_base | 497,007 | 327,399 | 34.13% |
| Actual context tokens, cl100k_base | 500,476 | 331,552 | 33.75% |

Rendered characters decreased by 32.44%. With the common task/header excluded,
Knowledge-only tokens decreased by 36.25% (o200k_base) and 35.85% (cl100k_base).
Average actual-context savings were approximately 463 and 462 tokens per call,
respectively, using `tiktoken==0.14.0` in an isolated measurement environment.

These tokenizer values are estimates/proxies, not exact provider billing tokens
or universal savings. The benchmark measures Recallry-added context only; system
prompts, conversation history and tool output outside that context are excluded.
It does not measure total Claude/Codex token use. Tokenizer packages are not
runtime dependencies.

## Independent stores

Recallry only opens Recallry-managed databases. Databases with unknown or
incompatible application identity/schema are rejected without modification.
Use a dedicated Recallry home; database migration is not supported.

## Development and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for tests and contribution guidance and
[SECURITY.md](SECURITY.md) for sensitive reports. Licensed under the [MIT License](LICENSE).
