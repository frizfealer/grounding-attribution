#!/usr/bin/env python3
"""
grounding-verifier.py — Claude Code Stop hook.

Checks the INTEGRITY of filesystem-checkable citations — Read / Edit / Write /
MultiEdit (the tools whose effect lands on disk and is re-readable now) — in the
final assistant message, against two ground-truth sources:
  (a) the current filesystem   — does the cited file/line actually exist now?
  (b) the session transcript   — was that file actually opened or written this
                                 session?

Citations to NON-filesystem tools are intentionally not checked here:
  - Grep / Glob are filesystem-checkable in principle (a search can be re-run
    deterministically), but the re-run is NOT implemented yet — they currently
    pass through unchecked.
  - Bash / WebFetch / WebSearch / Task / MCP are "recorded-output": not safely
    or deterministically re-runnable, so the deterministic core leaves them
    alone. They can only be transcript-cross-checked, never re-executed.

What it deliberately does NOT do: judge whether the prose semantically matches
the code at the cited line. A deterministic hook can't, and faking that would
reintroduce the exact self-report problem this setup exists to avoid. Honest
scope: citation integrity (is the pointer real, in range, and actually
read/written), NOT truth of the claim. See OPTIONAL ESCALATION at the bottom
for semantic checking via a second model call.

Wire to: Stop, PreToolUse/AskUserQuestion (Stop does not fire when Claude pauses
to ask the user a question), and SubagentStop if you use subagents.

Findings (whether one BLOCKS or merely WARNS is set by BLOCK_CODES below;
default is warn-only — nothing blocks until you opt a code in):
  FABRICATED   cited a file that does not exist on disk
  BAD_LINE     cited a line beyond the file's current length (often staleness)
  UNREAD_FILE  cited a file not opened/written this session
  UNREAD_LINE  opened the file but never the cited line range
  NO_CITATIONS substantial answer with zero [Source: ...] tags

A blocking finding tells Claude it claimed a checkable source that does not
check out, and the hook forces a fix. Warnings are reported and allowed (e.g. a
cited file may have been read in a resumed prior session).

Transcript JSON shapes vary across Claude Code versions; parsing here is
defensive. If reads aren't being detected, print the raw lines and adjust the
tool_use extraction to match your version's schema.
"""

import hashlib
import json
import os
import re
import sys
import tempfile
import time

# Single source of truth for the tool taxonomy. Both this verifier and the
# injection policy derive from grounding_spec.py, so the set of citations
# checked here cannot drift from the set the policy documents. The spec sits
# alongside this file; make it importable regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from grounding_spec import (  # noqa: E402
    ALL_TOOLS,
    ANY_CITATION,
    BACKTICK_SPAN,
    CITE_FULL_RE,
    FILE_CITE,
    RANGE_TOOLS,
)

# ---- policy -----------------------------------------------------------------
# Which finding codes actually BLOCK Claude (force a fix) vs. merely warn.
# Default: warn-only. Nothing blocks until you opt in — start here, watch the
# warnings, and only promote a code to blocking once you trust it on your repo.
#   e.g.  BLOCK_CODES = {"FABRICATED"}   # block only on truly nonexistent files
# Never put a code here that punishes honesty (there isn't one — unverified
# claims are not findings at all; they pass through untouched by design).
BLOCK_CODES = set()

# When True, list each pointer-verified citation (✓ file:line) beneath the
# summary line. Asserted (unchecked) and failed citations are NOT listed —
# failures still surface in the "Grounding check:" section, and every tier is
# counted in the summary line regardless. Set False to show the summary only.
LIST_CITATIONS = True


def resolve_path(path, cwd, read_keys):
    """Find the real file a citation points at, leniently, so a correct
    citation written relative to the repo root (when cwd is a subdir) is not
    mislabeled FABRICATED. Tries, in order: as-is under cwd, under the git
    root, and finally a unique basename match among files actually read."""
    expanded = os.path.expanduser(path)  # resolve a cited ~/… path like the shell would
    cands = [expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded)]
    git_root = cwd
    cur = cwd
    while cur and cur != os.path.dirname(cur):
        if os.path.isdir(os.path.join(cur, ".git")):
            git_root = cur
            break
        cur = os.path.dirname(cur)
    cands.append(os.path.join(git_root, expanded))
    for c in cands:
        if os.path.isfile(c):
            return c
    base = os.path.basename(expanded)
    hits = [k for k in read_keys if os.path.basename(k) == base and os.path.isfile(k)]
    if len(hits) == 1:
        return hits[0]
    return None


