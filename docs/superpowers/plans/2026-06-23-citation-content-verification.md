# Citation Content Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Verify the *content* a citation quotes — not just that its pointer is real — using one verbatim-quote (backtick) convention covering Bash output and (opt-in) file-line content.

**Architecture:** A single regex (`BACKTICK_SPAN`, in `grounding_spec.py`) extracts backticked spans from a footnote. The verifier checks each span is an exact substring of the relevant source: for a `Bash(...)` footnote, the union of recorded Bash outputs (→ `output-verified` / `BASH_OUTPUT_MISMATCH`); for a `Read/Edit/Write/MultiEdit` footnote whose pointer already holds, the current file at the cited line/range (→ stays `pointer-verified` / `CONTENT_MISMATCH`). No backticks → unchanged behavior. All new findings are warn-only.

**Tech Stack:** Python 3 standard library only; `unittest`; run via `uv`.

**Spec:** `docs/superpowers/specs/2026-06-21-bash-citation-verification-design.md`

## Global Constraints

- Python 3 **standard library only** — no third-party packages, no pip installs.
- Run all Python via **uv**: `uv run python ...`.
- Tests are plain `unittest`, run by **executing the file directly** (there is no `tests/__init__.py`, so `python -m unittest tests.X` fails): `uv run python tests/<file>.py [TestClass[.test_method]]`.
- Test docstrings start with `"Should ..."`; test names are `test_<action>_<scenario>`.
- **Warn-only default:** `BLOCK_CODES = set()` stays empty. Never add `CONTENT_MISMATCH` or `BASH_OUTPUT_MISMATCH` to it.
- **Single source of truth:** the backtick regex lives once in `grounding_spec.py` as `BACKTICK_SPAN` and is imported by the verifier — never duplicate it.
- When **no content is backticked**, behavior must be byte-for-byte identical to today (no new findings, same tiers).
- All commands run from the worktree root: `/Users/yeu-chernharn/grounding-attribution/.claude/worktrees/citation-content-verification`. Tests derive their own repo path, so they pass regardless of cwd.
- Commit messages use conventional-commit prefixes and end with:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure

- **Modify** `scripts/grounding_spec.py` — add `BACKTICK_SPAN`; extend `_check()`; add the verbatim-quote convention to the emitted policy.
- **Modify** `scripts/grounding-verifier.py` — collect Bash outputs in `collect()`; check Bash + file content in `verify()`; surface the new tiers in `summary_line()`/`report()`.
- **Create** `tests/test_content_verification.py` — TDD tests for every new behavior.
- **Modify** `README.md` — update the "Scope" note.

## Tiers and findings (reference)

| Footnote leading atom | Backticks? | Outcome |
|---|---|---|
| `Read/Edit/Write/MultiEdit`, pointer fails | — | `FABRICATED`/`BAD_LINE`/`UNREAD_FILE`/`UNREAD_LINE` (unchanged) |
| `Read/...`, pointer holds | none | `pointer-verified` (unchanged) |
| `Read/...`, pointer holds | all spans at cited line/range | `pointer-verified` |
| `Read/...`, pointer holds | a span absent at cited line/range | `CONTENT_MISMATCH` (warn) |
| `Bash(...)` | none | `asserted` (unchanged) |
| `Bash(...)` | all spans in recorded output | `output-verified` |
| `Bash(...)` | a span absent | `BASH_OUTPUT_MISMATCH` (warn) |
| anything else (Web/Task/MCP/Grep/Glob/context) | any | `asserted` (unchanged) |

`stats` dict produced by `verify()`: `pointer_verified`, `output_verified`, `asserted`, `failed`, `mismatched`.

---

## Task 1: `BACKTICK_SPAN` pattern in `grounding_spec.py`

**Files:**
- Create: `tests/test_content_verification.py`
- Modify: `scripts/grounding_spec.py` (add `BACKTICK_SPAN` after `ANY_CITATION`, line ~143; extend `_check()`, lines ~275-300)

**Interfaces:**
- Produces: `grounding_spec.BACKTICK_SPAN` — a compiled `re.Pattern`; `BACKTICK_SPAN.findall(s)` returns the inner text of each `` `...` `` span in order.

- [ ] **Step 1: Write the failing test** — create `tests/test_content_verification.py`:

