# grounding-attribution

A **Claude Code plugin** that fights LLM hallucination by making answers
**grounded and auditable**: every non-trivial claim must cite a
machine-checkable source, and those citations are mechanically **verified**
against your filesystem. It has two halves:

- **Injection** (`UserPromptSubmit`) — adds a policy telling Claude to mark every
  non-trivial claim as VERIFIABLE (with a machine-checkable citation like
  `Read(path:line)`, `Bash(cmd)`, `MCP(server.tool)`) or `unverified`.
- **Verifier** (`Stop`) — mechanically re-checks the filesystem-checkable
  citations (`Read`/`Edit`/`Write`/`MultiEdit`) against the **current** files and
  against what was actually opened this session. Fabricated, out-of-range, or
  never-opened citations are flagged.

The tool taxonomy lives once in `scripts/grounding_spec.py`; both halves derive
from it, so coverage cannot drift between the policy and the verifier.

## Install

Local (development):

    claude --plugin-dir /path/to/grounding-attribution

Or via a marketplace / GitHub repo once published (see Claude Code plugin docs).

## Requirements

- Python 3 on `PATH` (standard library only — no pip installs)
- A POSIX shell (the injection hook is a tiny `bash` wrapper)

## Behavior & tuning

- **Warn-only by default.** Nothing blocks until you set `BLOCK_CODES` in
  `scripts/grounding-verifier.py`, e.g. `BLOCK_CODES = {"FABRICATED"}`.
- When blocking is on, a **loop guard** caps forced retries
  (`MAX_FORCED_CONTINUATIONS`, default 3), stops on no-progress, and resets on a
  clean turn or after `STATE_RESET_SECONDS`. State persists in
  `$CLAUDE_PLUGIN_DATA` (survives plugin updates).
- To change which tools are cited/checked, edit the `TOOLS` table in
  `scripts/grounding_spec.py` — policy text, citation regex, and read-tracking
  all update together.

## Verify / self-check

    python3 scripts/grounding_spec.py --check         # consistency assertions
    python3 scripts/grounding_spec.py --emit-policy    # preview the injected policy

## Scope (honest limits)

- Verifies citation **integrity** (pointer real, in range, actually read), not
  semantic correctness of the prose.
- Only `Read`/`Edit`/`Write`/`MultiEdit` are auto-checked. `Grep`/`Glob` and the
  recorded-output/conversation citations are not auto-checked yet, so the absence
  of a flag on those is not confirmation.
