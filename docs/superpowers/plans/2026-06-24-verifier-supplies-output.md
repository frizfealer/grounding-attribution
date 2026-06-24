# Verifier-Supplies-Output for Bash Citations — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a Bash citation verify by *command presence* (the command ran this session) instead of by the model re-typing output, eliminating the `BASH_OUTPUT_MISMATCH` false-positive class.

**Architecture:** `collect()` already walks the transcript; extend it to pair each Bash command with its recorded output. The verifier matches a cited command against those recorded commands (`call-verified`), and surfaces the real recorded output in its own report. The model no longer transcribes output, so `output-verified` / `BASH_OUTPUT_MISMATCH` are removed entirely. Read's tiers (`pointer-verified` + optional backtick `CONTENT_MISMATCH`) are untouched.

**Tech Stack:** Python 3 stdlib only (`unittest`), `uv` for running. Two source files: `scripts/grounding-verifier.py` (the Stop/PreToolUse hook) and `scripts/grounding_spec.py` (single source of truth for taxonomy + policy text). Tests in `tests/test_content_verification.py`.

**Spec:** `docs/superpowers/specs/2026-06-23-verifier-supplies-output-design.md`

## Global Constraints

- **Single source of truth:** the tool taxonomy and policy text live ONLY in `scripts/grounding_spec.py`. Any atom/blurb/policy change is made there; the verifier imports from it.
- **Warn-only default:** `BLOCK_CODES = set()` stays empty. The new `command-not-found` code must NOT be added to `BLOCK_CODES`.
- **Read tiers are untouched:** `pointer-verified`, the optional backtick content check, `CONTENT_MISMATCH`, and `BACKTICK_SPAN` remain exactly as they are. Only the Bash path changes.
- **No `output-verified` / `BASH_OUTPUT_MISMATCH`** anywhere after this work — code, tier counts, policy text, or tests.
- **Command match rule:** a cited command matches a recorded one when the cited text (whitespace-normalized) is a **substring** of a recorded command (whitespace-normalized). This implements spec decision 3's "normalized matching" rather than strict equality, because real commands are often long/multi-line and exact-quoting them in a footnote is unusable. Trade-off: an over-generic cited fragment can over-match — acceptable, since it is a far softer failure than a false `command-not-found`.
- **`cited` entries are 3-tuples** `(display, tier, detail)`; `detail` is `None` except for `call-verified` Bash, where it is `(n_runs:int, latest_output:str)`.
- **Tests:** stdlib `unittest` only; docstrings start with "Should …"; run a file with `uv run python tests/test_content_verification.py`, a single test with `uv run python tests/test_content_verification.py Class.method -v`.

---

### Task 1: Pair Bash commands with their outputs in `collect()`

`collect()` currently returns `bash_outputs` (a list of output strings). Command-matching needs the command too, so return `bash_calls`: a list of `(command, output)` pairs.

**Files:**
- Modify: `scripts/grounding-verifier.py` — `collect()` (lines 245-309), and its docstring
- Test: `tests/test_content_verification.py` — replace `class TestCollectBashOutputs` (lines 48-102)

**Interfaces:**
- Produces: `collect(transcript_path, cwd) -> (reads, bash_calls, last_assistant_text)` where `bash_calls: list[tuple[str, str]]` is `(command, output)` in transcript order.

- [ ] **Step 1: Replace the collect-output tests with collect-pairs tests**

In `tests/test_content_verification.py`, replace the entire `class TestCollectBashOutputs` (lines 48-102) with:

