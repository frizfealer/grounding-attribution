# Block only finding-codes that check stable ground truth and have a safe fix

Status: accepted

Tracking: [#2](https://github.com/frizfealer/ground-check/issues/2) — implementation

## Context & decision

GroundCheck's verifier either blocks a turn (forces a fix) or merely warns, per
the `BLOCK_CODES` set. We ship `BLOCK_CODES = {"CONTENT_MISMATCH"}` as the
default. `FABRICATED` meets the same ground-truth + safe-fix bar (below) and is
*eligible* to block, but is held at warn pending further grilling of the
loop-guard ↔ blocking coupling. `UNREAD_FILE`, `command-not-found`, `BAD_LINE`,
`AMBIGUOUS_COMMAND`, and `NO_CITATIONS` stay at warn.

The governing principle: **a finding may block only if (a) its check runs against
stable ground truth — the filesystem — and (b) the forced fix is safe and
self-healing.** A false block punishes a *correct, honest* answer, which is the
one failure the tool must never commit — an over-blocking grounding tool gets
switched off, defeating its purpose.

- `CONTENT_MISMATCH` and `FABRICATED` check the file on disk: stable, near-zero
  false-positive, and the agent can fix in place (correct the quote, drop the bad
  citation) — both *eligible* to block. `CONTENT_MISMATCH` is enabled by default;
  `FABRICATED` is held at warn for now (the coupling is still being grilled).
- `UNREAD_FILE` and `command-not-found` check a *reconstructed session log*
  (`collect()`'s `reads` / `bash_calls` sets), not disk. → warn.

## Considered options

- **Block by severity** — promote the worst-*sounding* hallucinations (e.g.
  `UNREAD_FILE`) first. Rejected: severity doesn't track false-positive risk.
  `UNREAD_FILE` sounds severe but false-fires on honest citations whose evidence
  is genuinely real (see below).
- **Block by ground-truth + fix-safety** (chosen) — align blocking with the only
  cost that matters: never block a correct answer.

## Consequences

- `UNREAD_FILE` and `command-not-found` are held at warn because they check a
  *reconstructed session log* (`collect()`'s `reads` / `bash_calls` sets), not
  disk — and the engine warns that its read-tracking can silently break across
  Claude Code transcript-schema versions. A silent tracking gap would false-fire
  these on honest citations, so neither can block until tracking is proven robust.
- The **subagent case is not** a reason to ingest subagent transcripts. A file
  read only inside a subagent is logged to a separate `<session>/subagents/
  agent-*.jsonl` that `collect()` does not read; the honest citation for it is
  `Task(subagent)`, not a main-agent `Read`. So `UNREAD_FILE` firing there is
  *correct* provenance enforcement, not a false positive. Subagents are grounded
  as their own turns — see ADR 0002.
- Compaction and resume were investigated and do **not** drop reads (compaction
  is in-place; resume re-opens the same file), so they are not reasons to
  withhold blocking.
- **Withdrawn:** an earlier draft proposed making `collect()` ingest
  `subagents/agent-*.jsonl` to "unblock" promoting `UNREAD_FILE`. Rejected — it
  would launder a subagent's read into a main-agent direct-`Read` citation (the
  rejected "transcript merge" option in ADR 0002). The path to promoting
  `UNREAD_FILE` is read-tracking robustness, not subagent ingestion.
