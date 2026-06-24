# Verifier-Supplies-Output for Bash Citations — Design

**Status:** Approved (all decisions resolved) — ready for an implementation plan
**Date:** 2026-06-23
**Related:** `docs/superpowers/specs/2026-06-21-bash-citation-verification-design.md` (the verbatim-backtick convention this revisits)

## Problem

The current Bash-citation convention asks the model to **re-type the recorded
output** into a footnote, in backticks, and the verifier checks that string is a
verbatim substring of the recorded output.

In practice the model **compresses** output when it writes the footnote:
abbreviates with an ellipsis, collapses many lines into one, summarizes
("Ran 25 tests … OK"), drops whitespace. The *claim* is true (the tests really
passed), but the *typed evidence* is no longer a literal slice → the check
fails. These are self-inflicted failures: the verifier is correct, but it is
penalizing the model for transcribing output it shouldn't have to transcribe.

Observed this session: the majority of `BASH_OUTPUT_MISMATCH` warnings were of
this kind (ellipsis truncation, command quoted instead of output, reworded
summary) — not real fabrications.

## Goal

Eliminate the "model changed the output" failure class **without weakening
fabrication detection** — ideally strengthening it.

## Core idea

**The model should not transcribe output the system already has.**

The verifier reads the recorded Bash output from the transcript. So let the
model cite the **command only**; the verifier sources the output itself. The
model stops being the transcriber, so there is nothing for it to get wrong.

## Design

### Citation format (Bash)

- A Bash citation is the **exact command**: `Bash(<exact command>)`, with an
  optional ` — note` suffix for human readability (the verifier reads only up to
  the separator). See Resolved decision 3.
- The model MAY add a plain-prose summary inline — un-checked, as today
  ("Prose you do not backtick is never checked").
- The model does **not** paste output. There is no model-typed output tier
  (Resolved decision 2). To call attention to a specific value, the model states
  it in prose; the verifier renders the real output for the reader to confirm.

### What the verifier does

1. Parse the `Bash(<cmd>)` atom from the footnote (up to any ` — note`).
2. **Match** it to a recorded Bash call in the transcript by command string
   (match any occurrence — Resolved decision 1).
3. If found → **call-verified** (the command provably ran). The verifier
   surfaces the real recorded output (most-recent matching run) in **its own
   report**, noting "(ran N times; showing latest)" when several matched.
4. If not found → **flag** `command-not-found` (cited a command never run),
   unless a session boundary makes it unverifiable (Resolved decision 5).

### Verification tiers (Bash)

| Tier | Meaning |
|---|---|
| `call-verified` | command found in transcript (base tier) |
| `command-not-found` (failure) | cited command absent from transcript |

There is deliberately **no model-typed output tier**, so the verifier can never
emit `BASH_OUTPUT_MISMATCH` for a Bash citation. The only Bash failure is the
honest one: citing a command that was never run.

### Realism: a hook cannot edit the model's message

A Stop / PreToolUse hook cannot rewrite the assistant message; it can only emit
its **own** output (as the warnings already do). So "verifier supplies output"
means: the verifier's report shows the real recorded output next to each Bash
citation. The reader sees the model's command-only citation **plus** the
verifier's ground-truth output. The output shown is sourced by the tool, not by
the model's retyping.

## Relationship to Read tiers (symmetry)

Read and Bash share a two-tier shape:

| | Read (filesystem, live) | Bash (recorded-output, historical) |
|---|---|---|
| **Pointer / existence tier** | `pointer-verified` — path:line exists & was opened | `call-verified` — command is in the transcript |
| **Content tier (optional)** | backtick the line text → checked vs the file | ~~`output-verified`~~ — dropped |

**The pointer tier is the real symmetry.** In both, the model supplies only a
reference (path:line / command) and the verifier independently *fetches the
artifact* — no model-typed content is trusted. The only difference is artifact
liveness, matching the policy's two grades: Read's file is *re-readable now*;
Bash's output is *captured but not re-runnable*. This redesign simply makes
Bash's base tier behave like Read's already does.