```python
class TestCollectBashCalls(unittest.TestCase):
    def setUp(self):
        self.mod = _load_verifier()

    def test_pairs_bash_command_with_its_output(self):
        """Should return each Bash call as a (command, output) pair."""
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
            reads, bash_calls, text = self.mod.collect(tr, REPO)
        finally:
            os.remove(tr)
        self.assertEqual(len(bash_calls), 1)
        self.assertEqual(bash_calls[0][0], "npm test")
        self.assertIn("Ran 5 tests in 0.523s", bash_calls[0][1])

    def test_ignores_non_bash_tool_results(self):
        """Should not record a non-Bash tool_use as a Bash call."""
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
            reads, bash_calls, text = self.mod.collect(tr, REPO)
        finally:
            os.remove(tr)
        self.assertEqual(bash_calls, [])

    def test_joins_list_of_text_parts_output(self):
        """Should join list-of-text-parts tool_result content into the output."""
        rows = [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "b2", "name": "Bash",
                 "input": {"command": "echo hi"}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "b2",
                 "content": [{"type": "text", "text": "hi there"}]}]}},
        ]
        tr = _write_transcript(rows)
        try:
            reads, bash_calls, text = self.mod.collect(tr, REPO)
        finally:
            os.remove(tr)
        self.assertEqual(bash_calls, [("echo hi", "hi there")])
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run python tests/test_content_verification.py TestCollectBashCalls -v`
Expected: FAIL — `collect()` still returns the old `bash_outputs` list (pairs assertions fail, e.g. `bash_calls[0][0]` is a string of output, not the command).

- [ ] **Step 3: Rewrite `collect()` to return pairs**

In `scripts/grounding-verifier.py`, change the docstring's `bash_outputs:` paragraph (lines 248-250) to:

```python
    bash_calls: list[(command, output)] -- every Bash call this session, pairing
      the command string with the text of its tool_result, for command-presence
      checking of Bash citations.
```

Replace the body initialization (lines 256-259) `bash_outputs = []`, `bash_ids = set()` with:

```python
    reads = {}
    bash_calls = []
    bash_cmd = {}  # tool_use_id -> command string, to pair with its result
    answer_parts = []
```

Replace the Bash-capture branch (lines 274-277) with:

```python
                if name == "Bash":
                    bid = b.get("id")
                    if bid:
                        bash_cmd[bid] = inp.get("command", "")
```

Replace the tool_result branch (lines 294-298) with:

```python
            elif btype == "tool_result":
                tid = b.get("tool_use_id")
                if tid in bash_cmd:
                    bash_calls.append((bash_cmd[tid], _tool_result_text(b)))
```

Change the return (line 309) to:

```python
    return reads, bash_calls, last_assistant_text
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python tests/test_content_verification.py TestCollectBashCalls -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/grounding-verifier.py tests/test_content_verification.py
git commit -m "feat: collect Bash (command, output) pairs for command-presence checks"
```

---

### Task 2: Command-presence classification; drop output-verified

Replace output-substring checking with command-presence matching, and switch `cited` to 3-tuples carrying the recorded output to render.

**Files:**
- Modify: `scripts/grounding-verifier.py` — add helpers near line 347; rewrite `_classify_recorded` (370-387), `_tally` (390-401), `verify` (404-504); rename `verify`'s param `bash_outputs` → `bash_calls`; update `main()` unpack (639-640); update the finding-codes docstring (lines 32-36)
- Test: `tests/test_content_verification.py` — replace `class TestVerifyBashOutput` (lines 105-157) with `class TestVerifyBashCall`