def load_input():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def iter_transcript(path):
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except Exception:
        return


# ---- transcript settle ------------------------------------------------------
# The Stop hook can fire while Claude Code is still appending the current turn to
# the transcript JSONL, and Claude Code documents NO guarantee that the final
# assistant message is flushed before the hook runs (the payload hands us only
# transcript_path — there is no final-message field to read instead). Reading
# immediately then yields a partial view: a half-written last line is skipped as
# unparseable (see iter_transcript) and late assistant entries are simply absent,
# so the citation check scores only a fragment of the answer — or, commonly,
# nothing at all. Wait for the file to look settled before reading. Bounded by
# SETTLE_MAX_WAIT so the hook never hangs.
SETTLE_MAX_WAIT = 2.0     # seconds: hard ceiling on total wait
SETTLE_INTERVAL = 0.08    # seconds between polls
SETTLE_STABLE_POLLS = 2   # consecutive unchanged+parseable polls that mean "done"


def _last_nonempty_line_parses(path):
    """True if the file's last non-empty line is valid JSON — i.e. the writer is
    not mid-append on the final line. Full scan, matching iter_transcript's
    style; transcripts are line-delimited and small enough that the cost over a
    handful of polls is negligible."""
    try:
        last = ""
        with open(os.path.expanduser(path), "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if s:
                    last = s
        if not last:
            return False
        json.loads(last)
        return True
    except Exception:
        return False


def wait_for_stable_transcript(path,
                               max_wait=SETTLE_MAX_WAIT,
                               interval=SETTLE_INTERVAL,
                               stable_polls=SETTLE_STABLE_POLLS):
    """Best-effort: block until the transcript stops growing AND its last line
    parses, for `stable_polls` consecutive checks, or until `max_wait` elapses.
    Returns fast when the file is already settled (the common case)."""
    p = os.path.expanduser(path)
    if not path or not os.path.isfile(p):
        return
    deadline = time.time() + max_wait
    last_size = -1
    stable = 0
    while time.time() < deadline:
        try:
            size = os.path.getsize(p)
        except OSError:
            size = -1
        if size >= 0 and size == last_size and _last_nonempty_line_parses(p):
            stable += 1
            if stable >= stable_polls:
                return
        else:
            stable = 0
        last_size = size
        time.sleep(interval)


def _msg(entry):
    return entry.get("message", entry) if isinstance(entry, dict) else {}


def blocks_of(entry):
    msg = _msg(entry)
    content = msg.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def role_of(entry):
    msg = _msg(entry)
    return entry.get("role") or msg.get("role") or entry.get("type")


def _is_user_prompt(entry):
    """A genuine human turn — used to bound 'the current answer'. Tool results
    also arrive as role=user (with tool_result blocks); those are NOT prompts."""
    if role_of(entry) != "user":
        return False
    blocks = blocks_of(entry)
    has_text = any(
        isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
        for b in blocks
    )
    has_tool_result = any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks
    )
    return has_text and not has_tool_result


