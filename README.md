# Recallry

**Persistent, verified knowledge for coding agents.**

Recallry v0.1.0 is an early local CLI for keeping reusable project rules,
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

Install from a local repository checkout during v0.1.0 development. No PyPI
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

Task relevance ranks Knowledge within the eligible scope; it is not a minimum
inclusion threshold. Entries with zero lexical matches may still be returned.

## Independent stores

Recallry only opens Recallry-managed databases. Databases with unknown or
incompatible application identity/schema are rejected without modification.
Use a dedicated Recallry home; database migration is not supported.

## Development and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for tests and contribution guidance and
[SECURITY.md](SECURITY.md) for sensitive reports. Licensed under the [MIT License](LICENSE).