```python
"""Tests for citation content verification (backtick verbatim-quote checking).

Covers the shared BACKTICK_SPAN pattern, Bash-output checking, opt-in file-line
content checking, and how the new tiers are reported. Stdlib unittest only.
"""
import importlib.util
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)
import grounding_spec  # noqa: E402


def _load_verifier():
    """Import grounding-verifier.py fresh (hyphenated name -> load by path)."""
    spec = importlib.util.spec_from_file_location(
        "grounding_verifier", os.path.join(SCRIPTS, "grounding-verifier.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestBacktickSpan(unittest.TestCase):
    def test_extracts_each_backticked_span(self):
        """Should extract the content of every backticked span, in order."""
        spans = grounding_spec.BACKTICK_SPAN.findall("Bash(x) — `Ran 5 tests`, `OK`")
        self.assertEqual(spans, ["Ran 5 tests", "OK"])

    def test_ignores_unquoted_prose(self):
        """Should return nothing when no span is backticked."""
        self.assertEqual(grounding_spec.BACKTICK_SPAN.findall("all tests pass"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_content_verification.py TestBacktickSpan`
Expected: FAIL — `AttributeError: module 'grounding_spec' has no attribute 'BACKTICK_SPAN'`

- [ ] **Step 3: Add `BACKTICK_SPAN`** to `scripts/grounding_spec.py` immediately after the `ANY_CITATION = re.compile(...)` block (line ~143):