**Interfaces:**
- Consumes: `bash_calls` from Task 1.
- Produces:
  - `_cited_command(body) -> str` — normalized command inside the outer `Bash(...)`, backticks stripped; `""` if not a Bash atom.
  - `_classify_recorded(body, bash_calls) -> (tier, finding_or_None, detail_or_None)` — Bash → `call-verified` (detail `(n_runs, latest_output)`) or `command-not-found`; else `asserted` (detail `None`).
  - `verify(text, reads, bash_calls, cwd) -> (findings, stats, cited)` with `cited` entries `(display, tier, detail)`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_content_verification.py`, replace the entire `class TestVerifyBashOutput` (lines 105-157) with:

```python
class TestVerifyBashCall(unittest.TestCase):
    def setUp(self):
        self.mod = _load_verifier()

    def _verify(self, body, bash_calls):
        text = "A claim `[1]`.\n\n`[1]` " + body
        return self.mod.verify(text, {}, bash_calls, REPO)

    def test_call_verified_when_command_recorded(self):
        """Should tier a Bash citation call-verified when the command ran."""
        findings, stats, cited = self._verify(
            "Bash(npm test) — all pass", [("npm test", "Ran 5 tests\nOK")])
        self.assertEqual(stats["call_verified"], 1)
        self.assertEqual(cited[0][:2], ("Bash(npm test) — all pass", "call-verified"))
        self.assertEqual(cited[0][2], (1, "Ran 5 tests\nOK"))
        self.assertFalse(findings)

    def test_command_not_found_when_absent(self):
        """Should flag command-not-found (warn) when no such command ran."""
        findings, stats, cited = self._verify(
            "Bash(rm -rf /) — done", [("npm test", "OK")])
        self.assertIn("command-not-found", [f[0] for f in findings])
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(cited[0][1], "command-not-found")

    def test_substring_of_recorded_command_matches(self):
        """Should match when the cited command is a substring of a longer run."""
        findings, stats, cited = self._verify(
            "Bash(uv run python tests/x.py) — green",
            [("uv run python tests/x.py 2>&1 | tail -3", "OK")])
        self.assertEqual(stats["call_verified"], 1)

    def test_backticked_command_atom_matches(self):
        """Should strip backticks inside Bash(...) before matching."""
        findings, stats, cited = self._verify(
            "Bash(`ls scripts`) — listing", [("ls scripts", "a.py\nb.py")])
        self.assertEqual(stats["call_verified"], 1)

    def test_multi_run_shows_latest_with_count(self):
        """Should report the run count and the latest output when a command ran
        more than once."""
        findings, stats, cited = self._verify(
            "Bash(git status) — clean",
            [("git status", "dirty"), ("git status", "nothing to commit")])
        self.assertEqual(stats["call_verified"], 1)
        self.assertEqual(cited[0][2], (2, "nothing to commit"))

    def test_backticked_prose_never_causes_mismatch(self):
        """Should not flag anything for backticked output prose — there is no
        output tier anymore."""
        findings, stats, cited = self._verify(
            "Bash(npm test) — saw `9 passing`", [("npm test", "5 passing")])
        self.assertEqual(stats["call_verified"], 1)
        self.assertEqual([f for f in findings], [])

    def test_non_bash_recorded_is_asserted(self):
        """Should leave a non-Bash recorded citation asserted."""
        findings, stats, cited = self._verify("WebFetch(http://x) — said hi", [])
        self.assertEqual(cited[0][1], "asserted")
        self.assertEqual(stats["asserted"], 1)
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run python tests/test_content_verification.py TestVerifyBashCall -v`
Expected: FAIL — `stats` has no `call_verified` key, `cited` entries are 2-tuples, and `_classify_recorded` still does output-substring checking.

- [ ] **Step 3: Add command-extraction helpers**

In `scripts/grounding-verifier.py`, replace `_bash_output_portion` (lines 350-367) with these helpers (the balanced-paren scan is reused to find the command, not the output):

```python
def _bash_paren_span(body):
    """(open_index, close_index) of the OUTER Bash(...) parens, or (None, None)
    if not a balanced Bash atom. Balanced scan so a command containing parens
    (subshells, $(...)) is handled."""
    open_i = body.find("(")
    if open_i == -1:
        return None, None
    depth = 0
    for j in range(open_i, len(body)):
        if body[j] == "(":
            depth += 1
        elif body[j] == ")":
            depth -= 1
            if depth == 0:
                return open_i, j
    return None, None


def _normalize_cmd(s):
    """Collapse whitespace runs to single spaces and strip, so cosmetic spacing
    differences never cause a false command-not-found."""
    return " ".join(s.split())


def _cited_command(body):
    """The command text inside the outer Bash(...) of a footnote body, with any
    wrapping backticks removed and whitespace normalized. '' if not a Bash atom."""
    open_i, close_i = _bash_paren_span(body)
    if open_i is None:
        return ""
    inner = body[open_i + 1:close_i].strip().strip("`").strip()
    return _normalize_cmd(inner)
