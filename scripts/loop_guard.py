#!/usr/bin/env python3
"""
loop_guard.py — per-session forced-continuation budget for the grounding hook.

A self-contained, stateful concern lifted out of the verifier: when a blocking
finding would force Claude to continue, this decides whether to actually block,
bounded by two guards — a retry ceiling and a no-progress fingerprint — using a
small per-session state file (count + fingerprint). It shares nothing with the
citation engine except being called once per turn from grounding_engine.run, so
it lives in its own module with its own test (tests/test_loop_guard.py).

Deliberately does NOT consult stop_hook_active (documented but known to
mis-propagate when system reminders interleave); the per-session state file is
the sole bound, and it survives across the separate hook processes within a turn.
"""

import hashlib
import json
import os
import re
import tempfile
import time

MAX_FORCED_CONTINUATIONS = 3  # hard ceiling on blocks per task
STATE_RESET_SECONDS = 600  # gap that counts as a new task -> reset


def _state_dir():
    """Where per-session state lives, resolved at call time so a test (or a
    later process) that sets CLAUDE_PLUGIN_DATA is honored without re-importing.
    Use the plugin's data dir when running as a plugin (it survives plugin
    updates); fall back to the system temp dir otherwise."""
    return os.path.join(
        os.environ.get("CLAUDE_PLUGIN_DATA") or tempfile.gettempdir(),
        "grounding-verifier-state",
    )


def _state_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "nosession")
    return os.path.join(_state_dir(), safe + ".json")


def _load_state(session_id):
    try:
        with open(_state_path(session_id)) as f:
            return json.load(f)
    except Exception:
        return {"count": 0, "fingerprint": None, "ts": 0}


def _save_state(session_id, state):
    # Write to a temp file in the same dir, then os.replace() in — atomic on
    # POSIX, so an interrupted or concurrent write can't leave a truncated state
    # file that _load_state() would silently reset (weakening the retry ceiling).
    try:
        d = _state_dir()
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f)
            os.replace(tmp, _state_path(session_id))
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    except Exception:
        pass


def _clear_state(session_id):
    try:
        os.remove(_state_path(session_id))
    except Exception:
        pass


def _fingerprint(blocking):
    payload = "\n".join(sorted(f"{c}:{m}" for c, m in blocking))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def should_block(session_id, blocking):
    """Decide whether to actually block, bounded by two guards (a retry ceiling
    and a no-progress fingerprint).
    Returns (block: bool, note: str) — note explains a *declined* block."""
    if not blocking:
        _clear_state(session_id)  # clean turn -> reset the task
        return False, ""

    now = time.time()
    st = _load_state(session_id)
    if now - st.get("ts", 0) > STATE_RESET_SECONDS:
        st = {"count": 0, "fingerprint": None, "ts": now}  # new task

    fp = _fingerprint(blocking)
    if st["count"] >= MAX_FORCED_CONTINUATIONS:
        return False, f"hit the {MAX_FORCED_CONTINUATIONS}-retry ceiling; handing off"
    if fp == st.get("fingerprint"):
        return False, "identical findings as last block (no progress); handing off"

    st = {"count": st["count"] + 1, "fingerprint": fp, "ts": now}
    _save_state(session_id, st)
    return True, ""
