"""
operations.py — Operation handlers for the LLM Wiki Hermes plugin.

The plugin is a disciplined storage layer. The agent's own LLM does the
reasoning (reading sources, writing pages, deciding what to cross-link).
This module:

- exposes primitive file I/O (read/write/list) scoped to the wiki tree,
- exposes the four high-level operations from SCHEMA.md (ingest, query,
  lint, reflect) as workflow initiators,
- wraps every commit in the quality gate from `gate.py`,
- maintains the git-backed audit trail.

Tool returns are STRINGS sent back to the agent. They contain both data
(file contents, manifests) and instruction (what SCHEMA.md asks the agent
to do next). This keeps the agent on the rails without the plugin having
to drive an inner LLM loop.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import re
import subprocess
import textwrap
from datetime import datetime, timezone
from typing import Any

try:
    import fcntl  # POSIX
except ImportError:
    fcntl = None  # type: ignore[assignment]

# The quality gate ships alongside this plugin. See SCHEMA.md §8 — every
# ingest's diff is reviewed before commit.
from . import gate

try:
    from tools.registry import tool_error  # type: ignore[import-not-found]
except ImportError:
    def tool_error(message: str, **extra: Any) -> str:
        payload = {"error": message}
        payload.update(extra)
        return json.dumps(payload)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WIKI_SUBDIRS = ("entities", "concepts", "sources", "syntheses", "questions")
SPECIAL_FILES = ("index.md", "log.md", "overview.md")
SPECIAL_PAGES = {f"wiki/{n}" for n in SPECIAL_FILES}
PAGE_TYPES = {"entity", "concept", "source", "synthesis", "question"}
WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
INGEST_COUNTER_FILE = ".ingest_count"


# ---------------------------------------------------------------------------
# Storage layout
# ---------------------------------------------------------------------------


class Store:
    """Paths and git operations for the wiki tree.

    Layout under `root`:
        root/
        ├── SCHEMA.md      # the schema (loaded from plugin dir on first init)
        ├── raw/           # immutable source files
        ├── wiki/          # agent-owned pages
        └── .git/          # version history
    """

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.raw = root / "raw"
        self.wiki = root / "wiki"
        self.schema = root / "SCHEMA.md"

    # ---- bootstrap -------------------------------------------------------

    def bootstrap(self, schema_source: pathlib.Path) -> None:
        """Create directories, copy in SCHEMA.md, init git, scaffold seeds."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.raw.mkdir(exist_ok=True)
        self.wiki.mkdir(exist_ok=True)
        for sub in WIKI_SUBDIRS:
            (self.wiki / sub).mkdir(exist_ok=True)

        if not self.schema.exists() and schema_source.exists():
            self.schema.write_text(schema_source.read_text(encoding="utf-8"),
                                   encoding="utf-8")

        # Seed the special files so the gate has something to reason about.
        for name, body in (
            ("index.md", _seed_index()),
            ("log.md", _seed_log()),
            ("overview.md", _seed_overview()),
        ):
            target = self.wiki / name
            if not target.exists():
                target.write_text(body, encoding="utf-8")

        # Identity page — SCHEMA.md §0 requires this.
        identity = self.wiki / "entities" / "hermes-agent.md"
        if not identity.exists():
            identity.write_text(_seed_identity(), encoding="utf-8")

        if not (self.root / ".git").exists():
            self._git("init", "-q")
            self._git("add", "-A")
            self._git("-c", "user.email=hermes@local",
                      "-c", "user.name=hermes",
                      "commit", "-q", "-m", "bootstrap")

    # ---- git -------------------------------------------------------------

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True, check=False,
        )

    def commit(self, message: str, *, paths: list[str] | None = None) -> str | None:
        if paths:
            self._git("add", "--", *paths)
        else:
            self._git("add", "-A")
        res = self._git("-c", "user.email=hermes@local",
                        "-c", "user.name=hermes",
                        "commit", "-q", "-m", message)
        if res.returncode != 0 and "nothing to commit" not in (res.stdout + res.stderr):
            return f"git commit failed: {res.stderr.strip()}"
        return None

    def revert_uncommitted(self) -> None:
        self._git("reset", "-q", "--hard", "HEAD")
        self._git("clean", "-qfd")

    def has_uncommitted_changes(self) -> bool:
        res = self._git("status", "--porcelain")
        return bool(res.stdout.strip())

    # ---- safe path resolution --------------------------------------------

    def resolve(self, rel: str, *, must_be_under: str = "") -> pathlib.Path:
        """Resolve a relative path, blocking traversal outside the wiki tree."""
        rel = rel.lstrip("/")
        target = (self.root / rel).resolve()
        anchor = (self.root / must_be_under).resolve() if must_be_under else self.root.resolve()
        try:
            target.relative_to(anchor)
        except ValueError:
            raise ValueError(f"path escapes {anchor}: {rel}")
        return target

    # ---- ingest counter --------------------------------------------------

    def bump_ingest_count(self) -> int:
        f = self.root / INGEST_COUNTER_FILE
        n = int(f.read_text().strip()) if f.exists() else 0
        n += 1
        f.write_text(str(n))
        return n