**The content tier is symmetric in structure but not in practice** — which is
why we keep it for Read and drop it for Bash:

- **Read content** = one short line → copied faithfully, low false-positive
  rate, and useful (catches right-line/wrong-text, and drift if the file changed
  since it was read).
- **Bash output** = often long / multi-line → reflexively summarized → high
  false-positive rate, and redundant now that the verifier renders the output.

**Principle:** for any re-fetchable artifact, the pointer tier is always the
base; an optional content tier earns its keep *only* when the content is short
enough to transcribe faithfully **and** adds reader value. True for a file line;
false for a screen of command output.

*(Consistency note: "verifier supplies content" could be pushed onto Read too —
it can re-read the line — but Read's content tier isn't causing pain, so YAGNI.)*

## What this removes / keeps

- **Removes:** ellipsis / line-join / summarize mismatches — the model no longer
  types output, so it cannot mistype it. `BASH_OUTPUT_MISMATCH` goes away
  entirely.
- **Keeps:** fabrication detection — a cited command must exist in the
  transcript (`command-not-found` otherwise). Read's tiers are untouched.
- **Strengthens:** the evidence the reader sees comes from the record, not from
  the model.

## Resolved decisions

1. **Command ran multiple times → RESOLVED.** Two separate concerns:
   - *Existence (verification):* match **any** recorded call whose command equals
     the cited command → `call-verified`. The claim "this command ran" holds if
     it ran at least once.
   - *Output rendering:* show the **most recent** matching invocation, annotated
     "(ran N times; showing latest)" when several matched. The model no longer
     types output, so the rendered output is informational; most-recent + count
     is the honest default. Precise disambiguation (model adds a ` — note`) is a
     future option — YAGNI now.
2. **Optional verbatim-backtick output tier → RESOLVED: drop it.** It is the
   exact mechanism behind this session's false positives; it is redundant once
   the verifier renders the real output; and removing it makes
   `BASH_OUTPUT_MISMATCH` structurally impossible. A future *soft, non-failing*
   "highlight this span" hint (highlight if it matches, silent otherwise — never
   a failure) is possible but YAGNI.
3. **Exact-command rule → RESOLVED.** This is a *citation-format (attribution)
   rule*: the model cites the **exact command**, not a loose label like
   `Bash(git merge + verify)`, with an optional ` — note` suffix for readability
   (verifier reads only up to the separator). Known cost: brittle to rephrased
   commands (flag reorder, different quoting); normalized matching is the
   fallback if that bites.
4. **Scope → RESOLVED: Bash first.** Read tiers untouched; `WebFetch` /
   `WebSearch` / `Task` / `MCP` can adopt the same pattern later.
5. **Compaction / resumed sessions → RESOLVED (grounded).** `/compact` does NOT
   drop prior calls — it compresses only the context window, while the on-disk
   transcript keeps appending to the **same session file** (verified by
   inspecting this session's transcript, which still contains both pre-compaction
   commit shas and post-compaction tool outputs). So compaction within a session
   needs no special handling. The real risk is **cross-session** citations: each
   session has its own transcript file, so a command that actually ran in a
   *different/earlier* session (or a `--resume` that began a new file) won't be
   found. Guard: when a citation can't be matched **and** there is a session
   boundary, downgrade `command-not-found` to "unverifiable (other/resumed
   session)" rather than a hard failure.

## Non-goals

- Warn-only vs enforce ("bounce back for a fix") — separate configuration.
- Changes to filesystem tiers (`Read` / `Edit` / `Write` / `MultiEdit`).

## Risks

- **Command-matching ambiguity / loose labels** → mitigated by the
  exact-command rule (decision 3).
- **Less readable citations** (command instead of a friendly label) → mitigated
  by the optional ` — note` suffix and the verifier-rendered output.
