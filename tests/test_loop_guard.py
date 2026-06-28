"""Tests for the loop guard (should_block) in grounding-verifier.py.

The guard caps forced continuations at MAX_FORCED_CONTINUATIONS using a
per-session state file (count + fingerprint). It must reach that ceiling even on
a forced continuation -- it must NOT short-circuit on stop_hook_active, which is
documented but known to mis-propagate. Stdlib unittest only.
"""
import importlib.util
import os
import shutil
import tempfile
import time
import unittest

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


class TestLoopGuard(unittest.TestCase):
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

    def test_blocks_forced_continuation_under_ceiling(self):
        """Should re-block a forced continuation while still under the ceiling,
        instead of short-circuiting on stop_hook_active."""
        sid = "loop"
        self.mod._save_state(sid, {"count": 1, "fingerprint": "prev",
                                   "ts": time.time()})
        blocking = [("CONTENT_MISMATCH", "Read(x.py:1) — new finding")]
        block, note = self.mod.should_block(sid, blocking)
        self.assertTrue(block, "forced continuation under the ceiling should re-block")

    def test_blocks_up_to_three_times_then_hands_off(self):
        """Should block on three distinct findings then hand off at the ceiling."""
        sid = "three"
        outcomes = []
        for i in range(4):
            blocking = [("CONTENT_MISMATCH", "Read(x.py:%d) — finding %d" % (i, i))]
            block, _ = self.mod.should_block(sid, blocking)
            outcomes.append(block)
        self.assertEqual(outcomes, [True, True, True, False])

    def test_stops_at_retry_ceiling(self):
        """Should decline once count has reached MAX_FORCED_CONTINUATIONS."""
        sid = "ceiling"
        self.mod._save_state(
            sid, {"count": self.mod.MAX_FORCED_CONTINUATIONS,
                  "fingerprint": "prev", "ts": time.time()})
        blocking = [("CONTENT_MISMATCH", "Read(x.py:1) — new finding")]
        block, note = self.mod.should_block(sid, blocking)
        self.assertFalse(block)
        self.assertIn("ceiling", note)

    def test_stops_on_identical_findings_no_progress(self):
        """Should decline a re-block when the findings are identical to last time."""
        sid = "noprog"
        blocking = [("CONTENT_MISMATCH", "Read(x.py:1) — same finding")]
        self.mod._save_state(sid, {"count": 1,
                                   "fingerprint": self.mod._fingerprint(blocking),
                                   "ts": time.time()})
        block, note = self.mod.should_block(sid, blocking)
        self.assertFalse(block)
        self.assertIn("no progress", note)

    def test_clean_turn_clears_state(self):
        """Should reset the task state when a turn produces no blocking findings."""
        sid = "clean"
        self.mod._save_state(sid, {"count": 2, "fingerprint": "prev",
                                   "ts": time.time()})
        block, note = self.mod.should_block(sid, [])
        self.assertFalse(block)
        self.assertEqual(self.mod._load_state(sid)["count"], 0)


if __name__ == "__main__":
    unittest.main()
