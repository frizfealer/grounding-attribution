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
  command-not-found  cited a Bash command not run this session (warn-only)
  AMBIGUOUS_COMMAND  cited Bash slice matched 2+ distinct commands (warn-only)

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
    ATOM_HEAD,
    BACKTICK_SPAN,
    CHECKED,
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
    """Return (reads, bash_calls, last_assistant_text).

    reads: { realpath: "ALL" | list[(start,end|None)] }  -- lines opened this session
    bash_calls: list[(command, output)] -- every Bash call this session, pairing
      the command string with the text of its tool_result, for command-presence
      checking of Bash citations.
    last_assistant_text: ALL assistant text of the current turn, concatenated.
      A single answer is split across many assistant entries interleaved with
      tool calls, so we accumulate every assistant text chunk produced since the
      last genuine user prompt — not just the final fragment.
    """
    reads = {}
    bash_calls = []
    bash_cmd = {}  # tool_use_id -> command string, to pair with its result
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
                        bash_cmd[bid] = inp.get("command", "")
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
                tid = b.get("tool_use_id")
                if tid in bash_cmd:
                    bash_calls.append((bash_cmd[tid], _tool_result_text(b)))
        if role_of(entry) == "assistant":
            txt = "".join(
                b.get("text", "")
                for b in blocks_of(entry)
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if txt.strip():
                answer_parts.append(txt)

    last_assistant_text = "\n".join(answer_parts)
    return reads, bash_calls, last_assistant_text


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


def _match_close_paren(body, open_idx):
    """Index just AFTER the ')' that closes the '(' at open_idx, by depth count
    so nested parens inside a Bash command don't end the atom early. None if
    unbalanced."""
    depth = 0
    for j in range(open_idx, len(body)):
        c = body[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    return None


def _atoms_in(body):
    """Every citation atom in a footnote body, in order, each with the character
    span it occupies. An atom owns the backtick spans that fall after its
    closing ')' and before the next atom — so a Bash atom bounds a Read atom's
    quote region (and vice-versa) and every span is attributed to the atom on
    its left. Returns dicts:
        {token, text, start, end, path, line, end_line}
    path/line/end_line are filled only for filesystem atoms (Read/Edit/Write/
    MultiEdit, parsed via FILE_CITE)."""
    out = []
    for hm in ATOM_HEAD.finditer(body):
        open_idx = hm.end() - 1            # the '(' the head matched
        close = _match_close_paren(body, open_idx)
        if close is None:
            continue                       # unbalanced parens -> skip this atom
        rec = {
            "token": hm.group(1), "text": body[hm.start():close],
            "start": hm.start(), "end": close,
            "path": None, "line": None, "end_line": None,
        }
        if hm.group(1) in CHECKED:
            fm = FILE_CITE.match(rec["text"])
            if fm:
                rec["path"] = fm.group(2)
                rec["line"] = int(fm.group(3)) if fm.group(3) else None
                rec["end_line"] = int(fm.group(4)) if fm.group(4) else None
        out.append(rec)
    return out


def _bash_paren_span(body):
    """(open_index, close_index) of the OUTER Bash(...) parens, or (None, None)
    if not a balanced Bash atom. Balanced scan so a command containing parens
    (subshells, $(...)) is handled."""
    open_i = body.find("(")
    if open_i == -1:
        return None, None
    depth = 0
    for j in range(open_i, len(body)):
        if body[j] == "(":
            depth += 1
        elif body[j] == ")":
            depth -= 1
            if depth == 0:
                return open_i, j
    return None, None


def _normalize_cmd(s):
    """Collapse whitespace runs to single spaces and strip, so cosmetic spacing
    differences never cause a false command-not-found."""
    return " ".join(s.split())


def _cited_command(body):
    """The command text inside the outer Bash(...) of a footnote body, with any
    wrapping backticks removed and whitespace normalized. '' if not a Bash atom."""
    open_i, close_i = _bash_paren_span(body)
    if open_i is None:
        return ""
    inner = body[open_i + 1:close_i].strip().strip("`").strip()
    return _normalize_cmd(inner)


def _check_fs_atom(a, owned, reads, cwd):
    """(tier, finding|None, None) for a filesystem atom: pointer integrity first,
    then — if the atom owns backtick spans — a verbatim content check of each
    span against the cited line/range."""
    atom, path, line, end = a["text"], a["path"], a["line"], a["end_line"]
    abspath = resolve_path(path, cwd, list(reads.keys()))
    if abspath is None:
        return "FABRICATED", ("FABRICATED",
            f"{atom} — no such file found "
            f"(checked cwd, git root, and read files)"), None
    if line is not None:
        n = file_line_count(abspath)
        if n is not None and line > n:
            return "BAD_LINE", ("BAD_LINE",
                f"{atom} — file now has only {n} lines "
                f"(stale citation, or wrong line)"), None
    rp = os.path.realpath(abspath)
    read_state = line_was_read(reads, rp, line)
    if read_state is None:
        return "UNREAD_FILE", ("UNREAD_FILE",
            f"{atom} — cited but not opened this session "
            f"(ok if resumed from a prior session)"), None
    if read_state is False:
        return "UNREAD_LINE", ("UNREAD_LINE",
            f"{atom} — file opened, but this line was never in a read range"), None
    if owned:
        cited_text = read_cited_text(abspath, line, end)
        if cited_text is not None:
            missing = [sp for sp in owned if sp not in cited_text]
            if missing:
                return "CONTENT_MISMATCH", ("CONTENT_MISMATCH",
                    f"{atom} — quoted content not found at the cited "
                    f"line/range: {missing[0]!r}"), None
    return "pointer-verified", None, None


def _check_bash_atom(atom_text, bash_calls):
    """(tier, finding|None, detail|None) for a Bash atom, by COMMAND PRESENCE:
    the command inside Bash(...) must be a substring of a command actually run
    this session. detail for a call-verified citation is (n_runs, latest_output);
    the verifier renders that output, the model never transcribes it. Backtick
    spans after a Bash atom are description, not output — never checked.

    A slice that is a substring of TWO OR MORE distinct commands run this session
    cannot identify which run it refers to, so it is flagged AMBIGUOUS_COMMAND
    (warn-only) instead of call-verified — cite a longer, distinctive slice. The
    SAME command run repeatedly is one distinct command, not ambiguous."""
    cited = _cited_command(atom_text)
    if not cited:
        return "asserted", None, None
    matches = [(cmd, out) for (cmd, out) in bash_calls if cited in _normalize_cmd(cmd)]
    if not matches:
        return "command-not-found", (
            "command-not-found",
            "%s — no Bash call with this command was recorded this session "
            "(misquoted command, or it ran in a different/resumed session)"
            % atom_text), None
    distinct = {_normalize_cmd(cmd) for (cmd, _out) in matches}
    if len(distinct) >= 2:
        return "ambiguous-command", (
            "AMBIGUOUS_COMMAND",
            "%s — this slice is a substring of %d different commands run this "
            "session, so it cannot identify which run it refers to; cite a "
            "longer, distinctive slice" % (atom_text, len(distinct))), None
    return "call-verified", None, (len(matches), matches[-1][1])


def _tally(cited):
    """Count citation tiers for the trust summary."""
    tiers = [c[1] for c in cited]
    fail = {"FABRICATED", "BAD_LINE", "UNREAD_FILE", "UNREAD_LINE", "command-not-found"}
    return {
        "pointer_verified": tiers.count("pointer-verified"),
        "call_verified": tiers.count("call-verified"),
        "asserted": tiers.count("asserted"),
        "failed": sum(1 for t in tiers if t in fail),
        "mismatched": tiers.count("CONTENT_MISMATCH"),
        "ambiguous": tiers.count("ambiguous-command"),
    }


def verify(text, reads, bash_calls, cwd):
    """Verify ONLY the footnote definitions — the authoritative citation list.

    A footnote may carry MORE THAN ONE atom. Each atom is judged on its own and
    owns the backtick spans that fall between its ')' and the next atom (the
    nearest atom to a span's left), so a Bash atom bounds a Read atom's quote
    region and vice-versa:
      - a filesystem atom (Read/Edit/Write/MultiEdit) is checked against disk and
        the session reads; if its pointer holds and it owns backtick spans, each
        is checked against ITS cited line/range (CONTENT_MISMATCH on a miss).
      - a Bash atom is checked by command presence against the session's recorded
        Bash calls (call-verified / command-not-found); the command lives inside
        its OWN parens, so trailing spans are description, never checked.
      - anything else (Web/Task/MCP/Grep/Glob, or a footnote with no atom) is
        "asserted".
    Spans the author did not backtick are never checked, so paraphrase never
    false-positives; and a filesystem span is only ever checked against the
    source of the atom that owns it, never a neighbour's.
    """
    findings = []  # (code, message)
    cited = []     # (display, tier, detail) per footnote, in order, de-duplicated
    seen = set()

    for cm in CITE_FULL_RE.finditer(text or ""):
        body = cm.group(1).strip()
        atoms = _atoms_in(body)
        if not atoms:
            # No atom at all (e.g. a `context — …` footnote): asserted whole.
            if body not in seen:
                seen.add(body)
                cited.append((body, "asserted", None))
            continue
        spanms = list(BACKTICK_SPAN.finditer(body))
        for i, a in enumerate(atoms):
            display = a["text"]
            if display in seen:
                continue
            seen.add(display)
            # This atom owns the backtick spans between its ')' and the next
            # atom, so a filesystem span is never checked against a neighbour's
            # source. (Bash is checked by the command inside its OWN parens, not
            # by trailing spans, so any spans it owns are ignored here.)
            lo = a["end"]
            hi = atoms[i + 1]["start"] if i + 1 < len(atoms) else len(body)
            owned = [sm.group(1) for sm in spanms if lo <= sm.start() < hi]
            if a["path"] is not None:            # a parsed filesystem atom
                tier, finding, detail = _check_fs_atom(a, owned, reads, cwd)
            elif a["token"] == "Bash":
                tier, finding, detail = _check_bash_atom(display, bash_calls)
            else:                                # Web/Task/MCP/Grep/Glob/…
                tier, finding, detail = "asserted", None, None
            if finding:
                findings.append(finding)
            cited.append((display, tier, detail))

    stats = _tally(cited)

    if not ANY_CITATION.search(text or "") and len((text or "").strip()) > 600:
        findings.append(("NO_CITATIONS", "Substantial answer with no citations"))
    return findings, stats, cited


def _render_output(n_runs, output, max_lines=12, max_chars=800):
    """An indented block showing recorded Bash output in the verifier's own
    report. The verifier is the SOURCE of this text (not the model), so trimming
    is safe and is never a 'mismatch'."""
    trimmed = output if len(output) <= max_chars else output[-max_chars:]
    rows = trimmed.splitlines() or [""]
    if len(rows) > max_lines:
        rows = ["…(earlier output trimmed)"] + rows[-max_lines:]
    head = ("ran %d×; latest output:" % n_runs) if n_runs > 1 else "recorded output:"
    return "\n".join(["      " + head] + ["      | " + r for r in rows])


def summary_line(stats):
    """Honest one-line trust summary. 'pointer-verified'/'call-verified' mean
    the pointer/command holds — NOT that the claim's prose is correct."""
    parts = []
    if stats.get("pointer_verified"):
        parts.append("%d pointer-verified" % stats["pointer_verified"])
    if stats.get("call_verified"):
        parts.append("%d call-verified" % stats["call_verified"])
    if stats.get("asserted"):
        parts.append("%d asserted (unchecked)" % stats["asserted"])
    if stats.get("failed"):
        parts.append("%d failed" % stats["failed"])
    if stats.get("mismatched"):
        parts.append("%d content mismatch" % stats["mismatched"])
    if stats.get("ambiguous"):
        parts.append("%d ambiguous" % stats["ambiguous"])
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
        # List only pointer-verified and call-verified citations. Asserted
        # (unchecked) items are omitted as noise; failed citations are omitted here
        # because they already appear, with their reason, in the "Grounding check:"
        # section below. The summary line above still carries the counts for every tier.
        for atom, tier, detail in cited:
            if tier in ("pointer-verified", "call-verified"):
                lines.append("  ✓ %s  [%s]" % (atom, tier))
                if detail:
                    lines.append(_render_output(*detail))
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

    reads, bash_calls, text = collect(transcript_path, cwd)
    findings, stats, cited = verify(text, reads, bash_calls, cwd)
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
