#!/usr/bin/env python3
"""
grounding-verifier.py — Claude Code Stop / PreToolUse hook entrypoint.

Thin shim: read the hook payload from stdin, hand it to grounding_engine.run
(the importable engine that does all the citation-integrity work), and print the
JSON response. All testable logic lives in grounding_engine.py (the engine +
the build_response host adapters) and loop_guard.py (the forced-continuation
budget); this file is only stdin/stdout + sys.exit, so it stays trivial and is
the one piece that does not need its own tests.

Wire to: Stop, PreToolUse/AskUserQuestion (Stop does not fire when Claude pauses
to ask the user a question), and SubagentStop if you use subagents.
"""

import json
import os
import sys

# Running a script puts its dir on sys.path, but be explicit so `import
# grounding_engine` resolves no matter how the hook is invoked.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grounding_engine as engine  # noqa: E402


def load_input():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def main():
    response = engine.run(load_input())
    if response is not None:
        print(json.dumps(response))
    sys.exit(0)


if __name__ == "__main__":
    main()
