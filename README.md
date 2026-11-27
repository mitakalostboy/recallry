# Recallry

**Persistent, verified knowledge for coding agents.**

Recallry 2.0 is a local CLI for keeping project rules, decisions and lessons
between coding tasks. Claude Code and Codex can retrieve relevant verified
Knowledge through the existing lexical search. The new `learn` command checks
proposals against committed evidence. Manual `add` still creates a candidate;
operators retain the existing promote, reject and deprecate commands.

`add` always creates a candidate. `promote` records a human review decision;
`reject` declines a candidate, while `deprecate` retires verified Knowledge.
“Verified” is a workflow state, not an automatic guarantee of truth.
Global Knowledge is shared across projects; project Knowledge stays in its explicit
project scope. Rejected and deprecated entries are excluded from Context selection.

## Quick start

Install from the v2.0.0 GitHub release checkout. Python 3.12 or newer is required.

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

### Upgrading from 0.1.x

Install the new package into the same environment and keep your existing
`RECALLRY_HOME`. The public store identity, Knowledge schema and manual commands
are compatible. Upgrading the package does not enroll existing candidates in
unattended review. `recallry init` keeps existing settings and router templates.

To adopt evidence-gated learning in an existing project, compare the release's
two `templates/recallry-*-router.md` files with the copies under your store's
`templates/`. Merge the new learning instructions while keeping your own
restrictions. Preserve any local project instructions outside the managed blocks,
preview with `recallry connect <directory> --project-id <id> --dry-run`, and
then repeat without `--dry-run` to apply. Existing projects can continue using
their older candidate-only templates until refreshed. Automatic retrieval still
uses the verified-only `context --automatic --format json-compact` route.

### Evidence-gated approval

Use `recallry learn --project-root . --input proposal.json` to check a proposal.
Project scope comes from the connected project. Independent committed evidence
can verify a new, ordinary claim; human-review vetoes take precedence.
A model recommendation alone cannot verify a claim.
See the [candidate review policy](docs/CANDIDATE_REVIEW_POLICY.md) for the proposal format.

### Optional unattended AI review

External AI review is **off by default**, including after an upgrade. Ordinary
candidates may be enrolled only after explicit user consent. The supported
provider profile is Claude Code 2.1.303 on macOS using a first-party claude.ai
subscription. Codex remains supported as a coding agent for local retrieval
and learning. It is not an unattended review provider.

```sh
recallry review status    # Show this store's ON/OFF state, provider, threshold and consent
recallry review enable    # Read the disclosure in your terminal and type ENABLE to opt in
recallry review disable   # Turn off future unattended sends and revoke consent
```

`status` reads local settings. Enabling checks the Claude Code version and
subscription login and makes a local backup before saving consent. Revocation
works without a provider check or access to the backup destination. These
commands do not submit candidates for review.

The reviewed payload can include candidate IDs, titles, content, categories
and scope. The provider also receives the review instructions and normal request metadata. A review uses
the provider's subscription allowance. Revoking consent prevents subsequent
sends; it cannot recall content already sent. Only new eligible candidates
in the active consent period are considered. The default threshold is five.
External review never replaces independent local evidence or a human-review
veto. Do not copy consent or settings from another store or write consent
tokens into the configuration by hand.

The public 2.0.0 release was tested with synthetic candidates and a simulated
provider. A live review submission to Anthropic was not tested.

### Automatic backup

A successful Knowledge write triggers the local backup check. With default
settings, the next write after 24 hours records a verified SQLite snapshot.
The first write with no prior backup is eligible immediately. Backup failures
are reported separately from the committed write. See [backup and recovery](docs/BACKUPS.md).

## Privacy and local operation

Knowledge is stored in `~/Recallry/data/recallry.db`. Runtime operations use local
SQLite and files. Ordinary retrieval and evidence checking run locally.
Unattended external AI review is off by default and requires separate consent.
There is no Recallry-hosted service or remote telemetry. There are no runtime Python dependencies outside the standard library;
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

Automatic context uses **Fix F2 Variant A → deterministic top 5 → existing
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
Knowledge stores need no action. The retrieval contract from v0.1.2 remains in force.

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

### Task-level AI Brain ON/OFF pilot (2026-10-18)

A separate exploratory pilot compared the internal AI Brain implementation ON and
OFF across four repository-investigation questions (one pair per question, eight
completed v2 runs). The report records Claude Code / Opus 5.5 / HIGH. This was not
a benchmark of a particular public Recallry release, and it used no knowledge graph.

| Reported aggregate | AI Brain OFF | AI Brain ON | Change |
| --- | ---: | ---: | ---: |
| Correctness score | 2/2 on all four questions | 2/2 on all four questions | No difference |
| Mean weighted evidence recall | 0.952 | 0.994 | +0.042 |
| Cumulative task tokens, including cache categories | 8,975,671 | 7,072,993 | −21.2% |
| Estimated API-equivalent cost | $6.441 | $5.951 | −7.6% |
| Wall-clock time | 1,022 s | 978 s | −4.4% |
| Tool calls | 134 | 115 | −14.2% |

**Conclusion: IMPROVE, not a demonstrated repeatable benefit.** Q4 used 30.3% more
tokens with AI Brain. Q1-B also saw candidate titles through follow-up retrieval,
so the strict verified-only condition was not fully maintained. There was only one
pair per question, scoring was only partially blinded, and Q1's pair was separated
by a subscription-limit reset. Figures above are reported aggregates, not a promise
of savings or a statistical validation of Recallry. API-equivalent costs are not
additional subscription charges, and task-token totals include repeated cache reads.

Read the [full Japanese report and separate publication notes](docs/benchmarks/2026-10-18-ai-brain-on-off.md)
for per-question results, isolation checks, retrieval misses, and limitations.
Private project identifiers and Knowledge excerpts are redacted; source snapshots,
databases, answer keys and raw logs are not published, so full independent
reproduction is not possible from this report alone. This task-level comparison
is distinct from the context-size benchmark above.

## Independent stores

Recallry only opens Recallry-managed databases. Databases with unknown or
incompatible application identity/schema are rejected without modification.
Use a dedicated Recallry home; database migration is not supported.

## Development and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for tests and contribution guidance and
[SECURITY.md](SECURITY.md) for sensitive reports. Licensed under the [MIT License](LICENSE).