def _tool_result_text(block):
    """Best-effort plain text of a tool_result block's content (a string, or a
    list of {type:'text', text:...} parts). '' if none."""
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(
            part.get("text", "")
            for part in c
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def collect(transcript_path, cwd):
    """Return (reads, bash_outputs, last_assistant_text).

    reads: { realpath: "ALL" | list[(start,end|None)] }  -- lines opened this session
    bash_outputs: list[str] -- the text of every Bash tool_result this session,
      for verbatim-quote checking of Bash citations.
    last_assistant_text: ALL assistant text of the current turn, concatenated.
      A single answer is split across many assistant entries interleaved with
      tool calls, so we accumulate every assistant text chunk produced since the
      last genuine user prompt — not just the final fragment.
    """
    reads = {}
    bash_outputs = []
    bash_ids = set()
    answer_parts = []

    def real(p):
        return os.path.realpath(p if os.path.isabs(p) else os.path.join(cwd, p))

    for entry in iter_transcript(transcript_path):
        if _is_user_prompt(entry):
            answer_parts = []  # new human turn -> start of a fresh answer
        for b in blocks_of(entry):
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "tool_use":
                name = b.get("name")
                inp = b.get("input") or {}
                if name == "Bash":
                    bid = b.get("id")
                    if bid:
                        bash_ids.add(bid)
                p = inp.get("file_path") or inp.get("path")
                if not p:
                    continue
                rp = real(p)
                if name in RANGE_TOOLS:
                    offset = inp.get("offset")
                    limit = inp.get("limit")
                    if offset is None and limit is None:
                        reads[rp] = "ALL"
                    elif reads.get(rp) != "ALL":
                        start = int(offset) if offset else 1
                        end = start + int(limit) - 1 if limit else None
                        reads.setdefault(rp, []).append((start, end))
                elif name in ALL_TOOLS:
                    # the file was written/changed this session -> touched in full
                    reads[rp] = "ALL"
            elif btype == "tool_result":
                if b.get("tool_use_id") in bash_ids:
                    t = _tool_result_text(b)
                    if t:
                        bash_outputs.append(t)
        if role_of(entry) == "assistant":
            txt = "".join(
                b.get("text", "")
                for b in blocks_of(entry)
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if txt.strip():
                answer_parts.append(txt)

    last_assistant_text = "\n".join(answer_parts)
    return reads, bash_outputs, last_assistant_text


def line_was_read(reads, rp, line):
    cov = reads.get(rp)
    if cov is None:
        return None  # file not read at all
    if cov == "ALL" or line is None:
        return True
    return any(s <= line <= (e if e is not None else 10**9) for (s, e) in cov)


def file_line_count(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except Exception:
        return None


def read_cited_text(path, start, end):
    """Text of the cited line/range (1-indexed, inclusive), or the whole file
    when no line was cited. None on read error (then the content check is
    skipped — never a false mismatch)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        return None
    if start is None:
        return "\n".join(lines)
    lo = max(1, start)
    hi = end if (end is not None and end >= lo) else lo
    return "\n".join(lines[lo - 1:hi])


# A footnote whose leading atom is a Bash citation: its backticked output spans
# are checked against the session's recorded Bash output.
_BASH_ATOM_RE = re.compile(r"^\s*Bash\s*\(")


def _bash_output_portion(body):
    """The part of a Bash footnote AFTER the Bash(<cmd>) atom — its output
    description. Backticks INSIDE the command are not claimed output, so skip
    them by finding the matching close paren of Bash(. Falls back to the whole
    body if the parens are unbalanced."""
    open_i = body.find("(")
    if open_i == -1:
        return body
    depth = 0
    for j in range(open_i, len(body)):
        c = body[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return body[j + 1:]
    return body


def _classify_recorded(body, bash_outputs):
    """Tier a non-filesystem footnote. Bash footnotes get verbatim-quote checking
    against recorded output; everything else (Web/Task/MCP/Grep/Glob/context) is
    'asserted'. Returns (tier, finding_or_None)."""
    if not _BASH_ATOM_RE.match(body):
        return "asserted", None
    spans = BACKTICK_SPAN.findall(_bash_output_portion(body))
    if not spans:
        return "asserted", None  # nothing claimed verbatim
    missing = [sp for sp in spans if not any(sp in out for out in bash_outputs)]
    if missing:
        return "BASH_OUTPUT_MISMATCH", (
            "BASH_OUTPUT_MISMATCH",
            "%s — quoted output not found in this session's recorded Bash "
            "output: %r (stale quote, misquote, or resumed session)"
            % (body, missing[0]),
        )
    return "output-verified", None


def _tally(cited):
    """Count citation tiers for the trust summary."""
    tiers = [t for _, t in cited]
    fail = {"FABRICATED", "BAD_LINE", "UNREAD_FILE", "UNREAD_LINE"}
    mismatch = {"CONTENT_MISMATCH", "BASH_OUTPUT_MISMATCH"}
    return {
        "pointer_verified": tiers.count("pointer-verified"),
        "output_verified": tiers.count("output-verified"),
        "asserted": tiers.count("asserted"),
        "failed": sum(1 for t in tiers if t in fail),
        "mismatched": sum(1 for t in tiers if t in mismatch),
    }


def verify(text, reads, bash_outputs, cwd):
    """Verify ONLY the footnote definitions — the authoritative citation list.

    Each footnote is judged by its LEADING atom:
      - a filesystem atom (Read/Edit/Write/MultiEdit) is checked against disk and
        the session reads; if its pointer holds and it backticks line content,
        that span is checked against the cited line/range (CONTENT_MISMATCH on a
        miss) — see Task 4.
      - a Bash atom has its backticked output spans checked against the union of
        recorded Bash outputs (output-verified / BASH_OUTPUT_MISMATCH).
      - anything else is "asserted".
    Spans the author did not backtick are never checked, so paraphrase never
    false-positives.
    """
    findings = []  # (code, message)
    cited = []     # (display, tier) per footnote, in order, de-duplicated
    seen = set()

    for cm in CITE_FULL_RE.finditer(text or ""):
        body = cm.group(1).strip()
        m = FILE_CITE.match(body)  # a checked filesystem atom at the START?
        if not m:
            if body in seen:
                continue
            seen.add(body)
            tier, finding = _classify_recorded(body, bash_outputs)
            if finding:
                findings.append(finding)
            cited.append((body, tier))
            continue

        atom = m.group(0)
        if atom in seen:
            continue
        seen.add(atom)
        path, s, e = m.group(2), m.group(3), m.group(4)
        line = int(s) if s else None
        end = int(e) if e else None
        before = len(findings)

        abspath = resolve_path(path, cwd, list(reads.keys()))
        if abspath is None:
            findings.append(
                ("FABRICATED",
                 f"{atom} — no such file found "
                 f"(checked cwd, git root, and read files)")
            )
            cited.append((atom, "FABRICATED"))
            continue

        if line is not None:
            n = file_line_count(abspath)
            if n is not None and line > n:
                findings.append(
                    ("BAD_LINE",
                     f"{atom} — file now has only {n} lines "
                     f"(stale citation, or wrong line)")
                )
                cited.append((atom, "BAD_LINE"))
                continue

        rp = os.path.realpath(abspath)
        read_state = line_was_read(reads, rp, line)
        if read_state is None:
            findings.append(
                ("UNREAD_FILE",
                 f"{atom} — cited but not opened this session "
                 f"(ok if resumed from a prior session)")
            )
        elif read_state is False:
            findings.append(
                ("UNREAD_LINE",
                 f"{atom} — file opened, but this line was never in a read range")
            )

        if len(findings) != before:
            cited.append((atom, findings[-1][0]))  # pointer failure
            continue

        # Pointer holds. Opt-in content check: if the footnote backticks the cited
        # line content, confirm each span is verbatim at the cited line/range.
        spans = BACKTICK_SPAN.findall(body[m.end():])
        if spans:
            cited_text = read_cited_text(abspath, line, end)
            if cited_text is not None:
                missing = [sp for sp in spans if sp not in cited_text]
                if missing:
                    findings.append(
                        ("CONTENT_MISMATCH",
                         f"{atom} — quoted content not found at the cited "
                         f"line/range: {missing[0]!r}")
                    )
                    cited.append((atom, "CONTENT_MISMATCH"))
                    continue
        cited.append((atom, "pointer-verified"))

    stats = _tally(cited)

    if not ANY_CITATION.search(text or "") and len((text or "").strip()) > 600:
        findings.append(("NO_CITATIONS", "Substantial answer with no citations"))
    return findings, stats, cited


def summary_line(stats):
    """Honest one-line trust summary. 'pointer-verified'/'output-verified' mean
    the pointer/quote holds — NOT that the claim's prose is correct."""
    parts = []
    if stats.get("pointer_verified"):
        parts.append("%d pointer-verified" % stats["pointer_verified"])
    if stats.get("output_verified"):
        parts.append("%d output-verified" % stats["output_verified"])
    if stats.get("asserted"):
        parts.append("%d asserted (unchecked)" % stats["asserted"])
    if stats.get("failed"):
        parts.append("%d failed" % stats["failed"])
    if stats.get("mismatched"):
        parts.append("%d content/output mismatch" % stats["mismatched"])
    if not parts:
        return ""
    return "Citations: " + " · ".join(parts)


def report(findings, stats=None, cited=None):
    lines = []
    if stats:
        s = summary_line(stats)
        if s:
            lines.append(s)
    if LIST_CITATIONS and cited:
        # List only pointer-verified citations. Asserted (unchecked) items are
        # omitted as noise; failed citations are omitted here because they already
        # appear, with their reason, in the "Grounding check:" section below. The
        # summary line above still carries the counts for every tier.
        for atom, tier in cited:
            if tier in ("pointer-verified", "output-verified"):
                lines.append("  ✓ %s  [%s]" % (atom, tier))
    if findings:
        lines.append("Grounding check:")
        for code, msg in findings:
            mark = "X" if code in BLOCK_CODES else "!"
            lines.append(f"  [{mark}] {code}: {msg}")
    return "\n".join(lines)


# ---- loop guard -------------------------------------------------------------
# Independent of stop_hook_active (which is documented but known to mis-propagate
# when system reminders interleave). State is a per-session file, so it survives
# across the separate hook processes within a turn.
MAX_FORCED_CONTINUATIONS = 3  # hard ceiling on blocks per task
STATE_RESET_SECONDS = 600  # gap that counts as a new task -> reset
# Persist loop-guard state in the plugin's data dir when running as a plugin
# (it survives plugin updates); fall back to the system temp dir otherwise.
STATE_DIR = os.path.join(
    os.environ.get("CLAUDE_PLUGIN_DATA") or tempfile.gettempdir(),
    "grounding-verifier-state",
)


def _state_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "nosession")
    return os.path.join(STATE_DIR, safe + ".json")


def _load_state(session_id):
    try:
        with open(_state_path(session_id)) as f:
            return json.load(f)
    except Exception:
        return {"count": 0, "fingerprint": None, "ts": 0}


def _save_state(session_id, state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(_state_path(session_id), "w") as f:
            json.dump(state, f)
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


def should_block(session_id, stop_active, blocking):
    """Decide whether to actually block, with three independent guards.
    Returns (block: bool, note: str) — note explains a *declined* block."""
    if not blocking:
        _clear_state(session_id)  # clean turn -> reset the task
        return False, ""
    if stop_active:  # honor the flag when it IS set
        return False, "already in a forced continuation (stop_hook_active)"

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


def main():
    data = load_input()
    transcript_path = data.get("transcript_path", "")
    cwd = data.get("cwd") or os.getcwd()
    session_id = data.get("session_id", "")
    stop_active = bool(data.get("stop_hook_active"))
    # Which event invoked us. The verifier is wired to BOTH Stop (turn end) and
    # PreToolUse/AskUserQuestion (Claude is asking the user a question — Stop does
    # NOT fire at that pause). The warn-only systemMessage is identical for both,
    # but the BLOCK output schema differs: Stop uses {"decision":"block"} while
    # PreToolUse must deny via hookSpecificOutput.permissionDecision.
    event = data.get("hook_event_name", "")

    # The Stop hook may fire before Claude Code finishes writing this turn to the
    # transcript; wait for it to settle so we score the whole answer, not a
    # half-written fragment (see wait_for_stable_transcript).
    wait_for_stable_transcript(transcript_path)

    reads, bash_outputs, text = collect(transcript_path, cwd)
    findings, stats, cited = verify(text, reads, bash_outputs, cwd)
    blocking = [f for f in findings if f[0] in BLOCK_CODES]

    block, note = should_block(session_id, stop_active, blocking)

    if block:
        reason = (
            report(findings, stats, cited)
            + "\n\nFix or remove the flagged citations (re-Read the file, "
            "correct the line, or mark the claim unverified), then finish."
        )
        if event == "PreToolUse":
            # Deny the AskUserQuestion call so Claude fixes its citations before
            # asking; Stop's {"decision":"block"} schema is ignored by PreToolUse.
            out = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        else:
            out = {"decision": "block", "reason": reason}
        print(json.dumps(out))
        sys.exit(0)

    # Emit a report if there are findings OR there's a non-empty trust summary
    # (so a clean answer with citations still gets the positive tier line, and
    # silence unambiguously means "no citations to report on").
    has_summary = bool(summary_line(stats))
    if findings or has_summary:
        msg = report(findings, stats, cited)
        if blocking and note:
            # we WOULD have blocked but a guard declined — say so, so the human knows
            msg += f"\n(not blocking: {note})"
        print(json.dumps({"systemMessage": msg}))
    sys.exit(0)


if __name__ == "__main__":
    main()

# OPTIONAL ESCALATION (semantic match) ----------------------------------------
# To check that the PROSE actually matches the cited code — not just that the
# pointer is real — add a second pass that, for each surviving citation, reads
# the cited line range from disk and asks a model "does <claim> follow from
# <code>?". That is the only way to catch a real, in-range, actually-read
# citation that the model still mischaracterized. Keep it OUT of the
# deterministic core above so the cheap structural checks always run.
