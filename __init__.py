"""
WikiMemoryProvider — Hermes Agent memory provider plugin.

Provides Karpathy-style LLM Wiki memory: a three-layer knowledge base
(immutable raw sources, agent-owned wiki pages, schema) sitting alongside
Hermes's built-in MEMORY.md / USER.md.

Activate with:
    hermes memory setup            # interactive picker
    # or:
    hermes config set memory.provider llm_wiki

The plugin exposes 9 tools to the LLM:
    wiki_read, wiki_write, wiki_list, wiki_log_append,  (primitives)
    wiki_capture,                                        (raw input)
    wiki_ingest, wiki_query, wiki_lint, wiki_reflect,    (operations)
    wiki_commit, wiki_abort                              (transactions)

It also injects context every turn (prefetch) and flushes pre-compression
material into raw/ (on_pre_compress).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess
import traceback
from typing import Any

from agent.memory_provider import MemoryProvider  # type: ignore[import-not-found]

from . import operations
from .operations import Store

logger = logging.getLogger(__name__)


# Path to the canonical SCHEMA.md shipped with the plugin. Copied into the
# wiki tree on first init so the user can edit it without touching the
# plugin source.
PLUGIN_DIR = pathlib.Path(__file__).resolve().parent
SCHEMA_SOURCE = PLUGIN_DIR / "SCHEMA.md"


class WikiMemoryProvider(MemoryProvider):
    """Memory provider implementing the LLM Wiki pattern."""

    # ------------------------------------------------------------------
    # Identity & availability
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "llm_wiki"

    def is_available(self) -> bool:
        # Local-only. Required only that `git` is on PATH; we tolerate its
        # absence in initialize() but lose audit trail. Cheap check, no I/O.
        return True

    def system_prompt_block(self) -> str:
        return (
            "# LLM Wiki memory\n"
            "Active. A persistent, agent-owned knowledge base under "
            "`wiki/` with immutable raw sources under `raw/`.\n"
            "- Navigate with wiki_read/wiki_list (start at wiki/index.md).\n"
            "- Add a new source via wiki_capture, then wiki_ingest.\n"
            "- Every wiki_commit runs the quality gate; rejections revert.\n"
            "- See SCHEMA.md for page types, link conventions, and the "
            "ingest/query/lint/reflect operations."
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        hermes_home = pathlib.Path(
            kwargs.get("hermes_home")
            or os.environ.get("HERMES_HOME")
            or os.path.expanduser("~/.hermes")
        )
        config = self._load_config(hermes_home)

        wiki_root = pathlib.Path(
            config.get("wiki_dir") or hermes_home / "llm_wiki_data"
        )
        self._store = Store(wiki_root)
        self._store.bootstrap(schema_source=SCHEMA_SOURCE)

        self._session_id = session_id
        self._validator_model = config.get("validator_model") or ""
        raw = config.get("lint_every_n_ingests")
        self._lint_every_n = int(raw) if raw not in (None, "") else 50

        # The validator model client is supplied by the host app. If unset,
        # the gate runs deterministic-only (still useful, just less thorough).
        self._llm_call = self._build_validator_call()

    def shutdown(self) -> None:
        # Nothing to flush — every wiki write is durable on disk.
        pass

    def _load_config(self, hermes_home: pathlib.Path) -> dict:
        cfg_file = hermes_home / "llm_wiki.json"
        if cfg_file.exists():
            try:
                return json.loads(cfg_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def _build_validator_call(self):
        """Return a callable(prompt: str) -> str for gate.llm_review.

        Hermes does not currently expose a stable public client for invoking
        a different model from a plugin, so out of the box the gate runs
        deterministic-only. Users who want LLM review can override this
        method (subclass) or set `validator_model` once Hermes ships a
        public model-call API; today, leaving it unset is the supported path.
        """
        if self._validator_model:
            logger.warning(
                "llm_wiki: validator_model=%r configured but no public "
                "Hermes model-call API is available; running deterministic-"
                "only. Subclass _build_validator_call to wire your own.",
                self._validator_model,
            )
        return lambda prompt: '{"findings": []}'

    # ------------------------------------------------------------------
    # Configuration schema (hermes memory setup reads this)
    # ------------------------------------------------------------------

    def get_config_schema(self) -> list[dict]:
        return [
            {
                "key": "wiki_dir",
                "description": "Override wiki storage root.",
                "default": "",
                "required": False,
            },
            {
                "key": "validator_model",
                "description": (
                    "Model name for the quality gate's LLM review. Should "
                    "differ from the agent's primary model so failure modes "
                    "decorrelate. Leave empty for deterministic-only checks."
                ),
                "default": "",
                "required": False,
            },
            {
                "key": "lint_every_n_ingests",
                "description": "Auto-suggest lint after this many ingests. 0 disables.",
                "default": "50",
                "required": False,
            },
        ]

    def save_config(self, values: dict, hermes_home: str) -> None:
        path = pathlib.Path(hermes_home) / "llm_wiki.json"
        path.write_text(json.dumps(values, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # Tool surface — what the LLM sees
    # ------------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return _TOOL_SCHEMAS

    def handle_tool_call(self, name: str, args: dict[str, Any], **kwargs: Any) -> str:
        try:
            handler = _TOOL_HANDLERS.get(name)
            if handler is None:
                return json.dumps({"error": f"unknown tool: {name}"})

            # wiki_commit needs the validator client; everything else is pure store ops.
            if name == "wiki_commit":
                result = operations.wiki_commit(self._store, args, self._llm_call)
            else:
                result = handler(self._store, args)

            # Auto-suggest lint when the ingest counter rolls over.
            if name == "wiki_commit" and self._lint_every_n:
                count = self._read_ingest_count()
                if count and count % self._lint_every_n == 0:
                    result += (
                        f"\n\nNOTE: {count} ingests since last lint. "
                        f"Consider calling wiki_lint."
                    )
            return result
        except Exception as e:
            return json.dumps({
                "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc(limit=3),
            })

    # ------------------------------------------------------------------
    # Prefetch — context auto-injected every turn
    # ------------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        try:
            return operations.build_prefetch(self._store, query=query)
        except (OSError, subprocess.CalledProcessError) as e:
            logger.debug("llm_wiki prefetch failed: %s", e)
            return ""

    # ------------------------------------------------------------------
    # Hooks — pre-compression
    # ------------------------------------------------------------------

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        try:
            operations.flush_compression_to_raw(self._store, messages)
        except Exception as e:
            logger.debug("llm_wiki on_pre_compress failed: %s", e)
        return ""

    def on_session_end(self, history: list[dict[str, Any]]) -> None:
        # Could optionally capture the full transcript on session end. We
        # leave that to the user — sessions are noisy by default.
        return

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _read_ingest_count(self) -> int:
        f = self._store.root / operations.INGEST_COUNTER_FILE
        try:
            return int(f.read_text().strip()) if f.exists() else 0
        except (ValueError, OSError):
            return 0


# ----------------------------------------------------------------------
# Tool schemas — JSON shapes the LLM sees
# ----------------------------------------------------------------------


_TOOL_HANDLERS = {
    "wiki_read":       operations.wiki_read,
    "wiki_write":      operations.wiki_write,
    "wiki_list":       operations.wiki_list,
    "wiki_log_append": operations.wiki_log_append,
    "wiki_capture":    operations.wiki_capture,
    "wiki_ingest":     operations.wiki_ingest,
    "wiki_query":      operations.wiki_query,
    "wiki_lint":       operations.wiki_lint,
    "wiki_reflect":    operations.wiki_reflect,
    "wiki_commit":     None,  # special-cased in handle_tool_call
    "wiki_abort":      operations.wiki_abort,
}


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


_TOOL_SCHEMAS = [
    _schema(
        "wiki_read",
        "Read a wiki page or raw source. Use this to navigate the wiki — "
        "start from wiki/index.md, follow [[wikilinks]] from there. Path is "
        "relative to the wiki root, e.g. 'wiki/index.md' or 'raw/foo.md'.",
        {"path": {"type": "string", "description": "Wiki-relative path."}},
        ["path"],
    ),
    _schema(
        "wiki_write",
        "Write or overwrite a page in wiki/. Refuses paths under raw/ "
        "(immutable). Returns inline frontmatter warnings when applicable.",
        {
            "path": {"type": "string"},
            "content": {"type": "string", "description": "Full page content including frontmatter."},
        },
        ["path", "content"],
    ),
    _schema(
        "wiki_list",
        "List all .md pages under a prefix. Default lists wiki/. Useful for "
        "discovering what entities or concepts already have pages before creating duplicates.",
        {"prefix": {"type": "string", "description": "Optional prefix, e.g. 'wiki/entities'."}},
        [],
    ),
    _schema(
        "wiki_log_append",
        "Append an entry to wiki/log.md. The agent calls this after every operation.",
        {"entry": {"type": "string", "description": "Markdown for the log entry. Should start with '## [YYYY-MM-DD HH:MM] <op> | <slug>'."}},
        ["entry"],
    ),
    _schema(
        "wiki_capture",
        "Save new content under raw/<kind>/<date>-<slug>.md as an immutable source. "
        "Use when the user pastes material that should be ingested later, or to record "
        "external observations. The file is committed but NOT ingested — call wiki_ingest for that.",
        {
            "slug": {"type": "string", "description": "Lowercase ASCII + hyphens."},
            "content": {"type": "string"},
            "kind": {"type": "string", "description": "Subdirectory under raw/, e.g. 'articles', 'sessions', 'notes'.", "default": "notes"},
        },
        ["slug", "content"],
    ),
    _schema(
        "wiki_ingest",
        "Begin an INGEST workflow for a raw source. Returns the source content "
        "and the schema-prescribed steps. The agent then uses wiki_read/wiki_write "
        "to produce the diff, and finishes with wiki_commit. The quality gate "
        "runs at commit time.",
        {"source": {"type": "string", "description": "Path under raw/, e.g. 'raw/articles/2026-04-27-foo.md'."}},
        ["source"],
    ),
    _schema(
        "wiki_query",
        "Begin a QUERY workflow. Returns the current index and identity page. "
        "The agent reads candidate pages, answers with citations, and (for "
        "non-trivial syntheses) files the answer back via wiki_write + wiki_commit.",
        {"question": {"type": "string"}},
        ["question"],
    ),
    _schema(
        "wiki_lint",
        "Begin a LINT pass. Returns a deterministic inventory of broken "
        "links, orphan pages, and frontmatter issues for the agent to address.",
        {},
        [],
    ),
    _schema(
        "wiki_reflect",
        "Begin a REFLECT pass over recent log entries. Used weekly or on demand.",
        {},
        [],
    ),
    _schema(
        "wiki_commit",
        "Finalize the current uncommitted diff. Runs the quality gate. "
        "On approval, commits with the given slug. On rejection, reverts and "
        "files a question page describing the failure. Call this at the end of "
        "every ingest, lint, reflect, or non-trivial query.",
        {"slug": {"type": "string", "description": "Identifier for the commit (e.g. ingest slug)."}},
        ["slug"],
    ),
    _schema(
        "wiki_abort",
        "Discard uncommitted changes. Use when an ingest goes off the rails "
        "and you'd rather restart than commit a flawed diff.",
        {},
        [],
    ),
]


# ----------------------------------------------------------------------
# Hermes plugin entry point
# ----------------------------------------------------------------------


def register(ctx) -> None:
    """Register llm_wiki as a memory provider plugin."""
    ctx.register_memory_provider(WikiMemoryProvider())
