# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **memory provider plugin for Hermes Agent** (NousResearch/hermes-agent) implementing Andrej Karpathy's "LLM Wiki" pattern. The agent reads/writes a markdown knowledge base on disk, and every commit passes a quality gate. Stdlib + `git` only — no pip dependencies in the runtime path.

Hermes itself lives at `/tmp/hermes-agent/` (cloned from `https://github.com/NousResearch/hermes-agent`) and is the source of truth for plugin contracts. When in doubt about how Hermes calls into the plugin, read the canonical reference files:

- `/tmp/hermes-agent/agent/memory_provider.py` — the `MemoryProvider` ABC (lifecycle, signatures).
- `/tmp/hermes-agent/agent/memory_manager.py` — orchestration around providers.
- `/tmp/hermes-agent/plugins/memory/__init__.py` — plugin discovery (scans `$HERMES_HOME/plugins/<name>/`, calls `register(ctx)`).
- `/tmp/hermes-agent/plugins/memory/honcho/__init__.py` — bundled provider used as the canonical reference for tool-schema shape, `register(ctx)`, threading patterns.
- `/tmp/hermes-agent/run_agent.py` — call sites: `prefetch`, `handle_tool_call`, `on_pre_compress`, tool-schema wrapping.

## Code layout (4 files)

- `__init__.py` — `WikiMemoryProvider(MemoryProvider)` class, `register(ctx)`, flat tool schemas, `_TOOL_HANDLERS` dispatch map. Lifecycle hooks: `initialize`, `prefetch`, `on_pre_compress`, `on_session_end`, `system_prompt_block`. Validator wiring is a stub by default — see "Validator" below.
- `operations.py` — `Store` class (paths, git, bootstrap, lock-aware commit) and the 11 tool handlers. Also: `build_prefetch`, `flush_compression_to_raw`, `_lint_inventory`, the `_wiki_lock` context manager (POSIX `fcntl.flock`).
- `gate.py` — quality gate. `staged_diff` reads `git status --porcelain=v1 -z`. Deterministic checks (`check_frontmatter`, `check_links`, `check_growth`, `check_citations`) run first; if any block-severity finding, LLM review (`llm_review`) is skipped. `run` is the entry point. Verdicts: `APPROVE`, `APPROVE_WITH_EDITS`, `REJECT`.
- `SCHEMA.md` — the agent's operational contract, copied into the wiki tree at `bootstrap` time. **The agent reads this at runtime; it is not just documentation.** Editing it changes runtime behavior.
- `plugin.yaml` — Hermes plugin manifest. Honcho-style: `name`, `version`, `description`, `pip_dependencies: []`, `hooks: [on_pre_compress]`. Do **not** add `entry_point` or a `config:` block — config comes from `get_config_schema()`.

## On-disk layout (the wiki tree, NOT the plugin tree)

`Store.root` is `$HERMES_HOME/llm_wiki_data/` (overridable via `wiki_dir` config). It contains:

```
SCHEMA.md      copied from the plugin dir on first run
raw/           immutable source files, agent reads only
  sessions/    pre-compression captures
  notes/, articles/, ...
wiki/          agent-owned pages
  index.md, log.md, overview.md     special files (frontmatter-exempt)
  entities/, concepts/, sources/, syntheses/, questions/
.git/          full audit trail (committed by Store.commit and gate.commit)
.ingest_count  counter for the auto-lint cadence
.gate.lock     fcntl lock acquired during wiki_commit
```

`Store.root` is the **git repo root**, not the `wiki/` subdir. This distinction matters for path keys — see "Path-prefix invariant" below.

## Path-prefix invariant (read before touching the gate or lint)

Two different key shapes appear and they MUST be kept straight:

1. **`diff` keys (gate)** are wiki-relative-to-`Store.root`: `wiki/entities/foo.md`, `raw/sessions/...md`. That's what `git status` emits.
2. **Wikilinks in pages** are bare slugs without the `wiki/` prefix per `SCHEMA.md §3`: `[[entities/foo]]`, `[[sources/karpathy-llm-wiki-gist]]`.