```

- [ ] **Step 4: Rewrite `_classify_recorded`**

Replace `_classify_recorded` (now at the lines following the helpers) with:

```python
def _classify_recorded(body, bash_calls):
    """Tier a non-filesystem footnote.

    A Bash footnote is verified by COMMAND PRESENCE: the cited command must match
    a command actually run this session (cited text, normalized, is a substring of
    a recorded command). The verifier then surfaces that command's recorded output;
    the model never transcribes output, so there is no output-mismatch failure.
    Everything else (Web/Task/MCP/Grep/Glob/context) is 'asserted'.

    Returns (tier, finding_or_None, detail_or_None), where detail for a
    call-verified Bash citation is (n_runs, latest_output)."""
    if not _BASH_ATOM_RE.match(body):
        return "asserted", None, None
    cited = _cited_command(body)
    if not cited:
        return "asserted", None, None
    outs = [out for (cmd, out) in bash_calls if cited in _normalize_cmd(cmd)]
    if not outs:
        return "command-not-found", (
            "command-not-found",
            "%s — no Bash call with this command was recorded this session "
            "(misquoted command, or it ran in a different/resumed session)" % body,
        ), None
    return "call-verified", None, (len(outs), outs[-1])
```

- [ ] **Step 5: Rewrite `_tally`**

Replace `_tally` with (drop `output_verified`/`BASH_OUTPUT_MISMATCH`; add `call_verified`; `command-not-found` counts as failed; `mismatched` now only `CONTENT_MISMATCH`):

```python
def _tally(cited):
    """Count citation tiers for the trust summary."""
    tiers = [c[1] for c in cited]
    fail = {"FABRICATED", "BAD_LINE", "UNREAD_FILE", "UNREAD_LINE", "command-not-found"}
    return {
        "pointer_verified": tiers.count("pointer-verified"),
        "call_verified": tiers.count("call-verified"),
        "asserted": tiers.count("asserted"),
        "failed": sum(1 for t in tiers if t in fail),
        "mismatched": tiers.count("CONTENT_MISMATCH"),
    }
```

- [ ] **Step 6: Update `verify()` to 3-tuples and the new Bash param**

In `verify` (lines 404-504): change the signature `def verify(text, reads, bash_outputs, cwd):` to `def verify(text, reads, bash_calls, cwd):`. Update its docstring's Bash bullet (lines 411-413) to:

```python
      - a Bash atom is checked by command presence against the session's recorded
        Bash calls (call-verified / command-not-found); the verifier renders the
        recorded output, the model does not transcribe it.
```

Change the non-filesystem branch (lines 426-432) to capture and store `detail`:

```python
        if not m:
            if body in seen:
                continue
            seen.add(body)
            tier, finding, detail = _classify_recorded(body, bash_calls)
            if finding:
                findings.append(finding)
            cited.append((body, tier, detail))
            continue
```

Every remaining `cited.append((..., <tier>))` in `verify` must become a 3-tuple with `None` detail. The five sites:

```python
            cited.append((atom, "FABRICATED", None))
```
```python
                cited.append((atom, "BAD_LINE", None))
```
```python
            cited.append((atom, findings[-1][0], None))
```
```python
                    cited.append((atom, "CONTENT_MISMATCH", None))
```
```python
        cited.append((atom, "pointer-verified", None))
```

Then update `main()` (lines 639-640):

```python
    reads, bash_calls, text = collect(transcript_path, cwd)
    findings, stats, cited = verify(text, reads, bash_calls, cwd)