# ---------------------------------------------------------------------------
# Primitive tools — the agent uses these to read/write pages directly
# ---------------------------------------------------------------------------


def wiki_read(store: Store, args: dict[str, Any]) -> str:
    """Read a wiki page or raw source. Path is relative to the wiki root."""
    rel = args.get("path", "").strip()
    if not rel:
        return tool_error("path is required")
    try:
        target = store.resolve(rel)
    except ValueError as e:
        return tool_error(str(e))
    if not target.exists():
        return tool_error(f"not found: {rel}")
    if target.is_dir():
        return tool_error(f"is a directory, not a file: {rel}")
    return target.read_text(encoding="utf-8", errors="replace")


def wiki_write(store: Store, args: dict[str, Any]) -> str:
    """Write a wiki page. Refuses to write under raw/ (SCHEMA.md §1)."""
    rel = args.get("path", "").strip()
    content = args.get("content", "")
    if not rel or content == "":
        return tool_error("path and content are required")
    if rel.startswith("raw/") or rel == "raw" or "/raw/" in rel:
        return tool_error("raw/ is immutable. Use wiki_capture to add a raw source.")
    try:
        target = store.resolve(rel, must_be_under="wiki")
    except ValueError as e:
        return tool_error(str(e))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    rel_normalized = str(target.relative_to(store.root))
    warnings = _quick_frontmatter_check(rel_normalized, content)
    if warnings:
        return f"wrote {rel}\nWARNING: {warnings}"
    return f"wrote {rel}"


def wiki_list(store: Store, args: dict[str, Any]) -> str:
    """List pages under a prefix. Default: every wiki page."""
    prefix = args.get("prefix", "wiki").strip().lstrip("/") or "wiki"
    try:
        anchor = store.resolve(prefix)
    except ValueError as e:
        return tool_error(str(e))
    if not anchor.exists():
        return tool_error(f"not found: {prefix}")
    pages = sorted(
        str(p.relative_to(store.root))
        for p in anchor.rglob("*.md")
        if ".git" not in p.parts
    )
    return json.dumps(pages, indent=2) if pages else "(no pages)"


def wiki_log_append(store: Store, args: dict[str, Any]) -> str:
    """Append an entry to wiki/log.md. The agent uses this after every operation."""
    entry = args.get("entry", "").strip()
    if not entry:
        return tool_error("entry is required")
    log = store.wiki / "log.md"
    if not entry.startswith("## ["):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        entry = f"## [{ts}] note\n{entry}\n"
    if not entry.endswith("\n"):
        entry += "\n"
    with log.open("a", encoding="utf-8") as f:
        f.write("\n" + entry)
    return "log entry appended"


# ---------------------------------------------------------------------------
# wiki_capture — used by on_pre_compress to flush turn material into raw/
# ---------------------------------------------------------------------------


def wiki_capture(store: Store, args: dict[str, Any]) -> str:
    """Write a new file under raw/. Used by hooks (on_pre_compress) and the
    agent when a source needs to be saved for later ingest."""
    slug = (args.get("slug") or "").strip()
    content = args.get("content", "")
    kind = args.get("kind", "note")
    if not slug or not content:
        return tool_error("slug and content are required")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        return tool_error("slug must be lowercase ASCII + hyphens")
    today = datetime.now(timezone.utc).date().isoformat()
    path = store.raw / kind / f"{today}-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return tool_error(f"already exists: {path.relative_to(store.root)}")
    path.write_text(content, encoding="utf-8")
    store.commit(
        f"capture: {kind}/{slug}",
        paths=[str(path.relative_to(store.root))],
    )
    return f"captured raw/{kind}/{today}-{slug}.md"


