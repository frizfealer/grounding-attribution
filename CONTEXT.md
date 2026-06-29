# GroundCheck

GroundCheck is a Claude Code plugin that makes an agent's answers auditable: the
agent grades each claim and cites a machine-checkable source, and a hook
mechanically verifies those citations — by **input matching, never by meaning**.

## Language

The model has two orthogonal axes: the **claim grade** the agent assigns when
writing, and the **verifier outcome** the hook computes when checking. "Block" is
not a claim grade — it lives on the verifier axis.

### Claim grade — agent-assigned

**Claim**:
A single non-trivial assertion in a reply. Each must carry a grade.

**Verifiable**:
A claim that points at re-inspectable evidence outside the model. Carries a
citation atom.
_Avoid_: grounded, sourced

**Unverified**:
A claim nothing external backs (recalled, inferred, or guessed). Marked inline
with ⚠️, carries no atom, and is never blocked.
_Avoid_: not verifiable, ungrounded

**Citation atom**:
The smallest source token a verifiable claim cites — e.g. `Read(path:line)`,
`Bash(cmd)`, `MCP(server.tool)`. Each atom has an evidence sub-grade: **FS**
(filesystem), **Recorded** (recorded output), or **Conversation** (transcript).

### Verifier outcome — hook-computed, per atom

**Verified**:
An atom whose check held — `pointer-verified` for a filesystem atom matched
against disk and the session's reads, `call-verified` for a Bash atom matched to
a command actually run this session.

**Self-reported**:
An atom the verifier *cannot* mechanically check (Web/Task/MCP/Grep/Glob, or a
no-atom footnote such as `context — …`). Passed through on trust; no finding is
emitted. Not a claim grade.
_Avoid_: trusted, unchecked grade

**Finding code**:
The verifier's diagnosis when a check fails — e.g. `FABRICATED`,
`CONTENT_MISMATCH`, `AMBIGUOUS_COMMAND`, `command-not-found`, `NO_CITATIONS`.

**Warn / Block**:
What the hook does with a finding. Warn surfaces it; Block forces a fix. A
finding blocks iff its code is in `BLOCK_CODES`.

**BLOCK_CODES**:
The set of finding codes that block rather than warn — the single knob coupling
the two axes.

### Composition

**Grounded turn**:
A single agent turn — main agent *or* subagent — that has the policy injected at
its start and its citations verified at its stop. Subagents are grounded turns in
their own right; the orchestrator composes their results by citing
`Task(subagent)`, which stays **self-reported** at the main level. Transcripts of
distinct grounded turns are never merged.
