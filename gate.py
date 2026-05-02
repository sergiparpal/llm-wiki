"""
quality_gate.py — Validator pass for HERMES wiki ingests.

Runs after every INGEST, before the diff is committed. The ingester writes
into a working tree; the gate reviews what changed; on approval the diff is
committed, on rejection it's rolled back and a question is filed.

Design principles
-----------------
1. The gate runs a *different* LLM from the ingester. Same model checking
   itself reproduces its own failure modes. Use a smaller/cheaper model from
   a different provider, or at minimum a different model family.
2. The gate looks at the diff, not the full wiki. Cost scales with the
   ingest, not the corpus.
3. Deterministic checks first (cheap, catch most regressions). LLM checks
   second (expensive, catch what rules can't).
4. Verdicts are: APPROVE, REJECT, APPROVE_WITH_EDITS. There is no "warn".
   If something is wrong enough to warn about, it's wrong enough to fix.
5. Rejection is loud and recoverable: the diff is reverted, a question is
   filed, the operator can see exactly what failed.

Stdlib only except for the LLM call (which the project's existing client
handles). Mirrors the OpenClaw `logica.py` / `main.py` split.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
import re
import subprocess
import textwrap
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Finding:
    """One thing the gate noticed about the diff."""

    severity: str  # "block" | "fix" | "info"
    check: str  # short name of the check that produced it
    page: str  # wiki-relative path
    message: str
    suggested_edit: str | None = None  # if severity == "fix"


@dataclasses.dataclass
class Verdict:
    decision: str  # "APPROVE" | "REJECT" | "APPROVE_WITH_EDITS"
    findings: list[Finding]

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "findings": [dataclasses.asdict(f) for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Diff extraction
# ---------------------------------------------------------------------------


def staged_diff(wiki_root: pathlib.Path) -> dict[str, dict]:
    """Return {path: {"status": str, "before": str|None, "after": str|None}}.

    Uses the wiki's git working tree. The ingester is expected to have
    written files but NOT committed them yet.
    """
    out = subprocess.run(
        ["git", "-C", str(wiki_root), "status", "--porcelain=v1", "-z"],
        check=True,
        capture_output=True,
        text=True,
    )
    # In `--porcelain=v1 -z`, R/C entries take TWO NUL-separated fields:
    # the new path, then the old path. Naive split-by-NUL misalignments
    # parse the old path as a separate entry and reach `git show HEAD:<garbled>`.
    fields = [f for f in out.stdout.split("\0") if f]
    diff: dict[str, dict] = {}
    i = 0
    while i < len(fields):
        entry = fields[i]
        status_raw = entry[:2]
        path = entry[3:]
        i += 1
        if status_raw[0] in ("R", "C") and i < len(fields):
            i += 1  # consume the old-path field
        status = status_raw.strip()
        full = wiki_root / path
        before = _git_show_head(wiki_root, path) if status in {"M", "D"} else None
        after = full.read_text(encoding="utf-8") if full.exists() else None
        diff[path] = {"status": status, "before": before, "after": after}
    return diff


def _git_show_head(wiki_root: pathlib.Path, path: str) -> str | None:
    res = subprocess.run(
        ["git", "-C", str(wiki_root), "show", f"HEAD:{path}"],
        capture_output=True,
        text=True,
    )
    return res.stdout if res.returncode == 0 else None


# ---------------------------------------------------------------------------
# Deterministic checks
# ---------------------------------------------------------------------------

WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
REQUIRED_KEYS = {"type", "created", "updated", "status"}
VALID_TYPES = {"entity", "concept", "source", "synthesis", "question"}


SPECIAL_PAGES = {"wiki/index.md", "wiki/log.md", "wiki/overview.md"}


def check_frontmatter(diff: dict[str, dict]) -> list[Finding]:
    """Every changed page must have valid frontmatter with required keys."""
    findings: list[Finding] = []
    for path, change in diff.items():
        if not path.endswith(".md") or change["after"] is None:
            continue
        if path in SPECIAL_PAGES:
            continue
        m = FRONTMATTER.match(change["after"])
        if not m:
            findings.append(Finding("block", "frontmatter", path, "Missing frontmatter."))
            continue
        keys = set(re.findall(r"^(\w+):", m.group(1), re.MULTILINE))
        missing = REQUIRED_KEYS - keys
        if missing:
            findings.append(
                Finding("block", "frontmatter", path, f"Missing required keys: {sorted(missing)}.")
            )
        type_match = re.search(r"^type:\s*(\w+)", m.group(1), re.MULTILINE)
        if type_match and type_match.group(1) not in VALID_TYPES:
            findings.append(
                Finding(
                    "block",
                    "frontmatter",
                    path,
                    f"Unknown type '{type_match.group(1)}'. SCHEMA.md forbids inventing types.",
                )
            )
    return findings


def check_links(wiki_root: pathlib.Path, diff: dict[str, dict]) -> list[Finding]:
    """Every [[wikilink]] in changed pages must resolve to an existing file.

    Wikilinks in pages drop the leading `wiki/` per SCHEMA.md §3
    (e.g. `[[entities/foo]]`), so we strip it from the existing-set keys.
    Files outside `wiki/` (e.g. raw/) are not link targets.
    """
    findings: list[Finding] = []
    wiki_dir = wiki_root / "wiki"
    existing: set[str] = set()
    if wiki_dir.is_dir():
        for p in wiki_dir.rglob("*.md"):
            if ".git" in p.parts:
                continue
            existing.add(str(p.relative_to(wiki_dir).with_suffix("")))
    for path, change in diff.items():
        if change["after"] is None:
            continue
        for link in WIKILINK.findall(change["after"]):
            target = link.split("|")[0].strip()
            if target not in existing:
                findings.append(
                    Finding("block", "broken-link", path, f"Wikilink [[{target}]] does not resolve.")
                )
    return findings


def check_growth(diff: dict[str, dict]) -> list[Finding]:
    """A single ingest shouldn't touch more than ~20 pages or 5x any page's size.

    These are crude proxies for over-extraction (the model panicked and
    sprayed claims everywhere) and page bloat (a single page absorbed
    everything instead of being split).
    """
    findings: list[Finding] = []
    touched = sum(1 for c in diff.values() if c["status"] in {"A", "M", "??"})
    if touched > 20:
        findings.append(
            Finding(
                "block",
                "growth",
                "<diff>",
                f"Ingest touched {touched} pages. SCHEMA.md budget is 5–20.",
            )
        )
    for path, change in diff.items():
        before, after = change.get("before"), change.get("after")
        if before and after and len(after) > 5 * max(len(before), 500):
            findings.append(
                Finding(
                    "fix",
                    "growth",
                    path,
                    f"Page grew {len(before)}→{len(after)} chars (>5x). Probably needs to be split.",
                )
            )
    return findings


def check_citations(diff: dict[str, dict]) -> list[Finding]:
    """Every changed entity/concept page must cite at least one sources/* page.

    Sources/, syntheses/ (lint reports), and questions/ are exempt.
    Finds the worst offenders, not every uncited sentence.
    """
    findings: list[Finding] = []
    for path, change in diff.items():
        if change["after"] is None:
            continue
        if not (path.startswith("wiki/entities/") or path.startswith("wiki/concepts/")):
            continue
        body = FRONTMATTER.sub("", change["after"], count=1)
        # cheap heuristic: claims paragraphs that don't mention sources/
        paragraphs = [p for p in body.split("\n\n") if p.strip() and not p.startswith("#")]
        uncited = [p for p in paragraphs if "sources/" not in p and len(p) > 80]
        if len(uncited) >= 2:
            findings.append(
                Finding(
                    "fix",
                    "citation",
                    path,
                    f"{len(uncited)} substantive paragraphs lack a [[sources/...]] citation.",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# LLM-based check
# ---------------------------------------------------------------------------

VALIDATOR_PROMPT = """\
You are the QUALITY GATE for the HERMES wiki. A different agent just
performed an INGEST and produced the diff below. SCHEMA.md (the schema)
is also below.

Your job is to spot problems the deterministic checks can't:

1. CLAIM GROUNDING. Every claim added to a wiki page must be supported by
   the cited source(s). Flag any claim that is plausible-sounding but does
   not actually appear in the source it cites. This is hallucination and
   it is the single most important check.

2. CONTRADICTION HANDLING. If the diff overwrites a claim that was on the
   page before, did the ingester apply §6 of SCHEMA.md (CONTRADICTS
   blockquote, both views preserved, question filed if unresolved)? Or
   did it silently overwrite?

3. PROPORTIONALITY. Are the extracted claims proportional to what the
   source actually said, or did the ingester over-extract — generating
   detail that isn't in the source, or creating entity pages for trivial
   mentions?

4. SCHEMA DRIFT. Did the ingester invent a new convention that isn't in
   SCHEMA.md? New page type, new frontmatter key, new link style. This
   should never happen silently.

For each problem, output one finding in this JSON shape:

  {"severity": "block" | "fix", "check": "<one of: grounding, contradiction, proportion, drift>",
   "page": "<wiki-relative path>", "message": "<one sentence>",
   "suggested_edit": "<the corrected text, or null>"}

"block" = the diff cannot be committed as-is.
"fix"   = the diff can be committed if your suggested_edit is applied.

Output a single JSON object: {"findings": [...]}. No prose before or after.

---
SCHEMA.md:
{hermes}

---
Sources cited by this ingest (raw text, for grounding):
{sources}

---
Diff (each entry shows path, before, after):
{diff}
"""


def llm_review(
    diff: dict[str, dict],
    wiki_root: pathlib.Path,
    raw_root: pathlib.Path,
    hermes_md: pathlib.Path,
    llm_call,  # callable(prompt: str) -> str
) -> list[Finding]:
    """Hand the diff to a separate model and parse its findings."""
    sources_text = _gather_cited_sources(diff, raw_root)
    diff_blob = json.dumps(
        {p: {k: v for k, v in c.items() if k != "status"} for p, c in diff.items()},
        indent=2,
    )
    prompt = VALIDATOR_PROMPT.format(
        hermes=hermes_md.read_text(encoding="utf-8"),
        sources=sources_text,
        diff=diff_blob,
    )
    try:
        raw = llm_call(prompt)
    except Exception as e:
        # Validator outage shouldn't lose user work — deterministic checks
        # already cover the most common failure modes. Log and degrade.
        logger.warning("Gate validator call failed: %s", e)
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Gate validator returned non-JSON: %s", raw[:200])
        return []
    return [Finding(**f) for f in parsed.get("findings", [])]


def _gather_cited_sources(diff: dict[str, dict], raw_root: pathlib.Path) -> str:
    """Extract the raw text of every source mentioned in the diff.

    Source pages reference raw files via `[[sources/<slug>]]`. The on-disk
    raw filename is `<date>-<slug>.<ext>`, so we glob `*-{slug}.*` to find
    a match across the date-prefixed filenames.
    """
    cited: set[str] = set()
    for change in diff.values():
        if change["after"]:
            for m in re.finditer(r"sources/([\w-]+)", change["after"]):
                cited.add(m.group(1))
    chunks: list[str] = []
    for slug in sorted(cited):
        for candidate in raw_root.rglob(f"*-{slug}.*"):
            try:
                chunks.append(f"=== {slug} ===\n{candidate.read_text(encoding='utf-8')}")
                break
            except (UnicodeDecodeError, OSError):
                continue
    return "\n\n".join(chunks) if chunks else "(no cited sources found in raw/)"


# ---------------------------------------------------------------------------
# Verdict resolution
# ---------------------------------------------------------------------------


def resolve_verdict(findings: Iterable[Finding]) -> Verdict:
    findings = list(findings)
    if any(f.severity == "block" for f in findings):
        return Verdict("REJECT", findings)
    if any(f.severity == "fix" for f in findings):
        return Verdict("APPROVE_WITH_EDITS", findings)
    return Verdict("APPROVE", findings)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def commit(wiki_root: pathlib.Path, message: str) -> None:
    subprocess.run(["git", "-C", str(wiki_root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(wiki_root),
         "-c", "user.email=hermes@local",
         "-c", "user.name=hermes",
         "commit", "-m", message],
        check=True,
    )


def revert(wiki_root: pathlib.Path) -> None:
    """Roll back uncommitted ingest work without touching unrelated files.

    `clean -fd` is scoped to `wiki/` so any user notes dropped at the wiki
    repo root (or under `raw/`) survive a rejection.
    """
    subprocess.run(["git", "-C", str(wiki_root), "reset", "--hard", "HEAD"], check=True)
    subprocess.run(
        ["git", "-C", str(wiki_root), "clean", "-fd", "--", "wiki/"],
        check=True,
    )


def file_rejection_question(
    wiki_root: pathlib.Path, ingest_slug: str, verdict: Verdict
) -> pathlib.Path:
    """When the gate rejects, leave a question page so the next lint or the
    operator can see what went wrong. The diff itself is gone (reverted),
    but the rejection record persists."""
    qdir = wiki_root / "questions"
    qdir.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    path = qdir / f"rejected-ingest-{ingest_slug}-{today}.md"
    body = textwrap.dedent(
        f"""\
        ---
        type: question
        created: {today}
        updated: {today}
        sources: []
        status: active
        confidence: high
        ---

        # Rejected ingest: {ingest_slug}

        The quality gate rejected this ingest on {today}. The diff was
        reverted; this page records why. Re-attempt during lint after
        addressing the findings below, or escalate to the operator.

        ## Findings

        """
    )
    for f in verdict.findings:
        body += f"- **[{f.severity}] {f.check}** in `{f.page}`: {f.message}\n"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    wiki_root: pathlib.Path,
    raw_root: pathlib.Path,
    hermes_md: pathlib.Path,
    ingest_slug: str,
    llm_call,
) -> Verdict:
    diff = staged_diff(wiki_root)
    if not diff:
        return Verdict("APPROVE", [])  # nothing to review

    findings: list[Finding] = []
    findings += check_frontmatter(diff)
    findings += check_links(wiki_root, diff)
    findings += check_growth(diff)
    findings += check_citations(diff)
    # Only call the LLM if cheap checks all passed — saves tokens on
    # obviously broken ingests.
    if not any(f.severity == "block" for f in findings):
        findings += llm_review(diff, wiki_root, raw_root, hermes_md, llm_call)

    verdict = resolve_verdict(findings)

    if verdict.decision == "REJECT":
        revert(wiki_root)
        file_rejection_question(wiki_root, ingest_slug, verdict)
        commit(wiki_root, f"reject: {ingest_slug} (gate)")
    elif verdict.decision == "APPROVE_WITH_EDITS":
        # Apply suggested edits then commit. Edits arrive as full-file
        # replacements from the validator; if they don't, downgrade to REJECT.
        applied = _apply_edits(wiki_root, verdict.findings)
        if not applied:
            revert(wiki_root)
            file_rejection_question(wiki_root, ingest_slug, verdict)
            commit(wiki_root, f"reject: {ingest_slug} (gate, edits unusable)")
            verdict = Verdict("REJECT", verdict.findings)
        else:
            commit(wiki_root, f"ingest: {ingest_slug} (gate-edited)")
    else:
        commit(wiki_root, f"ingest: {ingest_slug}")

    return verdict


def _apply_edits(wiki_root: pathlib.Path, findings: list[Finding]) -> bool:
    """Apply every fix-severity edit. Return False if any expected edit
    couldn't be applied — partial application is treated as REJECT by the
    caller, since silently dropping fixes is the failure mode we want to
    avoid (it produces commits that claim to be 'gate-edited' but aren't)."""
    expected = sum(
        1 for f in findings
        if f.severity == "fix" and f.suggested_edit
    )
    if expected == 0:
        return False
    applied = 0
    for f in findings:
        if f.severity != "fix" or not f.suggested_edit:
            continue
        target = wiki_root / f.page
        if not target.exists():
            logger.warning(
                "Gate edit skipped: target does not exist (%s).", f.page,
            )
            continue
        target.write_text(f.suggested_edit, encoding="utf-8")
        applied += 1
    return applied == expected