```

Finally, update the finding-codes docstring at the top (lines 32-36): remove nothing there (it never listed Bash codes), but add one line after `UNREAD_LINE`:

```python
  command-not-found  cited a Bash command not run this session (warn-only)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run python tests/test_content_verification.py TestVerifyBashCall -v`
Expected: PASS (7 tests).

- [ ] **Step 8: Commit**

```bash
git add scripts/grounding-verifier.py tests/test_content_verification.py
git commit -m "feat: verify Bash citations by command presence; drop output-verified"
```

---

### Task 3: Reporting — call-verified summary + rendered output

Surface the new tier in the summary line and render the recorded output beneath each call-verified Bash citation.

**Files:**
- Modify: `scripts/grounding-verifier.py` — add `_render_output`; update `summary_line` (507-523) and `report` (526-545)
- Test: `tests/test_content_verification.py` — replace `class TestReportingTiers` (lines 254-288)

**Interfaces:**
- Consumes: `cited` 3-tuples and `stats` from Task 2.
- Produces: `_render_output(n_runs, output) -> str` (an indented block for the report); `summary_line`/`report` understand `call_verified` and the `detail` payload.

- [ ] **Step 1: Write the failing tests**

In `tests/test_content_verification.py`, replace the entire `class TestReportingTiers` (lines 254-288) with:

```python
class TestReportingTiers(unittest.TestCase):
    def setUp(self):
        self.mod = _load_verifier()

    def test_summary_includes_call_verified(self):
        """Should show a call-verified count in the summary line."""
        self.assertIn("2 call-verified",
                      self.mod.summary_line({"call_verified": 2}))

    def test_summary_includes_content_mismatch(self):
        """Should show a content mismatch count in the summary line."""
        self.assertIn("1 content mismatch",
                      self.mod.summary_line({"mismatched": 1}))

    def test_report_renders_recorded_output(self):
        """Should print the recorded output beneath a call-verified citation."""
        out = self.mod.report(
            [], {"call_verified": 1},
            [("Bash(npm test) — pass", "call-verified", (1, "Ran 5 tests\nOK"))])
        self.assertIn("call-verified", out)
        self.assertIn("Ran 5 tests", out)
        self.assertIn("OK", out)

    def test_report_shows_run_count_when_multiple(self):
        """Should note the run count when a command ran more than once."""
        out = self.mod.report(
            [], {"call_verified": 1},
            [("Bash(git status) — clean", "call-verified", (3, "clean"))])
        self.assertIn("ran 3", out)

    def test_command_not_found_surfaces_as_warning(self):
        """Should list command-not-found under Grounding check with a warn mark."""
        out = self.mod.report(
            [("command-not-found", "Bash(x) — not recorded")],
            {"failed": 1}, [("Bash(x)", "command-not-found", None)])
        self.assertIn("Grounding check:", out)
        self.assertIn("command-not-found", out)
        self.assertIn("[!]", out)  # warn, not [X]

    def test_new_code_is_warn_only_by_default(self):
        """Should keep command-not-found out of BLOCK_CODES."""
        self.assertNotIn("command-not-found", self.mod.BLOCK_CODES)
        self.assertNotIn("CONTENT_MISMATCH", self.mod.BLOCK_CODES)
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run python tests/test_content_verification.py TestReportingTiers -v`
Expected: FAIL — `summary_line` still emits "output-verified"; `report` unpacks 2-tuples and does not render output; `_render_output` does not exist.

- [ ] **Step 3: Add `_render_output`**

In `scripts/grounding-verifier.py`, add directly above `summary_line` (line 507):

```python
def _render_output(n_runs, output, max_lines=12, max_chars=800):
    """An indented block showing recorded Bash output in the verifier's own
    report. The verifier is the SOURCE of this text (not the model), so trimming
    is safe and is never a 'mismatch'."""
    trimmed = output if len(output) <= max_chars else output[-max_chars:]
    rows = trimmed.splitlines() or [""]
    if len(rows) > max_lines:
        rows = ["…(earlier output trimmed)"] + rows[-max_lines:]
    head = ("ran %d×; latest output:" % n_runs) if n_runs > 1 else "recorded output:"
    return "\n".join(["      " + head] + ["      | " + r for r in rows])