# ---------------------------------------------------------------------------
# Operation initiators — return guidance + context, agent does the work
# ---------------------------------------------------------------------------


def wiki_ingest(store: Store, args: dict[str, Any]) -> str:
    """Load a raw source and instruct the agent to perform an INGEST."""
    rel = (args.get("source") or "").strip().lstrip("/")
    if not rel.startswith("raw/"):
        return tool_error("source must be a path under raw/")
    try:
        path = store.resolve(rel, must_be_under="raw")
    except ValueError as e:
        return tool_error(str(e))
    if not path.exists():
        return tool_error(f"not found: {rel}")

    if store.has_uncommitted_changes():
        return tool_error("wiki has uncommitted changes. Call wiki_commit or wiki_abort first.")

    slug = path.stem
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return tool_error(f"cannot read source: {e}")

    return textwrap.dedent(
        f"""\
        INGEST initiated for slug='{slug}' (source: {rel})

        Per SCHEMA.md §7.1, your steps are:

        1. Write wiki/sources/{slug}.md with frontmatter, summary, 5–15
           extracted claims, an Entities section, and any Open questions.
        2. For each entity and concept the source touches, create or update
           the matching wiki/entities/<slug>.md or wiki/concepts/<slug>.md.
           Cite this new source on every claim added.
        3. Apply contradiction handling (§6) when the source disagrees with
           an existing page. Do NOT silently overwrite.
        4. Update bidirectional links across every touched page.
        5. Update wiki/index.md with new pages and refreshed one-liners.
        6. When you are finished, call wiki_commit with slug='{slug}'.
           The quality gate runs at commit. If it rejects, your work is
           reverted and a question page is filed automatically.

        Budget: 5–20 wiki pages touched. If you find yourself touching more,
        you are over-extracting.

        ===== source content begins =====
        {content}
        ===== source content ends =====
        """
    )


def wiki_query(store: Store, args: dict[str, Any]) -> str:
    """Initiate a QUERY workflow. Returns the index + identity page so the
    agent has the navigation context SCHEMA.md §7.2 expects."""
    question = (args.get("question") or "").strip()
    if not question:
        return tool_error("question is required")

    index = (store.wiki / "index.md").read_text(encoding="utf-8")
    identity = (store.wiki / "entities" / "hermes-agent.md").read_text(encoding="utf-8")

    return textwrap.dedent(
        f"""\
        QUERY initiated.

        Per SCHEMA.md §7.2:
        1. Use the index below to identify candidate pages.
        2. Read those pages with wiki_read, follow [[wikilinks]] as needed.
        3. Answer with citations to wiki pages (not raw sources directly).
        4. If the synthesis is non-trivial, file a syntheses/<slug>.md page
           and call wiki_commit('synthesis-<slug>'). Trivial single-page
           lookups skip the file-back step.
        5. Append a log entry via wiki_log_append.

        Question: {question}

        ===== wiki/entities/hermes-agent.md =====
        {identity}

        ===== wiki/index.md =====
        {index}
        """
    )


def wiki_lint(store: Store, args: dict[str, Any]) -> str:
    """Initiate a LINT pass. Returns inventories the agent needs to reason
    over — orphans, missing pages, stale claims."""
    if store.has_uncommitted_changes():
        return tool_error("wiki has uncommitted changes. Resolve before linting.")

    inventory = _lint_inventory(store)
    return textwrap.dedent(
        f"""\
        LINT initiated. Per SCHEMA.md §7.3, work through the checks in order
        and produce a single wiki/syntheses/lint-{datetime.now(timezone.utc).date().isoformat()}.md
        report. Use wiki_write to fix issues, wiki_log_append for the entry,
        then call wiki_commit('lint-{datetime.now(timezone.utc).date().isoformat()}').

        Inventory (deterministic checks already run):
        {json.dumps(inventory, indent=2)}
        """
    )


def wiki_reflect(store: Store, args: dict[str, Any]) -> str:
    """Initiate a REFLECT pass. Returns the recent log."""
    log = (store.wiki / "log.md").read_text(encoding="utf-8")
    # last ~7 days of entries by header parsing
    recent = _tail_log(log, days=7)
    return textwrap.dedent(
        f"""\
        REFLECT initiated. Per SCHEMA.md §7.4, produce
        wiki/syntheses/reflection-{datetime.now(timezone.utc).date().isoformat()}.md.

        Recent log (last 7 days):
        {recent}
        """
    )


