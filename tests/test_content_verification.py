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

    def test_collects_list_of_text_parts_output(self):
        """Should join list-of-text-parts tool_result content into bash_outputs."""
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
            reads, bash_outputs, text = self.mod.collect(tr, REPO)
        finally:
            os.remove(tr)
        self.assertEqual(bash_outputs, ["hi there"])


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
        self.assertIn(("Bash(npm test) — `Ran 9 tests`", "BASH_OUTPUT_MISMATCH"), cited)

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
        self.assertEqual(stats["mismatched"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