```

- [ ] **Step 4: Update `summary_line`**

Replace the `output_verified` block (lines 513-514) with a `call_verified` block, and relabel the mismatch part (line 519-520) to "content mismatch":

```python
    if stats.get("pointer_verified"):
        parts.append("%d pointer-verified" % stats["pointer_verified"])
    if stats.get("call_verified"):
        parts.append("%d call-verified" % stats["call_verified"])
    if stats.get("asserted"):
        parts.append("%d asserted (unchecked)" % stats["asserted"])
    if stats.get("failed"):
        parts.append("%d failed" % stats["failed"])
    if stats.get("mismatched"):
        parts.append("%d content mismatch" % stats["mismatched"])
```

Also update the `summary_line` docstring (lines 508-509) `'output-verified'` → `'call-verified'`.

- [ ] **Step 5: Update `report` to unpack 3-tuples and render output**

Replace the `LIST_CITATIONS` loop (lines 532-539) with:

```python
    if LIST_CITATIONS and cited:
        # List pointer-verified and call-verified citations. Asserted items are
        # omitted as noise; failed citations appear in "Grounding check:" below.
        for atom, tier, detail in cited:
            if tier in ("pointer-verified", "call-verified"):
                lines.append("  ✓ %s  [%s]" % (atom, tier))
                if detail:
                    lines.append(_render_output(*detail))
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run python tests/test_content_verification.py TestReportingTiers -v`
Expected: PASS (6 tests).

- [ ] **Step 7: Run the whole verifier test file**

Run: `uv run python tests/test_content_verification.py`
Expected: PASS — all classes green (the Read content tests in `TestVerifyFileContent` are unchanged and still pass).

- [ ] **Step 8: Commit**

```bash
git add scripts/grounding-verifier.py tests/test_content_verification.py
git commit -m "feat: report call-verified tier and render recorded Bash output"
```

---

### Task 4: Policy text + spec taxonomy

Rewrite the injected policy so the model is told to cite the command (not the output), and update the `Bash` blurb. This is the single source of truth — the verifier already reflects the behavior after Tasks 1-3.

**Files:**
- Modify: `scripts/grounding_spec.py` — `Bash` row blurb (line 58); the recorded-output Bash paragraph in `_POLICY_TEMPLATE` (lines 228-231)
- Test: `tests/test_content_verification.py` — replace `class TestPolicyText` (lines 291-304)

**Interfaces:**
- Consumes: nothing new.
- Produces: policy text that says to cite the exact command and that the verifier supplies the output; no "output-verified" wording.

- [ ] **Step 1: Write the failing tests**

In `tests/test_content_verification.py`, replace the entire `class TestPolicyText` (lines 291-304) with:

```python
class TestPolicyText(unittest.TestCase):
    def test_policy_tells_model_to_cite_command_not_output(self):
        """Should tell the model to cite the exact command, not the output."""
        policy = grounding_spec.render_policy()
        self.assertIn("the command, not the output", policy)

    def test_policy_has_no_output_verified_tier(self):
        """Should not mention the removed output-verified tier."""
        self.assertNotIn("output-verified", grounding_spec.render_policy())

    def test_policy_still_cites_injected_memory_as_context(self):
        """Should keep telling the model to cite injected memory as context."""
        policy = grounding_spec.render_policy()
        self.assertIn("injected", policy.lower())
        self.assertIn("context — memory", policy)

    def test_policy_keeps_read_backtick_convention(self):
        """Should keep the optional backtick line-content check for Read."""
        policy = grounding_spec.render_policy()
        self.assertIn("backtick", policy.lower())  # Read content tier survives
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run python tests/test_content_verification.py TestPolicyText -v`
Expected: FAIL — the policy still says "wrap the exact output … (output-verified)".

- [ ] **Step 3: Update the `Bash` blurb**

In `scripts/grounding_spec.py`, change the `Bash` row (line 58) blurb from `"+ the output"` to `"the exact command (the verifier supplies the output)"`:

```python
    Tool("Bash",      RECORDED, "Bash(<exact command>)", False, None, "the exact command (the verifier supplies the output)"),