# ---------------------------------------------------------------------------
# Commit + gate
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _wiki_lock(store: Store):
    """Exclusive lock around the wiki tree for the duration of a commit.

    Two concurrent commits would race on `git status` / `git commit` and
    produce torn or interleaved history. POSIX `fcntl.flock` blocks across
    threads in this process AND across processes sharing the same wiki dir;
    on platforms without fcntl (Windows) we fall back to a no-op.
    """
    lock_path = store.root / ".gate.lock"
    if fcntl is None:
        yield
        return
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def wiki_commit(store: Store, args: dict[str, Any], llm_call) -> str:
    """Run the quality gate on the current uncommitted diff. Approve →
    commit. Reject → revert + file question. Approve-with-edits → apply
    edits, commit."""
    slug = (args.get("slug") or "").strip()
    if not slug:
        return tool_error("slug is required (used in commit message and rejection records)")

    with _wiki_lock(store):
        if not store.has_uncommitted_changes():
            return "no changes to commit"

        schema_md = store.schema if store.schema.exists() else None
        if schema_md is None:
            return tool_error("SCHEMA.md missing — bootstrap incomplete")

        verdict = gate.run(
            wiki_root=store.root,
            raw_root=store.raw,
            hermes_md=schema_md,
            ingest_slug=slug,
            llm_call=llm_call,
        )

        n = store.bump_ingest_count() if verdict.decision != "REJECT" else None
        body = json.dumps(verdict.to_dict(), indent=2)
        suffix = f"\n\nIngest count: {n}" if n is not None else ""
        return f"verdict: {verdict.decision}\n{body}{suffix}"


def wiki_abort(store: Store, args: dict[str, Any]) -> str:
    """Discard uncommitted changes. Used when an agent decides mid-ingest
    that the source isn't worth processing, or to recover from confusion."""
    if not store.has_uncommitted_changes():
        return "no changes to abort"
    store.revert_uncommitted()
    return "aborted: uncommitted changes discarded"


# ---------------------------------------------------------------------------
# Prefetch & on_pre_compress helpers
# ---------------------------------------------------------------------------


def build_prefetch(store: Store, query: str | None = None,
                   max_chars: int = 8000) -> str:
    """Context block injected into the system prompt every turn. Includes the
    identity page and (truncated) index. Optional query is matched against
    page slugs to surface relevant pages first."""
    parts = ["===== Hermes wiki memory ====="]

    identity_path = store.wiki / "entities" / "hermes-agent.md"
    if identity_path.exists():
        parts.append("--- entities/hermes-agent.md ---")
        parts.append(identity_path.read_text(encoding="utf-8"))

    index_path = store.wiki / "index.md"
    if index_path.exists():
        index = index_path.read_text(encoding="utf-8")
        if query:
            # bring lines mentioning the query terms to the top
            terms = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 3]
            lines = index.splitlines()
            scored = [(sum(1 for t in terms if t in ln.lower()), ln) for ln in lines]
            scored.sort(key=lambda x: -x[0])
            index = "\n".join(ln for _, ln in scored)
        parts.append("--- wiki/index.md ---")
        parts.append(index)

    blob = "\n".join(parts)
    if len(blob) > max_chars:
        blob = blob[:max_chars] + "\n[... truncated ...]"
    return blob


def flush_compression_to_raw(store: Store,
                             messages: list[dict[str, Any]]) -> None:
    """Save the user side of a window about to be compressed as a raw source.

    Why user-only: assistant turns are model output that can be regenerated
    from the same prompt; user turns are irrecoverable input.
    No auto-ingest — that would block compression.
    """
    user_turns = [m.get("content", "") for m in messages if m.get("role") == "user"]
    if not user_turns:
        return
    body = "\n\n---\n\n".join(t for t in user_turns if isinstance(t, str) and t.strip())
    if not body.strip():
        return
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    today = now.date().isoformat()
    slug = f"session-compress-{stamp}"
    path = store.raw / "sessions" / f"{today}-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# Session compression {stamp}\n\n"
        f"_Captured at compression. Pending ingest._\n\n{body}\n",
        encoding="utf-8",
    )
    store.commit(f"capture: sessions/{slug}", paths=[str(path.relative_to(store.root))])


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _quick_frontmatter_check(rel: str, content: str) -> str | None:
    """Cheap pre-commit feedback. Heavy validation is the gate's job.

    `rel` is wiki-relative (e.g. 'wiki/entities/foo.md').
    """
    if rel in SPECIAL_PAGES:
        return None
    m = FRONTMATTER.match(content)
    if not m:
        return "missing frontmatter"
    keys = set(re.findall(r"^(\w+):", m.group(1), re.MULTILINE))
    missing = {"type", "created", "updated", "status"} - keys
    if missing:
        return f"missing frontmatter keys: {sorted(missing)}"
    type_match = re.search(r"^type:\s*(\w+)", m.group(1), re.MULTILINE)
    if type_match and type_match.group(1) not in PAGE_TYPES:
        return f"invalid type '{type_match.group(1)}'"
    return None


