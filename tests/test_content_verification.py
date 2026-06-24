"""Tests for citation content verification (backtick verbatim-quote checking).

Covers the shared BACKTICK_SPAN pattern, Bash-output checking, opt-in file-line
content checking, and how the new tiers are reported. Stdlib unittest only.
"""
import importlib.util
import json
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


def _write_transcript(rows):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


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

    def _verify_citation(self, path, locator, quoted):
        """Verify one Read footnote with an arbitrary :locator ('2', '2-4', or '' for whole file)."""
        reads = {os.path.realpath(path): "ALL"}
        atom = "Read(%s%s)" % (path, (":" + locator) if locator else "")
        body = atom + ((" — `%s`" % quoted) if quoted is not None else "")
        text = "Claim `[1]`.\n\n`[1]` " + body
        return self.mod.verify(text, reads, [], os.path.dirname(path))

    def test_range_content_match(self):
        """Should pointer-verify when the span is within the cited line range."""
        p = self._file(["L1", "def foo():", "    return 1", "L4"])
        try:
            findings, stats, cited = self._verify_citation(p, "2-4", "return 1")
        finally:
            os.remove(p)
        self.assertEqual(stats["pointer_verified"], 1)
        self.assertEqual([f for f in findings if f[0] == "CONTENT_MISMATCH"], [])

    def test_range_content_mismatch(self):
        """Should flag CONTENT_MISMATCH when the span is outside the cited range."""
        p = self._file(["L1", "def foo():", "    return 1", "nope"])
        try:
            findings, stats, cited = self._verify_citation(p, "2-3", "nope")
        finally:
            os.remove(p)
        self.assertIn("CONTENT_MISMATCH", [f[0] for f in findings])

    def test_whole_file_content_match(self):
        """Should match a span anywhere in the file when no line is cited."""
        p = self._file(["alpha", "beta", "gamma"])
        try:
            findings, stats, cited = self._verify_citation(p, "", "beta")
        finally:
            os.remove(p)
        self.assertEqual(stats["pointer_verified"], 1)


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

    def test_policy_tells_model_to_quote_command_verbatim(self):
        """Should tell the model to quote the command verbatim and not expand
        shell variables, since the command match is literal."""
        policy = grounding_spec.render_policy()
        self.assertIn("verbatim", policy.lower())
        self.assertIn("shell variable", policy.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
