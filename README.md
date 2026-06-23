# grounding-attribution

A **Claude Code plugin** that fights LLM hallucination by making answers
**grounded and auditable**: every non-trivial claim must cite a
machine-checkable source, and those citations are mechanically **verified**
against your filesystem. It has two halves:

- **Injection** (`UserPromptSubmit`) — adds a policy telling Claude to mark every
  non-trivial claim as VERIFIABLE (with a machine-checkable citation like
  `Read(path:line)`, `Bash(cmd)`, `MCP(server.tool)`) or `unverified`.
- **Verifier** (`Stop` **and** `PreToolUse`/`AskUserQuestion`) — mechanically
  re-checks the filesystem-checkable citations (`Read`/`Edit`/`Write`/`MultiEdit`)
  against the **current** files and against what was actually opened this session.
  Fabricated, out-of-range, or never-opened citations are flagged. It runs both
  when Claude finishes a turn **and** when Claude pauses to ask you a question
  (where `Stop` does not fire), so question-ending answers still get checked.

The tool taxonomy lives once in `scripts/grounding_spec.py`; both halves derive
from it, so coverage cannot drift between the policy and the verifier.

## What problem does this solve?

Large language models confidently cite files, line numbers, and command output
that don't exist — the `app.py:42` that was never opened, the test result that
was never run. If you've wanted to **stop Claude Code from hallucinating
citations**, **verify that AI-generated `file:line` references are real**, or
**audit which claims in an answer are actually grounded in your codebase**, that
is exactly what this plugin enforces:

- **Catch fabricated file:line citations** — every `Read`/`Edit`/`Write`
  citation is re-checked against the current file on disk; pointers to missing
  files or out-of-range lines are flagged.
- **Catch never-opened references** — a citation to a file the session never
  actually read is flagged, even if the file exists.
- **Force a grounded-vs-unverified split** — Claude must label every non-trivial
  claim as a machine-checkable citation or an explicit `unverified`, so you can
  see at a glance what rests on evidence and what rests on the model's guess.

It is a **provenance and fact-checking layer for AI coding agents**, built on
Claude Code hooks — no API keys, no external services, standard-library Python
only.

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
- Only `Read`/`Edit`/`Write`/`MultiEdit` pointers are auto-checked. Backticked
  Bash output and backticked file-line content are also grounded (the quoted span
  really appears in the source), but are not semantically judged. `Grep`/`Glob`
  and other recorded-output/conversation citations are not auto-checked yet, so
  the absence of a flag on those is not confirmation.
