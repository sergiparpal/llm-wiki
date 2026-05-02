# llm_wiki — Karpathy-style LLM Wiki memory for Hermes Agent

A memory provider plugin for [Hermes Agent](https://hermes-agent.nousresearch.com/)
that gives the agent a persistent, self-maintained knowledge base instead
of a flat note pile. Implements [Andrej Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
with autonomous-agent guardrails and a quality gate on every ingest.

## What this is, briefly

Three layers under `$HERMES_HOME/llm_wiki_data/`:

```
llm_wiki_data/
├── SCHEMA.md       schema (the agent's operational contract)
├── raw/            immutable source material (agent reads, never writes)
└── wiki/           agent-owned pages — entities, concepts, sources,
                    syntheses, questions; an index, a log, an overview
```

Four operations the agent invokes (defined in `SCHEMA.md`):

- **INGEST** — process a new raw source into wiki pages, with
  cross-references and contradiction handling.
- **QUERY** — answer a question against the wiki, file the answer back if
  the synthesis is non-trivial.
- **LINT** — periodic health check: orphans, broken links, stale claims.
- **REFLECT** — weekly meta-pass; surfaces patterns and proposes schema changes.

Every commit passes through a quality gate (`gate.py`) that runs
deterministic checks plus an LLM review by a *different* model, then
either commits, edits, or reverts the diff.

## Install

```bash
mkdir -p ~/.hermes/plugins
cp -r llm_wiki ~/.hermes/plugins/llm_wiki
hermes memory setup     # pick "llm_wiki"
```

Or manually:

```bash
hermes config set memory.provider llm_wiki
```

The plugin is stdlib + `git`. No pip dependencies. `git` should be on
`PATH`; if it isn't, the plugin still works but you lose the audit trail.

## Configure

`hermes memory setup` walks you through these. Settings live in
`~/.hermes/llm_wiki.json`:

| Key | Default | Notes |
| --- | --- | --- |
| `wiki_dir` | `$HERMES_HOME/llm_wiki_data` | Override storage root. |
| `validator_model` | `""` | Model used by the quality gate. **Should differ from the agent's primary model** so failure modes decorrelate. Empty = deterministic checks only. |
| `lint_every_n_ingests` | `50` | Auto-suggest a lint pass every N ingests. `0` disables. |

Recommended: if your agent runs Claude Sonnet 4.6, set `validator_model` to
a different family (e.g. a Hermes finetune, Gemini, or a small Anthropic
model). Same model checking itself is the most common gate failure.

## How the agent uses it

The plugin exposes 11 tools. Most are primitives; the four operation
tools are the ones that drive the workflow.

```text
wiki_read(path)          read any wiki page or raw source
wiki_write(path, content) write a wiki page (raw/ is refused)
wiki_list(prefix?)       list pages
wiki_log_append(entry)   append to wiki/log.md
wiki_capture(slug, content, kind?)
                         add a new immutable raw/ source

wiki_ingest(source)      begin INGEST workflow for a raw/ file
wiki_query(question)     begin QUERY workflow with index + identity
wiki_lint()              begin LINT pass with deterministic inventory
wiki_reflect()           begin REFLECT pass over recent log

wiki_commit(slug)        run gate, then commit / edit / revert
wiki_abort()             discard uncommitted changes
```

Typical ingest flow (the agent does this on its own):

1. `wiki_ingest({"source": "raw/articles/2026-04-27-foo.md"})` — returns
   the source content + the schema steps.
2. `wiki_read({"path": "wiki/index.md"})` — to find related pages.
3. `wiki_read({"path": "wiki/entities/karpathy.md"})` — read what's there.
4. `wiki_write(...)` for the new source page, updated entity pages, etc.
5. `wiki_log_append(...)` — record what was touched.
6. `wiki_commit({"slug": "2026-04-27-foo"})` — gate runs. On approval the
   diff is committed; on rejection it's reverted and a question is filed.

Typical query flow:

1. `wiki_query({"question": "How does Karpathy distinguish wiki from RAG?"})`
2. Agent reads candidate pages, follows wikilinks.
3. If the answer is non-trivial, agent writes
   `wiki/syntheses/karpathy-on-rag.md` and calls
   `wiki_commit({"slug": "synthesis-karpathy-on-rag"})`.

You don't need to memorize this — `SCHEMA.md` (loaded as part of the
prefetch context, and copied into the wiki on bootstrap) walks the agent
through it.

## Hooks

The plugin participates in two Hermes lifecycle events:

- **`prefetch`** — every turn, the agent's identity page
  (`wiki/entities/hermes-agent.md`) and `wiki/index.md` are injected as
  context. Truncated to 8KB; pages matching the user's query are surfaced
  first.
- **`on_pre_compress`** — before the conversation window is compressed, the
  user side of the discarded messages is captured to
  `raw/sessions/<timestamp>-session-compress.md`. The plugin does **not**
  auto-ingest from here — that would block compression. Trigger an
  ingest manually with `wiki_ingest("raw/sessions/...")` when you want
  the material absorbed.

## What the quality gate checks

`gate.py` runs after every `wiki_commit`. Cheap deterministic checks
first (frontmatter validity, link integrity, growth budget, citation
coverage), then — only if those pass — an LLM review by the configured
`validator_model` looking for:

1. **Claim grounding.** Every new claim must be supported by the cited
   source. Hallucinations are the #1 thing the gate exists to catch.
2. **Contradiction handling.** Did the ingest apply SCHEMA.md §6, or did
   it silently overwrite older claims?
3. **Proportionality.** Are the extracted claims actually in the source,
   or did the agent over-extract?
4. **Schema drift.** Did the agent invent a new convention not in
   SCHEMA.md? (This should never happen silently — REFLECT is the
   sanctioned channel for schema changes.)

Verdicts: `APPROVE` (commit), `APPROVE_WITH_EDITS` (apply edits, then
commit), `REJECT` (revert, file question, commit the rejection record).

## File layout

```
llm_wiki/
├── plugin.yaml      Hermes plugin manifest
├── __init__.py      WikiMemoryProvider class + tool schemas
├── operations.py    Operation handlers (Store + ingest/query/lint/...)
├── gate.py          Quality gate validator pass
├── SCHEMA.md        Schema — copied into the wiki on first run
└── README.md        This file
```

## Operational notes

**Backups.** The wiki tree is a git repo. `git -C $HERMES_HOME/llm_wiki_data log`
gives you the full audit trail. `git push` to a remote for backup.

**Recovery from a bad ingest.** The gate auto-reverts on rejection. For
a manually-triggered rollback: `git -C $HERMES_HOME/llm_wiki_data revert <sha>`.

**Schema evolution.** Don't edit `SCHEMA.md` mid-session — the agent
loaded the old version. Edit, then start a new session.

**Disable the LLM gate.** Set `validator_model: ""` in
`~/.hermes/llm_wiki.json`. You keep the deterministic checks; you lose
hallucination detection. Useful while iterating on the schema.

**Multi-profile.** Each Hermes profile has its own `$HERMES_HOME`, so
each gets its own wiki automatically. They don't share state.

## Limitations

- No vector search. Index-driven navigation works to ~hundreds of pages;
  beyond that you'll want to add a search layer (Karpathy's gist
  recommends [qmd](https://github.com/tobi/qmd)).
- No image handling. Sources can reference images in `raw/assets/`, but
  the agent reads markdown text only.
- Single-writer assumption. The wiki is one git working tree; concurrent
  Hermes sessions on the same profile will conflict. Use separate
  profiles for parallel work.

## License

MIT.
