"""Tests for the AskUserQuestion grounding hook.

The verifier must also run when Claude asks the user a question — Claude Code
fires PreToolUse (matcher AskUserQuestion) right before the question UI shows,
but does NOT fire Stop at that pause. Without a PreToolUse wiring, a turn that
ends by asking a question gets no grounding report and no hook at all.

A PreToolUse hook blocks via hookSpecificOutput.permissionDecision="deny", NOT
the Stop-only {"decision":"block"} schema, so the verifier must emit the right
shape for the event that invoked it.

Stdlib unittest only — the plugin is deliberately dependency-free.
"""
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")


def _load_verifier():
    """Import grounding-verifier.py fresh (hyphenated name -> load by path)."""
    spec = importlib.util.spec_from_file_location(
        "grounding_verifier", os.path.join(SCRIPTS, "grounding-verifier.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_transcript(rows):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def _run_main(mod, payload):
    """Run verifier.main() with payload on stdin; return parsed stdout JSON or None."""
    out = io.StringIO()
    saved = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        with redirect_stdout(out):
            try:
                mod.main()
            except SystemExit:
                pass
    finally:
        sys.stdin = saved
    s = out.getvalue().strip()
    return json.loads(s) if s else None


def _askq_transcript(citation_line):
    """A turn that reads README, writes a substantial answer carrying
    `citation_line`, then ends by calling AskUserQuestion (no tool_result yet —
    this is the PreToolUse moment, before the user answers)."""
    readme = os.path.join(REPO, "README.md")
    answer = (
        "The project is a Claude Code plugin `[1]`.\n\n"
        + ("pad " * 200)
        + "\n\n`[1]` " + citation_line
    )
    return [
        {"type": "user", "message": {"role": "user",
            "content": [{"type": "text", "text": "check the readme and ask me"}]}},
        {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "tool_use", "name": "Read",
                         "input": {"file_path": readme}}]}},
        {"type": "user", "message": {"role": "user",
            "content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": answer}]}},
        {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "tool_use", "name": "AskUserQuestion",
                         "input": {"questions": []}}]}},
    ]


class TestHookWiring(unittest.TestCase):
    def test_wires_verifier_to_pretooluse_askuserquestion(self):
        """Should wire grounding-verifier.py to PreToolUse for AskUserQuestion."""
        with open(os.path.join(REPO, "hooks", "hooks.json")) as f:
            cfg = json.load(f)
        pre = cfg.get("hooks", {}).get("PreToolUse", [])

        def runs_verifier(entry):
            for h in entry.get("hooks", []):
                blob = " ".join(h.get("args", [])) + " " + h.get("command", "")
                if "grounding-verifier.py" in blob:
                    return True
            return False

        matched = [e for e in pre
                   if e.get("matcher") == "AskUserQuestion" and runs_verifier(e)]
        self.assertTrue(
            matched,
            "no PreToolUse hook with matcher 'AskUserQuestion' runs grounding-verifier.py",
        )


class TestPreToolUseOutput(unittest.TestCase):
    def setUp(self):
        # Isolate loop-guard state per test (STATE_DIR derives from this at import).
        self._tmp = tempfile.mkdtemp()
        self._old = os.environ.get("CLAUDE_PLUGIN_DATA")
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp
        self.mod = _load_verifier()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("CLAUDE_PLUGIN_DATA", None)
        else:
            os.environ["CLAUDE_PLUGIN_DATA"] = self._old
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_warn_path_emits_systemMessage_when_asked_a_question(self):
        """Should emit a systemMessage grounding report at PreToolUse (warn-only)."""
        tr = _write_transcript(_askq_transcript("Read(README.md:1)"))
        try:
            out = _run_main(self.mod, {"transcript_path": tr, "cwd": REPO,
                                       "session_id": "warn",
                                       "hook_event_name": "PreToolUse"})
        finally:
            os.remove(tr)
        self.assertIsNotNone(out, "expected output at the AskUserQuestion boundary")
        self.assertIn("systemMessage", out)
        self.assertIn("pointer-verified", out["systemMessage"])

    def test_block_path_uses_permissionDecision_deny_at_pretooluse(self):
        """Should block via permissionDecision:deny, not the Stop-only decision:block."""
        self.mod.BLOCK_CODES = {"FABRICATED"}
        tr = _write_transcript(_askq_transcript("Read(does/not/exist.py:1)"))
        try:
            out = _run_main(self.mod, {"transcript_path": tr, "cwd": REPO,
                                       "session_id": "block",
                                       "hook_event_name": "PreToolUse"})
        finally:
            os.remove(tr)
        self.assertIsNotNone(out)
        self.assertNotIn("decision", out)  # Stop schema must NOT be used at PreToolUse
        hso = out.get("hookSpecificOutput", {})
        self.assertEqual(hso.get("hookEventName"), "PreToolUse")
        self.assertEqual(hso.get("permissionDecision"), "deny")
        self.assertIn("FABRICATED", hso.get("permissionDecisionReason", ""))

    def test_block_path_still_uses_decision_block_at_stop(self):
        """Should keep the {"decision":"block"} schema for the Stop event."""
        self.mod.BLOCK_CODES = {"FABRICATED"}
        tr = _write_transcript(_askq_transcript("Read(does/not/exist.py:1)"))
        try:
            out = _run_main(self.mod, {"transcript_path": tr, "cwd": REPO,
                                       "session_id": "stop",
                                       "hook_event_name": "Stop"})
        finally:
            os.remove(tr)
        self.assertIsNotNone(out)
        self.assertEqual(out.get("decision"), "block")
        self.assertNotIn("hookSpecificOutput", out)


class TestCitationListing(unittest.TestCase):
    def setUp(self):
        self.mod = _load_verifier()

    def test_lists_only_pointer_verified_citations(self):
        """Should list only pointer-verified citations; asserted and failed are
        not listed (failures still surface in the Grounding check section)."""
        findings = [("FABRICATED", "Read(missing.py:1) — no such file found")]
        stats = {"pointer_verified": 1, "asserted": 1, "failed": 1}
        cited = [
            ("Read(a.py:1)", "pointer-verified"),
            ("Bash(git push)", "asserted"),
            ("Read(missing.py:1)", "FABRICATED"),
        ]
        out = self.mod.report(findings, stats, cited)

        # pointer-verified IS listed
        self.assertIn("[pointer-verified]", out)
        self.assertIn("Read(a.py:1)", out)
        # asserted is NOT listed as a citation line
        self.assertNotIn("[asserted]", out)
        self.assertNotIn("Bash(git push)", out)
        # failed is NOT duplicated as a citation line ...
        self.assertNotIn("[FABRICATED]", out)
        # ... but the failure still appears in the Grounding check section
        self.assertIn("Grounding check:", out)
        self.assertIn("FABRICATED:", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
