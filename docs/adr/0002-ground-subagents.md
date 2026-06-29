# Ground subagents as independent turns, composed via Task()

Status: accepted

Tracking: [#3](https://github.com/frizfealer/ground-check/issues/3) — implementation

## Context & decision

Subagents (launched via the Agent/Task tool) run in a fresh, isolated context and
their own transcript file (`<session>/subagents/agent-<agent_id>.jsonl`), entirely
outside the main agent's grounding loop: the main `UserPromptSubmit` injection and
`Stop` verification do not reach them. Today a subagent's answer is therefore
ungrounded and unverified, yet the orchestrator routinely relays its findings.

We decided to bring subagents into the protocol as **independent grounded turns**,
each grounded on its own evidence, composed by the orchestrator citing the
subagent — not by merging transcripts:

- **Inject** the grounding policy at subagent start via a `SubagentStart` hook,
  whose `hookSpecificOutput.additionalContext` is documented to add text to the
  subagent's context.
- **Verify** the subagent's citations at `SubagentStop` (which honors
  `{"decision":"block"}`), reusing the existing engine, blocking up to N times.
- **Key the loop guard on the per-subagent `agent_id`** from the hook payload,
  not the shared `session_id`, so concurrent subagents don't share one retry
  budget.

The orchestrator composes results by citing `Task(subagent) — what it reported`,
which stays **self-reported** (pass-through) at the main level. The seam between
the two grounded turns is exactly that `Task()` citation; the transcripts are
**never merged**.

## Considered options

- **Transcript merge / propagate** — have `collect()` ingest subagent transcripts
  so the main agent can cite `Read(foo.py)` for a file a *subagent* read.
  Rejected: it launders a subagent's observation into the main agent's direct-read
  claim — the exact provenance blur GroundCheck exists to prevent — and couples
  the two verifiers. (This also withdraws ADR 0001's original "collect()-ingest"
  unblock path.)
- **Independent turns + `Task()` boundary** (chosen) — each agent grounded on its
  own evidence; the `Task()` citation is the single trust seam, and it is
  trustworthy precisely because the subagent verified itself.

## Consequences

- Two new hooks to wire, symmetric to the main agent's `UserPromptSubmit` +
  `Stop`: `SubagentStart` (inject) and `SubagentStop` (verify).
- The loop guard must rekey from `session_id` to `agent_id` when invoked for a
  subagent (it is already per-session-keyed; this is a one-field change).
- `SubagentStop`'s `transcript_path` target is undocumented; the engine should
  build the subagent transcript path from `agent_id`
  (`<session>/subagents/agent-<agent_id>.jsonl`) and confirm empirically at
  implementation.
- **Inject and verify must ship together.** A subagent that was verified but never
  injected would be judged against a policy it never received and would flag
  `NO_CITATIONS` on everything. Never wire `SubagentStop` without `SubagentStart`.
- Fork-type subagents inherit the parent context, so their injection/verification
  semantics should be confirmed separately from fresh (non-fork) subagents.