```

- [ ] **Step 4: Rewrite the Bash policy paragraph**

In `_POLICY_TEMPLATE`, replace the paragraph at lines 228-231:

```python
For a Bash citation, wrap the exact output you are claiming in backticks (the
output, not the command); the verifier confirms each backticked span is a
verbatim substring of the recorded output (output-verified). Prose you do not
backtick is never checked.
```

with:

```python
For a Bash citation, cite the exact COMMAND you ran (the command, not the
output) — e.g. Bash(npm test) — with an optional " — note" after it for the
reader. Do NOT paste the output: the verifier confirms the command actually ran
this session (call-verified) and shows the real recorded output itself, so any
output you would have typed is redundant and unchecked.
```

- [ ] **Step 5: Run the policy tests, the spec self-check, and emit the policy**

Run: `uv run python tests/test_content_verification.py TestPolicyText -v`
Expected: PASS (4 tests).

Run: `uv run python scripts/grounding_spec.py --check`
Expected: `grounding_spec OK — 12 tools; checked=Read,Edit,Write,MultiEdit; range=Read; all=Edit,Write,MultiEdit,NotebookEdit`

Run: `uv run python scripts/grounding_spec.py --emit-policy`
Expected: prints the full policy; no `%%` placeholder markers remain; contains "the command, not the output"; does not contain "output-verified".

- [ ] **Step 6: Run the entire test suite**

Run: `uv run python tests/test_content_verification.py`
Expected: PASS (all classes).

Run: `uv run python tests/test_askuserquestion_hook.py`
Expected: PASS (regression — the PreToolUse wiring is unchanged).

- [ ] **Step 7: Commit**

```bash
git add scripts/grounding_spec.py tests/test_content_verification.py
git commit -m "feat: policy cites the Bash command, not the output (verifier supplies it)"
```

---

## Self-Review

**1. Spec coverage:**
- Core idea (model cites command, verifier supplies output) → Tasks 1-3.
- Drop `output-verified` / make `BASH_OUTPUT_MISMATCH` impossible → Task 2 (removed) + Task 3 (summary/report) + Task 4 (policy).
- `call-verified` / `command-not-found` tiers → Task 2.
- Verifier renders output in its own report → Task 3 (`_render_output`).
- Read symmetry / Read untouched → enforced by Global Constraints; only the Bash path changes.
- Decision 1 (multi-run: match-any existence, render latest + count) → Task 2 `_classify_recorded` (matches any; detail `(n_runs, latest)`), Task 3 render.
- Decision 3 (exact-command rule + `— note`) → Task 2 `_cited_command` (reads inside `Bash(...)`, before any `— note`) + policy (Task 4). Implemented as normalized-substring per the Global Constraints note.
- Decision 5 (cross-session/compaction) → handled by the hedged `command-not-found` message ("misquoted command, or it ran in a different/resumed session") kept warn-only. **Gap vs. spec:** a *distinct* "unverifiable (resumed session)" tier with explicit boundary detection is NOT built — it needs transcript-shape investigation and only matters in enforce-mode (warn-only already never hard-fails). Flag this to the final reviewer as a deliberate deferral.
- Decision 4 (Bash first; Read/Web/Task/MCP later) → only Bash changes here.

**2. Placeholder scan:** No "TBD"/"handle edge cases"/"similar to Task N". Every code step shows complete code. (The one deferral — boundary-detection tier — is called out explicitly above, not hidden as a placeholder.)

**3. Type consistency:** `bash_calls: list[(cmd, out)]` produced by Task 1, consumed by Task 2 (`verify`/`_classify_recorded`). `cited` 3-tuples `(display, tier, detail)` produced in Task 2, consumed by `_tally` (Task 2) and `report` (Task 3). `detail` is `(n_runs, latest_output)` or `None`; `_render_output(n_runs, output)` matches. `stats` keys (`call_verified`, no `output_verified`) consistent across `_tally`/`summary_line`/tests.