```python
# Verbatim-quote convention (shared by Bash output and file-line content checks):
# the author wraps spans they claim are verbatim in backticks; the verifier checks
# exactly those against the real source. findall -> the inner text of each span.
BACKTICK_SPAN = re.compile(r"`([^`]+)`")
```

- [ ] **Step 4: Extend `_check()`** — in `scripts/grounding_spec.py`, add these assertions just before the final `print(...)` in `_check()` (after the `assert rx.search(cite("Read(f.py:1)")), ...` line, ~298):

```python
    # backticked-span extraction: the shared verbatim-quote convention
    spans = BACKTICK_SPAN.findall("Bash(x) — `Ran 5 tests`, `OK`")
    assert spans == ["Ran 5 tests", "OK"], "BACKTICK_SPAN extraction wrong: %r" % spans
    assert BACKTICK_SPAN.findall("no backticks here") == [], "BACKTICK_SPAN matched prose"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python tests/test_content_verification.py TestBacktickSpan`
Expected: PASS (2 tests)
Run: `uv run python scripts/grounding_spec.py --check`
Expected: `grounding_spec OK — 12 tools; ...`, exit 0

- [ ] **Step 6: Commit**

```bash
git add scripts/grounding_spec.py tests/test_content_verification.py
git commit -m "feat: add BACKTICK_SPAN verbatim-quote pattern to grounding_spec

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `collect()` gathers Bash outputs

**Files:**
- Modify: `scripts/grounding-verifier.py` — add `_tool_result_text()` (near `collect()`); rewrite `collect()` (lines ~229-281); update its call site in `main()` (line ~530)
- Modify: `tests/test_content_verification.py` — add `TestCollectBashOutputs`

**Interfaces:**
- Consumes: `RANGE_TOOLS`, `ALL_TOOLS` (already imported).
- Produces: `collect(transcript_path, cwd)` now returns **`(reads, bash_outputs, last_assistant_text)`**. `bash_outputs` is `list[str]` — the text of each Bash `tool_result` this session.

- [ ] **Step 1: Write the failing test** — append to `tests/test_content_verification.py` (before the `if __name__` block):

```python
import json


def _write_transcript(rows):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


class TestCollectBashOutputs(unittest.TestCase):
    def setUp(self):
        self.mod = _load_verifier()

    def test_collects_bash_tool_result_output(self):
        """Should return the text of each Bash tool_result as bash_outputs."""
        rows = [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "b1", "name": "Bash",
                 "input": {"command": "npm test"}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "b1",
                 "content": "Ran 5 tests in 0.523s\nOK"}]}},
        ]
        tr = _write_transcript(rows)
        try:
            reads, bash_outputs, text = self.mod.collect(tr, REPO)
        finally:
            os.remove(tr)
        self.assertEqual(len(bash_outputs), 1)
        self.assertIn("Ran 5 tests in 0.523s", bash_outputs[0])

    def test_ignores_non_bash_tool_results(self):
        """Should not collect tool_results whose tool_use was not Bash."""
        rows = [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "r1", "name": "Read",
                 "input": {"file_path": "/tmp/x.py"}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "r1",
                 "content": "file contents"}]}},
        ]
        tr = _write_transcript(rows)
        try:
            reads, bash_outputs, text = self.mod.collect(tr, REPO)
        finally:
            os.remove(tr)
        self.assertEqual(bash_outputs, [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_content_verification.py TestCollectBashOutputs`
Expected: FAIL — `ValueError: not enough values to unpack (expected 3, got 2)` (collect still returns a 2-tuple)

- [ ] **Step 3: Add `_tool_result_text()`** in `scripts/grounding-verifier.py` immediately above `def collect(` (line ~229):

```python
def _tool_result_text(block):
    """Best-effort plain text of a tool_result block's content (a string, or a
    list of {type:'text', text:...} parts). '' if none."""
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(
            part.get("text", "")
            for part in c
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""
```

- [ ] **Step 4: Replace the whole `collect()` function** (lines ~229-281) with:

```python
def collect(transcript_path, cwd):
    """Return (reads, bash_outputs, last_assistant_text).

    reads: { realpath: "ALL" | list[(start,end|None)] }  -- lines opened this session
    bash_outputs: list[str] -- the text of every Bash tool_result this session,
      for verbatim-quote checking of Bash citations.
    last_assistant_text: ALL assistant text of the current turn, concatenated.
      A single answer is split across many assistant entries interleaved with
      tool calls, so we accumulate every assistant text chunk produced since the
      last genuine user prompt — not just the final fragment.
    """
    reads = {}
    bash_outputs = []
    bash_ids = set()
    answer_parts = []

    def real(p):
        return os.path.realpath(p if os.path.isabs(p) else os.path.join(cwd, p))

    for entry in iter_transcript(transcript_path):
        if _is_user_prompt(entry):
            answer_parts = []  # new human turn -> start of a fresh answer
        for b in blocks_of(entry):
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "tool_use":
                name = b.get("name")
                inp = b.get("input") or {}
                if name == "Bash":
                    bid = b.get("id")
                    if bid:
                        bash_ids.add(bid)
                p = inp.get("file_path") or inp.get("path")
                if not p:
                    continue
                rp = real(p)
                if name in RANGE_TOOLS:
                    offset = inp.get("offset")
                    limit = inp.get("limit")
                    if offset is None and limit is None:
                        reads[rp] = "ALL"
                    elif reads.get(rp) != "ALL":
                        start = int(offset) if offset else 1
                        end = start + int(limit) - 1 if limit else None
                        reads.setdefault(rp, []).append((start, end))
                elif name in ALL_TOOLS:
                    # the file was written/changed this session -> touched in full
                    reads[rp] = "ALL"
            elif btype == "tool_result":
                if b.get("tool_use_id") in bash_ids:
                    t = _tool_result_text(b)
                    if t:
                        bash_outputs.append(t)
        if role_of(entry) == "assistant":
            txt = "".join(
                b.get("text", "")
                for b in blocks_of(entry)
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if txt.strip():
                answer_parts.append(txt)

    last_assistant_text = "\n".join(answer_parts)
    return reads, bash_outputs, last_assistant_text
```

- [ ] **Step 5: Update the call site in `main()`** — change the line (~530):

```python
    reads, text = collect(transcript_path, cwd)
```
to:
```python
    reads, bash_outputs, text = collect(transcript_path, cwd)
```
(Leave the next line `findings, stats, cited = verify(text, reads, cwd)` unchanged — `bash_outputs` is wired into `verify()` in Task 3.)

- [ ] **Step 6: Run tests to verify they pass (incl. regression)**

Run: `uv run python tests/test_content_verification.py TestCollectBashOutputs`
Expected: PASS (2 tests)
Run: `uv run python tests/test_askuserquestion_hook.py`
Expected: PASS (5 tests) — confirms `main()` still works end-to-end

- [ ] **Step 7: Commit**

```bash
git add scripts/grounding-verifier.py tests/test_content_verification.py
git commit -m "feat: collect Bash tool_result outputs in collect()

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `verify()` Bash output check

**Files:**
- Modify: `scripts/grounding-verifier.py` — import `BACKTICK_SPAN`; add `_BASH_ATOM_RE`, `_classify_recorded()`, `_tally()`; rewrite `verify()` (lines ~301-399); update its call site in `main()`
- Modify: `tests/test_content_verification.py` — add `TestVerifyBashOutput`

**Interfaces:**
- Consumes: `grounding_spec.BACKTICK_SPAN` (Task 1); `bash_outputs` from `collect()` (Task 2).
- Produces: `verify(text, reads, bash_outputs, cwd)` (new 4-arg signature) → `(findings, stats, cited)`. New tier strings `"output-verified"` and `"BASH_OUTPUT_MISMATCH"`; `stats` gains keys `output_verified`, `mismatched`.

- [ ] **Step 1: Write the failing test** — append `TestVerifyBashOutput` to `tests/test_content_verification.py`:

```python
class TestVerifyBashOutput(unittest.TestCase):
    def setUp(self):
        self.mod = _load_verifier()

    def _verify(self, body, bash_outputs):
        text = "A claim `[1]`.\n\n`[1]` " + body
        return self.mod.verify(text, {}, bash_outputs, REPO)

    def test_output_verified_when_span_present(self):
        """Should tier a Bash citation output-verified when the span is a
        substring of recorded output."""
        findings, stats, cited = self._verify(
            "Bash(npm test) — `Ran 5 tests`", ["Ran 5 tests in 0.523s\nOK"])
        self.assertIn(("Bash(npm test) — `Ran 5 tests`", "output-verified"), cited)
        self.assertEqual(stats["output_verified"], 1)
        self.assertFalse([f for f in findings if f[0] == "BASH_OUTPUT_MISMATCH"])

    def test_mismatch_when_span_absent(self):
        """Should flag BASH_OUTPUT_MISMATCH (warn) when the span is absent."""
        findings, stats, cited = self._verify(
            "Bash(npm test) — `Ran 9 tests`", ["Ran 5 tests in 0.523s\nOK"])
        self.assertIn("BASH_OUTPUT_MISMATCH", [f[0] for f in findings])
        self.assertEqual(stats["mismatched"], 1)

    def test_asserted_when_no_backticks(self):
        """Should leave a Bash citation asserted when nothing is backticked."""
        findings, stats, cited = self._verify(
            "Bash(npm test) — all tests pass", ["Ran 5 tests"])
        self.assertIn(("Bash(npm test) — all tests pass", "asserted"), cited)
        self.assertEqual(stats["asserted"], 1)

    def test_exact_substring_semantics(self):
        """Should treat `0.5s` as absent from `0.523s` (no fuzzy match)."""
        findings, stats, cited = self._verify("Bash(t) — `0.5s`", ["ran in 0.523s"])
        self.assertIn("BASH_OUTPUT_MISMATCH", [f[0] for f in findings])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_content_verification.py TestVerifyBashOutput`
Expected: FAIL — `TypeError: verify() takes 3 positional arguments but 4 were given`

- [ ] **Step 3: Add the `BACKTICK_SPAN` import** — in `scripts/grounding-verifier.py`, add `BACKTICK_SPAN,` to the `from grounding_spec import (...)` block (lines ~60-66), keeping alphabetical order:

```python
from grounding_spec import (  # noqa: E402
    ALL_TOOLS,
    ANY_CITATION,
    BACKTICK_SPAN,
    CITE_FULL_RE,
    FILE_CITE,
    RANGE_TOOLS,
)
```

- [ ] **Step 4: Add helpers and rewrite `verify()`** — replace the entire `verify()` function (lines ~301-399) with the following (defines `_BASH_ATOM_RE`, `_classify_recorded`, `_tally`, then `verify`). NOTE: the file-content branch is added in Task 4; here the pointer-holds path ends at `cited.append((atom, "pointer-verified"))`:

```python
# A footnote whose leading atom is a Bash citation: its backticked output spans
# are checked against the session's recorded Bash output.
_BASH_ATOM_RE = re.compile(r"^\s*Bash\s*\(")


def _classify_recorded(body, bash_outputs):
    """Tier a non-filesystem footnote. Bash footnotes get verbatim-quote checking
    against recorded output; everything else (Web/Task/MCP/Grep/Glob/context) is
    'asserted'. Returns (tier, finding_or_None)."""
    if not _BASH_ATOM_RE.match(body):
        return "asserted", None
    spans = BACKTICK_SPAN.findall(body)
    if not spans:
        return "asserted", None  # nothing claimed verbatim
    missing = [sp for sp in spans if not any(sp in out for out in bash_outputs)]
    if missing:
        return "BASH_OUTPUT_MISMATCH", (
            "BASH_OUTPUT_MISMATCH",
            "%s — quoted output not found in this session's recorded Bash "
            "output: %r (stale quote, misquote, or resumed session)"
            % (body, missing[0]),
        )
    return "output-verified", None


def _tally(cited):
    """Count citation tiers for the trust summary."""
    tiers = [t for _, t in cited]
    fail = {"FABRICATED", "BAD_LINE", "UNREAD_FILE", "UNREAD_LINE"}
    mismatch = {"CONTENT_MISMATCH", "BASH_OUTPUT_MISMATCH"}
    return {
        "pointer_verified": tiers.count("pointer-verified"),
        "output_verified": tiers.count("output-verified"),
        "asserted": tiers.count("asserted"),
        "failed": sum(1 for t in tiers if t in fail),
        "mismatched": sum(1 for t in tiers if t in mismatch),
    }


def verify(text, reads, bash_outputs, cwd):
    """Verify ONLY the footnote definitions — the authoritative citation list.

    Each footnote is judged by its LEADING atom:
      - a filesystem atom (Read/Edit/Write/MultiEdit) is checked against disk and
        the session reads; if its pointer holds and it backticks line content,
        that span is checked against the cited line/range (CONTENT_MISMATCH on a
        miss) — see Task 4.
      - a Bash atom has its backticked output spans checked against the union of
        recorded Bash outputs (output-verified / BASH_OUTPUT_MISMATCH).
      - anything else is "asserted".
    Spans the author did not backtick are never checked, so paraphrase never
    false-positives.
    """
    findings = []  # (code, message)
    cited = []     # (display, tier) per footnote, in order, de-duplicated
    seen = set()

    for cm in CITE_FULL_RE.finditer(text or ""):
        body = cm.group(1).strip()
        m = FILE_CITE.match(body)  # a checked filesystem atom at the START?
        if not m:
            if body in seen:
                continue
            seen.add(body)
            tier, finding = _classify_recorded(body, bash_outputs)
            if finding:
                findings.append(finding)
            cited.append((body, tier))
            continue

        atom = m.group(0)
        if atom in seen:
            continue
        seen.add(atom)
        path, s = m.group(2), m.group(3)
        line = int(s) if s else None
        before = len(findings)

        abspath = resolve_path(path, cwd, list(reads.keys()))
        if abspath is None:
            findings.append(
                ("FABRICATED",
                 f"{atom} — no such file found "
                 f"(checked cwd, git root, and read files)")
            )
            cited.append((atom, "FABRICATED"))
            continue

        if line is not None:
            n = file_line_count(abspath)
            if n is not None and line > n:
                findings.append(
                    ("BAD_LINE",
                     f"{atom} — file now has only {n} lines "
                     f"(stale citation, or wrong line)")
                )
                cited.append((atom, "BAD_LINE"))
                continue

        rp = os.path.realpath(abspath)
        read_state = line_was_read(reads, rp, line)
        if read_state is None:
            findings.append(
                ("UNREAD_FILE",
                 f"{atom} — cited but not opened this session "
                 f"(ok if resumed from a prior session)")
            )
        elif read_state is False:
            findings.append(
                ("UNREAD_LINE",
                 f"{atom} — file opened, but this line was never in a read range")
            )

        if len(findings) != before:
            cited.append((atom, findings[-1][0]))  # pointer failure
            continue

        cited.append((atom, "pointer-verified"))

    stats = _tally(cited)

    if not ANY_CITATION.search(text or "") and len((text or "").strip()) > 600:
        findings.append(("NO_CITATIONS", "Substantial answer with no citations"))
    return findings, stats, cited
```

- [ ] **Step 5: Update the call site in `main()`** — change the line (~531):

```python
    findings, stats, cited = verify(text, reads, cwd)
```
to:
```python
    findings, stats, cited = verify(text, reads, bash_outputs, cwd)
```

- [ ] **Step 6: Run tests to verify they pass (incl. regression)**

Run: `uv run python tests/test_content_verification.py TestVerifyBashOutput`
Expected: PASS (4 tests)
Run: `uv run python tests/test_askuserquestion_hook.py`
Expected: PASS (5 tests)

- [ ] **Step 7: Commit**

```bash
git add scripts/grounding-verifier.py tests/test_content_verification.py
git commit -m "feat: verify Bash citation output via backtick spans

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `verify()` opt-in file-line content check

**Files:**
- Modify: `scripts/grounding-verifier.py` — add `read_cited_text()` (near `file_line_count`, line ~293); parse the end line and insert the content check in `verify()`'s pointer-holds branch
- Modify: `tests/test_content_verification.py` — add `TestVerifyFileContent`

**Interfaces:**
- Consumes: `BACKTICK_SPAN`; `resolve_path()`; pointer-holds branch of `verify()` from Task 3.
- Produces: tier `"CONTENT_MISMATCH"` (warn) when a backticked span is absent at the cited line/range; otherwise unchanged.

- [ ] **Step 1: Write the failing test** — append `TestVerifyFileContent`:

```python
class TestVerifyFileContent(unittest.TestCase):
    def setUp(self):
        self.mod = _load_verifier()

    def _file(self, lines):
        fd, path = tempfile.mkstemp(suffix=".py")
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def _verify_read(self, path, line, quoted):
        reads = {os.path.realpath(path): "ALL"}
        body = "Read(%s:%d)" % (path, line)
        if quoted is not None:
            body += " — `%s`" % quoted
        text = "Claim `[1]`.\n\n`[1]` " + body
        return self.mod.verify(text, reads, [], os.path.dirname(path))

    def test_pointer_verified_when_content_matches(self):
        """Should stay pointer-verified when the span is at the cited line."""
        p = self._file(["import os", "def foo():", "    return 1"])
        try:
            findings, stats, cited = self._verify_read(p, 2, "def foo():")
        finally:
            os.remove(p)
        self.assertEqual(stats["pointer_verified"], 1)
        self.assertFalse([f for f in findings if f[0] == "CONTENT_MISMATCH"])

    def test_content_mismatch_when_span_absent(self):
        """Should flag CONTENT_MISMATCH when the span is not at the cited line."""
        p = self._file(["import os", "def foo():", "    return 1"])
        try:
            findings, stats, cited = self._verify_read(p, 2, "def bar():")
        finally:
            os.remove(p)
        self.assertIn("CONTENT_MISMATCH", [f[0] for f in findings])
        self.assertEqual(stats["mismatched"], 1)

    def test_indentation_tolerated(self):
        """Should match a span that is a substring of an indented line."""
        p = self._file(["def foo():", "    return 1"])
        try:
            findings, stats, cited = self._verify_read(p, 2, "return 1")
        finally:
            os.remove(p)
        self.assertEqual(stats["pointer_verified"], 1)

    def test_pointer_only_when_no_backticks(self):
        """Should behave exactly as before when no content is backticked."""
        p = self._file(["def foo():", "    return 1"])
        try:
            findings, stats, cited = self._verify_read(p, 1, None)
        finally:
            os.remove(p)
        self.assertEqual(stats["pointer_verified"], 1)
        self.assertFalse(findings)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_content_verification.py TestVerifyFileContent`
Expected: FAIL — `test_content_mismatch_when_span_absent` fails (no `CONTENT_MISMATCH` produced yet; the citation is still tiered `pointer-verified`).

- [ ] **Step 3: Add `read_cited_text()`** in `scripts/grounding-verifier.py` immediately after `file_line_count()` (line ~298):

```python
def read_cited_text(path, start, end):
    """Text of the cited line/range (1-indexed, inclusive), or the whole file
    when no line was cited. None on read error (then the content check is
    skipped — never a false mismatch)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        return None
    if start is None:
        return "\n".join(lines)
    lo = max(1, start)
    hi = end if (end is not None and end >= lo) else lo
    return "\n".join(lines[lo - 1:hi])
```

- [ ] **Step 4: Parse the end line** — in `verify()`, change:

```python
        path, s = m.group(2), m.group(3)
        line = int(s) if s else None
```
to:
```python
        path, s, e = m.group(2), m.group(3), m.group(4)
        line = int(s) if s else None
        end = int(e) if e else None
```

- [ ] **Step 5: Insert the content check** — in `verify()`, replace the pointer-holds tail:

```python
        if len(findings) != before:
            cited.append((atom, findings[-1][0]))  # pointer failure
            continue

        cited.append((atom, "pointer-verified"))
```
with:
```python
        if len(findings) != before:
            cited.append((atom, findings[-1][0]))  # pointer failure
            continue

        # Pointer holds. Opt-in content check: if the footnote backticks the cited
        # line content, confirm each span is verbatim at the cited line/range.
        spans = BACKTICK_SPAN.findall(body[m.end():])
        if spans:
            cited_text = read_cited_text(abspath, line, end)
            if cited_text is not None:
                missing = [sp for sp in spans if sp not in cited_text]
                if missing:
                    findings.append(
                        ("CONTENT_MISMATCH",
                         f"{atom} — quoted content not found at the cited "
                         f"line/range: {missing[0]!r}")
                    )
                    cited.append((atom, "CONTENT_MISMATCH"))
                    continue
        cited.append((atom, "pointer-verified"))
```

- [ ] **Step 6: Run tests to verify they pass (incl. regression)**

Run: `uv run python tests/test_content_verification.py TestVerifyFileContent`
Expected: PASS (4 tests)
Run: `uv run python tests/test_content_verification.py` then `uv run python tests/test_askuserquestion_hook.py`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add scripts/grounding-verifier.py tests/test_content_verification.py
git commit -m "feat: opt-in file-line content check (CONTENT_MISMATCH)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Surface new tiers in `summary_line()` and `report()`

**Files:**
- Modify: `scripts/grounding-verifier.py` — `summary_line()` (lines ~402-414) and `report()` citation-listing loop (lines ~423-430)
- Modify: `tests/test_content_verification.py` — add `TestReportingTiers`

**Interfaces:**
- Consumes: `stats` keys from `_tally()` (Task 3); `cited` tiers.
- Produces: `summary_line()` reports `output-verified` and `content/output mismatch`; `report()` lists `output-verified` citations; mismatches appear under "Grounding check:" with a warn `!` mark.

- [ ] **Step 1: Write the failing test** — append `TestReportingTiers`:

```python
class TestReportingTiers(unittest.TestCase):
    def setUp(self):
        self.mod = _load_verifier()

    def test_summary_includes_output_verified(self):
        """Should show an output-verified count in the summary line."""
        self.assertIn("2 output-verified",
                      self.mod.summary_line({"output_verified": 2}))

    def test_summary_includes_mismatched(self):
        """Should show a content/output mismatch count in the summary line."""
        self.assertIn("1 content/output mismatch",
                      self.mod.summary_line({"mismatched": 1}))

    def test_report_lists_output_verified_citation(self):
        """Should list output-verified citations alongside pointer-verified."""
        out = self.mod.report(
            [], {"output_verified": 1},
            [("Bash(npm test) — `OK`", "output-verified")])
        self.assertIn("output-verified", out)
        self.assertIn("Bash(npm test) — `OK`", out)

    def test_mismatch_surfaces_as_warning(self):
        """Should list BASH_OUTPUT_MISMATCH under Grounding check with a warn mark."""
        out = self.mod.report(
            [("BASH_OUTPUT_MISMATCH", "Bash(x) — `y` not found")],
            {"mismatched": 1}, [("Bash(x) — `y`", "BASH_OUTPUT_MISMATCH")])
        self.assertIn("Grounding check:", out)
        self.assertIn("BASH_OUTPUT_MISMATCH", out)
        self.assertIn("[!]", out)  # warn, not [X]

    def test_new_codes_are_warn_only_by_default(self):
        """Should keep the new codes out of BLOCK_CODES."""
        self.assertNotIn("CONTENT_MISMATCH", self.mod.BLOCK_CODES)
        self.assertNotIn("BASH_OUTPUT_MISMATCH", self.mod.BLOCK_CODES)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_content_verification.py TestReportingTiers`
Expected: FAIL — `test_summary_includes_output_verified` fails (`summary_line` ignores the key and returns `""`).

- [ ] **Step 3: Replace `summary_line()`** (lines ~402-414) with:

```python
def summary_line(stats):
    """Honest one-line trust summary. 'pointer-verified'/'output-verified' mean
    the pointer/quote holds — NOT that the claim's prose is correct."""
    parts = []
    if stats.get("pointer_verified"):
        parts.append("%d pointer-verified" % stats["pointer_verified"])
    if stats.get("output_verified"):
        parts.append("%d output-verified" % stats["output_verified"])
    if stats.get("asserted"):
        parts.append("%d asserted (unchecked)" % stats["asserted"])
    if stats.get("failed"):
        parts.append("%d failed" % stats["failed"])
    if stats.get("mismatched"):
        parts.append("%d content/output mismatch" % stats["mismatched"])
    if not parts:
        return ""
    return "Citations: " + " · ".join(parts)
```

- [ ] **Step 4: Update the `report()` citation-listing loop** — change:

```python
        for atom, tier in cited:
            if tier == "pointer-verified":
                lines.append("  ✓ %s  [%s]" % (atom, tier))
```
to:
```python
        for atom, tier in cited:
            if tier in ("pointer-verified", "output-verified"):
                lines.append("  ✓ %s  [%s]" % (atom, tier))
```

- [ ] **Step 5: Run tests to verify they pass (full suite + self-check)**

Run: `uv run python tests/test_content_verification.py`
Expected: PASS (all classes)
Run: `uv run python tests/test_askuserquestion_hook.py`
Expected: PASS (5 tests) — confirms the citation-listing change didn't regress
Run: `uv run python scripts/grounding_spec.py --check`
Expected: exit 0

- [ ] **Step 6: Commit**

```bash
git add scripts/grounding-verifier.py tests/test_content_verification.py
git commit -m "feat: surface output-verified and mismatch tiers in the report

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Policy text + README scope

**Files:**
- Modify: `scripts/grounding_spec.py` — `_POLICY_TEMPLATE` (filesystem section ~line 213; recorded-output section ~line 218)
- Modify: `README.md` — "Scope (honest limits)" section (lines ~73-79)
- Modify: `tests/test_content_verification.py` — add `TestPolicyText`

**Interfaces:**
- Consumes: `render_policy()`.
- Produces: policy text documenting the verbatim-quote convention (must contain the substrings `backtick` and `output-verified`).

- [ ] **Step 1: Write the failing test** — append `TestPolicyText`:

```python
class TestPolicyText(unittest.TestCase):
    def test_policy_mentions_backtick_convention(self):
        """Should document the verbatim-quote (backtick) convention in the policy."""
        policy = grounding_spec.render_policy()
        self.assertIn("backtick", policy.lower())
        self.assertIn("output-verified", policy)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python tests/test_content_verification.py TestPolicyText`
Expected: FAIL — `AssertionError` (the policy has no "output-verified" / backtick text yet).

- [ ] **Step 3: Add the file-content mention** — in `scripts/grounding_spec.py`, in `_POLICY_TEMPLATE`, after the filesystem guidance line ending `...NOT Read(path:line).` (line ~213), add:

```
  You MAY also quote the exact cited line content in backticks for a stronger
  check — but ONLY if it is already in your context; never re-read a file just
  to quote it.
```

- [ ] **Step 4: Add the Bash mention** — in `_POLICY_TEMPLATE`, immediately after the `%%RECORDED_LIST%%` line (line ~218), add a blank line then:

```
For a Bash citation, wrap the exact output you are claiming in backticks; the
verifier confirms each backticked span is a verbatim substring of the recorded
output (output-verified). Prose you do not backtick is never checked.
```

- [ ] **Step 5: Update the README "Scope" bullet** — in `README.md`, replace the last bullet (lines ~77-79):

```
- Only `Read`/`Edit`/`Write`/`MultiEdit` are auto-checked. `Grep`/`Glob` and the
  recorded-output/conversation citations are not auto-checked yet, so the absence
  of a flag on those is not confirmation.
```
with:
```
- Only `Read`/`Edit`/`Write`/`MultiEdit` pointers are auto-checked. Backticked
  Bash output and backticked file-line content are also grounded (the quoted span
  really appears in the source), but are not semantically judged. `Grep`/`Glob`
  and other recorded-output/conversation citations are not auto-checked yet, so
  the absence of a flag on those is not confirmation.
```

- [ ] **Step 6: Run tests + checks to verify they pass**

Run: `uv run python tests/test_content_verification.py TestPolicyText`
Expected: PASS
Run: `uv run python scripts/grounding_spec.py --check`
Expected: exit 0
Run: `uv run python scripts/grounding_spec.py --emit-policy` then eyeball that the two new sentences render with the rest of the policy.

- [ ] **Step 7: Commit**

```bash
git add scripts/grounding_spec.py README.md tests/test_content_verification.py
git commit -m "docs: document the verbatim-quote convention in policy + README

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Final verification

- [ ] Run the whole suite + self-check:

```bash
uv run python tests/test_content_verification.py
uv run python tests/test_askuserquestion_hook.py
uv run python scripts/grounding_spec.py --check
```
Expected: all PASS / exit 0.

- [ ] Confirm `BLOCK_CODES` is still `set()` (warn-only) in `scripts/grounding-verifier.py`.

---

## Self-Review

**Spec coverage:**
- Backtick mechanism (one rule) → Task 1 (`BACKTICK_SPAN`), used by Tasks 3-4.
- Bash output-verified / `BASH_OUTPUT_MISMATCH` / asserted → Tasks 2 (collect) + 3 (verify).
- File content `pointer-verified` / `CONTENT_MISMATCH`, opt-in, indentation tolerated, drift caught → Task 4.
- No-backtick = unchanged behavior → asserted (Task 3) / pointer-only (Task 4) tests.
- Exact-substring semantics (`0.5s` ⊄ `0.523s`) → Task 3.
- report/summary include `output-verified`; mismatches under "Grounding check:" → Task 5.
- `grounding_spec.py --check` still passes → Tasks 1, 5, 6.
- Opt-in + light policy mention (no re-read) → Task 6; README scope → Task 6.
- All new findings warn-only → Task 5 test `test_new_codes_are_warn_only_by_default` + Final verification.

**Type consistency:** `collect()` → 3-tuple `(reads, bash_outputs, text)` (Task 2) consumed by `main()` and `verify(text, reads, bash_outputs, cwd)` (Task 3). Tier strings `"output-verified"`, `"CONTENT_MISMATCH"`, `"BASH_OUTPUT_MISMATCH"` and `stats` keys `output_verified`/`mismatched` are defined in `_tally()` (Task 3) and read identically in `summary_line()`/`report()` (Task 5).

**Deferred (not in scope, per spec):** Layer 2 semantic intent check (stays the OPTIONAL ESCALATION stub); `is_error` capture for Bash results (only needed by Layer 2 — omitted under YAGNI).