_TOP_LEVEL_KEYS = {n[:-3] for n in SPECIAL_FILES}  # {"index", "log", "overview"}
_IDENTITY_KEY = "entities/hermes-agent"


def _lint_inventory(store: Store) -> dict[str, Any]:
    """Run cheap deterministic checks and return what the agent needs to fix.

    Wikilinks in pages drop the `wiki/` prefix (SCHEMA.md §3), so we key
    `existing` by the wiki-internal slug (`entities/foo`, not `wiki/entities/foo`).
    """
    pages = [p for p in store.wiki.rglob("*.md") if ".git" not in p.parts]
    existing = {str(p.relative_to(store.wiki).with_suffix("")) for p in pages}

    broken_links: list[dict[str, str]] = []
    inbound: dict[str, set[str]] = {p: set() for p in existing}
    missing_frontmatter: list[str] = []

    for p in pages:
        rel = str(p.relative_to(store.root))
        text = p.read_text(encoding="utf-8", errors="replace")
        if rel not in SPECIAL_PAGES and _quick_frontmatter_check(rel, text):
            missing_frontmatter.append(rel)
        page_key = str(p.relative_to(store.wiki).with_suffix(""))
        for link in WIKILINK.findall(text):
            target = link.split("|")[0].strip()
            if target not in existing:
                broken_links.append({"in": rel, "to": target})
            else:
                inbound[target].add(page_key)

    orphans = [
        p for p in existing
        if not inbound[p]
        and p not in _TOP_LEVEL_KEYS
        and p != _IDENTITY_KEY
    ]
    return {
        "page_count": len(existing),
        "broken_links": broken_links[:50],
        "orphans": orphans[:50],
        "missing_frontmatter": missing_frontmatter[:50],
    }


def _tail_log(log: str, days: int) -> str:
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    out: list[str] = []
    for block in log.split("\n## "):
        if not block.strip():
            continue
        block = block if block.startswith("## ") else "## " + block
        m = re.search(r"\[(\d{4}-\d{2}-\d{2})", block)
        if not m:
            continue
        try:
            ts = datetime.fromisoformat(m.group(1)).replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            continue
        if ts >= cutoff:
            out.append(block)
    return "\n".join(out[-50:])


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


def _seed_index() -> str:
    return textwrap.dedent("""\
        # Wiki index

        _The catalog. One line per page, grouped by type. Hermes reads this
        first on every query — keep it tight._

        ## Entities
        - [[entities/hermes-agent]] — This agent

        ## Concepts
        _(none yet)_

        ## Sources
        _(none yet)_

        ## Syntheses
        _(none yet)_

        ## Questions
        _(none yet)_
    """)


def _seed_log() -> str:
    return f"# Operation log\n\n## [{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}] bootstrap\n- wiki initialized\n"


def _seed_overview() -> str:
    return textwrap.dedent("""\
        # Overview

        _Top-level synthesis. Rewritten during lint, not during ingest._

        The wiki is empty. Once sources are ingested, this page will
        summarize what is well-covered, what is thin, and what the major
        themes are.
    """)


def _seed_identity() -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    return textwrap.dedent(f"""\
        ---
        type: entity
        created: {today}
        updated: {today}
        sources: []
        status: active
        confidence: high
        ---

        # Hermes Agent

        This page is Hermes's persistent self-model. It is read at the start
        of every session and updated only during lint.

        ## Capabilities
        _Updated by lint as new tools and integrations come online._

        ## Known failure modes
        _Updated by REFLECT when patterns emerge across sessions._

        ## Current goals
        _Set by the user. Hermes does not write to this section without
        an explicit instruction._
    """)