So `check_links` and `_lint_inventory` build their `existing` set keyed by `relative_to(store.wiki).with_suffix("")` (no `wiki/` prefix), while `check_frontmatter` / `check_citations` filter `diff.items()` with `path.startswith("wiki/...")`. If you mix these up, the gate silently approves garbage. The original review caught this in six places — the regression is easy to reintroduce.

## Quality gate flow

`wiki_commit` is the only entry point that runs the gate. It:

1. Acquires `_wiki_lock` (process- and cross-process-exclusive on POSIX).
2. Calls `gate.run` which builds the diff, runs deterministic checks, then LLM review (only if no block-severity findings yet).
3. On `APPROVE` → commit. On `REJECT` → `revert` (`git reset --hard HEAD` + `git clean -fd -- wiki/`, scoped so user notes outside `wiki/` survive), file a `questions/rejected-ingest-...md`, commit the rejection. On `APPROVE_WITH_EDITS` → apply every fix-severity edit; if `applied != expected`, downgrade to REJECT (no silent partial commits).

`Store.commit(message, paths=...)` stages only the listed paths when `paths` is given; with no paths it does `git add -A`. **`wiki_capture` and `flush_compression_to_raw` MUST pass `paths=` to avoid sweeping in-flight wiki edits into a capture commit.**

## Validator (LLM review)

`_build_validator_call` returns a stub by default (`{"findings": []}`). Hermes does not currently expose a stable public client for invoking a different model from a plugin, so out-of-the-box the gate runs deterministic-only. To wire real LLM review, subclass `WikiMemoryProvider` and override `_build_validator_call`.

Validator failures are non-blocking: `gate.llm_review` catches both `Exception` from the call and `JSONDecodeError` on the response, logs, and returns empty findings. **Do not regress this** — earlier versions blocked commits on validator outage and lost user work.

## Hermes contract (things that bite)

- **Tool schemas are flat**: `{"name", "description", "parameters": {...}}`. Hermes wraps them into OpenAI shape itself in `run_agent.py`. Do not double-wrap.
- **`register(ctx)` is the canonical entry**: `ctx.register_memory_provider(WikiMemoryProvider())`. Returning the class works only via the loader's class-walking fallback.
- **`prefetch(self, query, *, session_id="")`** — `session_id` is keyword-only. Hermes passes it; missing the kwarg crashes with `TypeError`.
- **`handle_tool_call(self, name, args, **kwargs)`** — `**kwargs` is part of the ABC contract even though current callers pass positionally.
- **`on_pre_compress(self, messages) -> str`**, NOT `on_compress` and NOT `(messages, count)`. Returns a string contributed to the compression summary prompt; we return `""`.
- **`hermes_home` is a string** in `initialize` kwargs (not a `Path`). Wrap with `pathlib.Path` at the call site.

## Common operations

```bash
# Syntax-check after edits (no test suite exists)
python3 -c "import ast; [ast.parse(open(f).read()) for f in ('__init__.py','operations.py','gate.py')]; print('OK')"

# Install the plugin into a Hermes profile
cp -r /home/sergi/llm-wiki ~/.hermes/plugins/llm_wiki

# Activate
hermes config set memory.provider llm_wiki
# or, interactively
hermes memory setup

# Inspect the wiki state directly
git -C ~/.hermes/llm_wiki_data log --oneline
git -C ~/.hermes/llm_wiki_data status
```

There is **no test suite**. Verification is end-to-end via Hermes — the smoke test path is in `~/.claude/plans/binary-knitting-lake.md` (the fix plan): install, load, dispatch, ingest+gate, compression, lint, concurrency stress.

## Reference docs in the repo

- `README.md` — install, configure, tool list, hooks, gate behavior, operational notes.
- `SCHEMA.md` — the agent's runtime contract; defines page taxonomy, slug rules, frontmatter, linking, contradiction handling, and the four operations (INGEST, QUERY, LINT, REFLECT). Read this before changing operation semantics — the agent reads it too.
- `Review - llm_wiki memory provider plugin.txt` — the original code review against actual Hermes source. Useful as the historical record of why specific shapes exist (path-prefix logic, signature changes, validator degradation policy).
- `llm-wiki Plugin — Fix Plan.txt` — the prior fix plan executed against the review. The plan document at `~/.claude/plans/binary-knitting-lake.md` is the executable version.
