"""Tests for the grounding on/off toggle (global flag file).

The toggle lives in grounding_spec.py so BOTH halves (the --emit-policy injector
and the verifier, which imports grounding_spec) consult one switch. The flag file
is global (not per-session) and defaults to on when absent. Stdlib unittest only.
"""
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
sys.path.insert(0, SCRIPTS)
import grounding_spec  # noqa: E402
import grounding_engine  # noqa: E402


def _write_transcript(rows):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def _bad_citation_transcript():
    """An assistant turn citing a file that does not exist (would normally warn)."""
    return [{"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text",
         "text": "See `[1]`.\n\n`[1]` Read(does/not/exist.py:1)"}]}}]


class TestToggle(unittest.TestCase):
    def setUp(self):
        # Isolate the global flag file under a temp CLAUDE_CONFIG_DIR. The flag
        # must live at a fixed config-dir path, NOT under CLAUDE_PLUGIN_DATA
        # (which varies by plugin context across the command vs. the hooks).
        self._tmp = tempfile.mkdtemp()
        self._old_cfg = os.environ.get("CLAUDE_CONFIG_DIR")
        self._old_data = os.environ.get("CLAUDE_PLUGIN_DATA")
        os.environ["CLAUDE_CONFIG_DIR"] = self._tmp
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def tearDown(self):
        for key, old in (("CLAUDE_CONFIG_DIR", self._old_cfg),
                         ("CLAUDE_PLUGIN_DATA", self._old_data)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_enabled_by_default(self):
        """Should be enabled when no flag file exists."""
        self.assertTrue(grounding_spec.is_enabled())

    def test_set_enabled_round_trip(self):
        """Should disable then re-enable through set_enabled."""
        grounding_spec.set_enabled(False)
        self.assertFalse(grounding_spec.is_enabled())
        grounding_spec.set_enabled(True)
        self.assertTrue(grounding_spec.is_enabled())

    def test_set_enabled_creates_missing_data_dir(self):
        """Should write the flag even when the config dir does not exist yet."""
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self._tmp, "nested", "dir")
        grounding_spec.set_enabled(False)
        self.assertFalse(grounding_spec.is_enabled())

    def test_flag_path_independent_of_plugin_data(self):
        """Flag path should not move when CLAUDE_PLUGIN_DATA changes."""
        os.environ["CLAUDE_PLUGIN_DATA"] = "/ctx/A"
        p1 = grounding_spec._flag_path()
        os.environ["CLAUDE_PLUGIN_DATA"] = "/ctx/B"
        p2 = grounding_spec._flag_path()
        self.assertEqual(p1, p2)

    def test_toggle_persists_across_plugin_context_change(self):
        """A value written under one plugin context should read back under another.

        The command and the hooks run as separate processes with different
        CLAUDE_PLUGIN_DATA; the off-write must still reach the reader.
        """
        os.environ["CLAUDE_PLUGIN_DATA"] = "/ctx/A"
        grounding_spec.set_enabled(False)
        os.environ["CLAUDE_PLUGIN_DATA"] = "/ctx/B"
        self.assertFalse(grounding_spec.is_enabled())

    def test_emit_policy_empty_when_disabled(self):
        """Should emit nothing for --emit-policy when grounding is off."""
        grounding_spec.set_enabled(False)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = grounding_spec.main(["--emit-policy"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")

    def test_emit_policy_present_when_enabled(self):
        """Should emit the full policy for --emit-policy when grounding is on."""
        out = io.StringIO()
        with redirect_stdout(out):
            grounding_spec.main(["--emit-policy"])
        self.assertIn("GROUNDING POLICY", out.getvalue())

    def _run_set(self, value):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = grounding_spec.main(["--set", value])
        return rc, out.getvalue()

    def test_set_cli_off_disables_and_reports(self):
        """Should disable and report the new state for --set off."""
        rc, out = self._run_set("off")
        self.assertEqual(rc, 0)
        self.assertFalse(grounding_spec.is_enabled())
        self.assertIn(": off", out)

    def test_set_cli_on_enables_and_reports(self):
        """Should enable and report the new state for --set on."""
        grounding_spec.set_enabled(False)
        rc, out = self._run_set("on")
        self.assertTrue(grounding_spec.is_enabled())
        self.assertIn(": on", out)

    def test_set_cli_toggle_flips(self):
        """Should flip the current state for --set toggle."""
        grounding_spec.set_enabled(True)
        self._run_set("toggle")
        self.assertFalse(grounding_spec.is_enabled())
        self._run_set("toggle")
        self.assertTrue(grounding_spec.is_enabled())

    def test_set_cli_empty_reports_status_without_changing(self):
        """Should report status and not change state for a bare --set."""
        grounding_spec.set_enabled(False)
        rc, out = self._run_set("")
        self.assertFalse(grounding_spec.is_enabled())
        self.assertIn(": off", out)

    def test_verifier_noops_when_disabled(self):
        """Engine run() should return None (print nothing) when grounding is off."""
        grounding_spec.set_enabled(False)
        tr = _write_transcript(_bad_citation_transcript())
        try:
            out = grounding_engine.run({"transcript_path": tr, "cwd": REPO,
                                        "session_id": "off", "hook_event_name": "Stop"})
        finally:
            os.remove(tr)
        self.assertIsNone(out)

    def test_verifier_reports_when_enabled(self):
        """Engine run() should still report on the same input when grounding is on."""
        tr = _write_transcript(_bad_citation_transcript())
        try:
            out = grounding_engine.run({"transcript_path": tr, "cwd": REPO,
                                        "session_id": "on", "hook_event_name": "Stop"})
        finally:
            os.remove(tr)
        self.assertIsNotNone(out)


if __name__ == "__main__":
    unittest.main()
