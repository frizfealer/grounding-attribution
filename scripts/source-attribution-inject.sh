#!/bin/bash
# Source-attribution policy — context-injection hook.
#
# Wire to SessionStart (inject once) or UserPromptSubmit (inject every turn —
# more durable, since SessionStart context can be evicted by compaction in a
# long session, at the cost of a few tokens per turn).
#
# stdout on exit 0 is injected into Claude's context.
#
# This is the *self-report* half. It cannot verify anything on its own — its
# only job is to make claims carry machine-checkable citations so the companion
# Stop hook (grounding-verifier.py) has something real to check.
#
# The policy text is GENERATED from grounding_spec.py (the single source of
# truth for the tool taxonomy), so the tool coverage advertised here can never
# drift from what the verifier actually checks. To change which tools are cited
# or checked, edit the TOOLS table in grounding_spec.py — both sides update.

exec python3 "$(dirname "$0")/grounding_spec.py" --emit-policy
