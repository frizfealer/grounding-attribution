#!/usr/bin/env python3
"""
grounding_spec.py — single source of truth for the grounding system.

The tool taxonomy — which tools exist, what grade they're in, how they're
cited, and whether the verifier mechanically checks them — is defined ONCE in
the TOOLS table below. Both consumers derive from it:

  - grounding-verifier.py imports FILE_CITE, RANGE_TOOLS, ALL_TOOLS, CHECKED.
  - source-attribution-inject.sh runs `python3 grounding_spec.py --emit-policy`
    to print the injection policy text.

So a tool can never be documented-but-unchecked or checked-but-undocumented:
add one row to TOOLS and the policy text, the citation regex, and the
session-read tracking all update together.

CLI:
  python3 grounding_spec.py --emit-policy   # print the injection policy
  python3 grounding_spec.py --check         # self-consistency assertions
"""

import os
import re
import sys

# --- grades ------------------------------------------------------------------
FS = "filesystem-checkable"     # re-inspect current disk
RECORDED = "recorded-output"    # transcript-checkable; not safely re-runnable
CONVERSATION = "conversation"   # transcript-checkable; the session itself


# --- the canonical tool table ------------------------------------------------
class Tool:
    """
    name    : Claude Code tool name as it appears in transcript tool_use blocks
    grade   : FS | RECORDED | CONVERSATION
    atom    : the [Source: <atom>] form the model should write; None = the tool
              is tracked for read-coverage but has no citation atom of its own
    checked : True if the verifier mechanically checks citations of this form
              (drives FILE_CITE)
    touch   : how collect() records a tool_use of this tool for the
              "was-it-opened-this-session" check: "ranges" | "all" | None
    blurb   : short gloss shown next to the atom in the policy
    """
    def __init__(self, name, grade, atom, checked, touch, blurb):
        self.name, self.grade, self.atom = name, grade, atom
        self.checked, self.touch, self.blurb = checked, touch, blurb


TOOLS = [
    # name           grade      atom                      checked touch     blurb
    Tool("Read",      FS, "Read(path:line)",       True,  "ranges", "a line / range / whole file"),
    Tool("Edit",      FS, "Edit(path:line)",       True,  "all",    "the resulting line after an edit"),
    Tool("Write",     FS, "Write(path:line)",      True,  "all",    "a line in a file you wrote"),
    Tool("MultiEdit", FS, "MultiEdit(path:line)",  True,  "all",    "a line after a multi-edit"),
    Tool("NotebookEdit", FS, None,                 False, "all",    None),  # touch-tracked only
    Tool("Grep",      FS, "Grep(pattern)",         False, None,     "a search re-runnable over the project files"),
    Tool("Glob",      FS, "Glob(pattern)",         False, None,     "a filename match over the project files"),
    Tool("Bash",      RECORDED, "Bash(<exact command>)", False, None, "the exact command (the verifier supplies the output)"),
    Tool("WebFetch",  RECORDED, "WebFetch(<url>)",       False, None, "+ what the page said"),
    Tool("WebSearch", RECORDED, "WebSearch(<query>)",    False, None, "+ the result you used"),
    Tool("Task",      RECORDED, "Task(<subagent>)",      False, None, "+ what it reported"),
    Tool("MCP",       RECORDED, "MCP(<server.tool>)",    False, None, "+ the value it returned"),
]

# --- derived sets (consumed by grounding-verifier.py) ------------------------
CHECKED     = [t.name for t in TOOLS if t.checked]            # Read,Edit,Write,MultiEdit
RANGE_TOOLS = [t.name for t in TOOLS if t.touch == "ranges"]  # Read
ALL_TOOLS   = [t.name for t in TOOLS if t.touch == "all"]     # Edit,Write,MultiEdit,NotebookEdit


def file_cite_regex():
    """Regex matching the citation atoms the verifier checks:
    <CheckedTool>(path[:line[-end]]).

    The (?<![A-Za-z]) left boundary prevents a short tool name from matching
    inside a longer one — without it, 'Edit' would match inside
    'NotebookEdit(...)' or 'MultiEdit(...)' and mislabel the citation."""
    alt = "|".join(re.escape(n) for n in CHECKED)
    return re.compile(
        r"(?<![A-Za-z])(" + alt + r")\(\s*([^()\s:]+?)\s*(?::\s*(\d+)(?:\s*-\s*(\d+))?)?\s*\)"
    )


FILE_CITE = file_cite_regex()


# All citable atom tokens (any grade), longest-first so the alternation prefers a
# full name. The verifier uses this to slice a footnote into per-atom ownership
# intervals: each backtick span is attributed to the atom immediately to its
# left, so a Bash atom bounds a Read atom's quote region (and vice-versa) and a
# filesystem span is never checked against a neighbour's source. Derived from
# TOOLS so it cannot drift from the taxonomy.
CITE_TOKENS = [t.name for t in TOOLS if t.atom]


def atom_head_regex():
    """Match the HEAD of any citation atom — a known tool token immediately
    followed by '(' — anywhere in a footnote. The (?<![A-Za-z]) boundary stops a
    short token matching inside a longer one (e.g. 'Edit' inside 'MultiEdit')."""
    alt = "|".join(re.escape(n) for n in sorted(CITE_TOKENS, key=len, reverse=True))
    return re.compile(r"(?<![A-Za-z])(" + alt + r")\s*\(")


ATOM_HEAD = atom_head_regex()


# --- citation style (cosmetic; the verifier matches the inner atom regardless) -
# Citations are FOOTNOTES. At the claim you drop a tinted MARKER — an inline-code
# span around a bracketed number, `[1]` — and at the END of the reply you DEFINE
# each number once as `[1]` <atom>. Identical sources reuse the same number, so
# each distinct source is defined exactly once. Unverified claims are marked
# inline with a yellow ⚠️ (no number — they have no re-inspectable atom).
# Change MARK_L / MARK_R / WARN_LEAD to restyle every marker in one place; the
# regexes below derive from them so the verifier can never drift from the policy.
CITE_TICK = "`"          # inline-code span delimiter (wraps a bare atom in lists)
MARK_L = "`["            # citation marker opener: code span + bracket
MARK_R = "]`"            # citation marker closer
WARN_LEAD = "⚠️ "        # yellow warning-sign lead for inline unverified claims
WARN_GLYPH = "⚠"    # base codepoint of ⚠ / ⚠️, for drift-free detection


def cite(atom):
    """Wrap a bare citation atom in the inline-code style, e.g. `Read(app.py:42)`.
    Used in the policy's grade lists; in a reply this atom follows a footnote
    number — `[1]` Read(app.py:42)."""
    return "%s%s%s" % (CITE_TICK, atom, CITE_TICK)


def mark(n):
    """Render an inline citation marker that sits at the claim, e.g. `[1]`."""
    return "%s%d%s" % (MARK_L, n, MARK_R)


def footnote(n, atom):
    """Render a footnote DEFINITION line for the end of the reply,
    e.g. `[1]` Read(app.py:42)."""
    return "%s%d%s %s" % (MARK_L, n, MARK_R, atom)


def warn(text):
    """Render an inline unverified marker, e.g. ⚠️ unverified — ...  (left outside
    a code span so the warning sign keeps its yellow emoji color)."""
    return "%s%s" % (WARN_LEAD, text)


# A footnote DEFINITION line: `[1]` <atom> at line start (an optional list bullet
# is tolerated). group(1) captures the footnote number, group(2) the atom — one
# pass yields both, so the verifier can label each listed citation with its number
# without re-scanning. This is the authoritative citation list — the verifier
# COUNTS these (one per distinct source) and reads each atom from them. Both
# CITE_CHIP_RE (count) and CITE_FULL_RE (per-atom listing) are this same pattern,
# so the count and the listing can never disagree.
_MARK_PAT = re.escape(MARK_L) + r"\d+" + re.escape(MARK_R)
FOOT_DEF_RE = re.compile(r"(?m)^[ \t]*(?:[-*+][ \t]+)?"
                         + _MARK_PAT.replace(r"\d+", r"(\d+)")
                         + r"[ \t]+(.+?)[ \t]*$")
CITE_CHIP_RE = FOOT_DEF_RE   # len(findall) = number of footnote definitions
CITE_FULL_RE = FOOT_DEF_RE   # finditer -> group(1) = number, group(2) = the cited atom

# Pull the leading tool token out of a footnote definition, for trust-tiering.
# (Defined for consistency; not imported by the verifier today.)
CITE_ATOM_RE = re.compile(_MARK_PAT + r"\s*([A-Za-z_]+)\s*\(")

# Detector for "does the answer cite anything at all" (the verifier's
# NO_CITATIONS heuristic): any citation marker, or the unverified warning sign.
# Derived from the markers above so it never drifts.
ANY_CITATION = re.compile(_MARK_PAT + r"|" + re.escape(WARN_GLYPH))

# Verbatim-quote convention (shared by Bash output and file-line content checks):
# the author wraps spans they claim are verbatim in backticks; the verifier checks
# exactly those against the real source. findall -> the inner text of each span.
BACKTICK_SPAN = re.compile(r"`([^`]+)`")


def is_checked(tool_name):
    """True if the verifier mechanically checks citations of this tool."""
    return tool_name in CHECKED


def tool_grade(tool_name):
    """Grade of a tool name, or None if unknown."""
    for t in TOOLS:
        if t.name == tool_name:
            return t.grade
    return None


# --- policy rendering (consumed by source-attribution-inject.sh) -------------
def _atom_lines(grade):
    out = []
    for t in TOOLS:
        if t.grade == grade and t.atom:
            out.append(("    " + cite(t.atom)).ljust(46) + (t.blurb or ""))
    return "\n".join(out)


def render_policy():
    text = _POLICY_TEMPLATE
    text = text.replace("%%FS_LIST%%", _atom_lines(FS))
    text = text.replace("%%RECORDED_LIST%%", _atom_lines(RECORDED))
    text = text.replace("%%CHECKED_LIST%%", "/".join(CHECKED))
    text = text.replace("%%CITE_CONTEXT%%", cite("context — user request / visible conversation"))
    text = text.replace("%%CITE_UNVERIFIED%%", warn("unverified — not checked against this session's files/tools"))
    text = text.replace("%%CITE_MARK%%", mark(1))
    text = text.replace("%%CITE_FOOT1%%", footnote(1, "Read(app.py:42)"))
    text = text.replace("%%CITE_FOOT2%%", footnote(2, "Bash(npm test) — all 12 pass"))
    return text


_POLICY_TEMPLATE = r"""[GROUNDING POLICY]

A claim is checkable only if it points at something OUTSIDE you that can be
re-inspected — a file location, a command plus its output, or a retrieved URL.
"Feels like memory / inference / recalled" is not verifiable and is NOT a
grounding label.

Sort every non-trivial claim into VERIFIABLE (it points at re-inspectable
evidence) or UNVERIFIED. The grade is set by what the source leaves behind, not
by the tool's name — so new tools and any mcp__server__tool slot into the grades
below:

Mark each claim inline with a code-span number, e.g. %%CITE_MARK%%, and DEFINE
each number once in a block at the END of the reply, as the number plus its
source atom (the verifier reads the LEADING atom):
    %%CITE_FOOT1%%
    %%CITE_FOOT2%%
Reuse the SAME number for the SAME source and define it once. A filesystem
footnote may add a short description; a recorded-output footnote, its output.
Mark an unverified claim inline with ⚠️ and no number (it has no atom).

VERIFIABLE / filesystem-checkable — the effect is on disk, re-readable NOW:
%%FS_LIST%%
  Cite a real path and a line that EXISTS, in a file you opened or changed THIS
  session with Read/Edit/Write. If grep or a search gave you the line, cite
  Grep(pattern) or Bash(<cmd>), NOT Read(path:line). After you edit a file this
  turn its line numbers shift — recite from the current file.
  You MAY also quote the cited line's content in backticks for a stronger check,
  but only if it is already in context — never re-read just to quote. A backtick
  span is matched LITERALLY: it must be a verbatim slice (exact characters,
  correct file, current line). A paraphrase, a rebuilt Class.attr, or a wrong
  file/range is a CONTENT_MISMATCH even when the claim is true. When unsure, drop
  the backticks — Read(path:line) alone is checked on file+line only.

VERIFIABLE / recorded-output — the call ran and you captured its result, but it
is not safely re-runnable. Cite the call and show the output you got — except
Bash, whose output the verifier supplies itself:
%%RECORDED_LIST%%

For Bash, cite the command, not the output — the exact command, verbatim as
typed: do not expand shell variables, re-quote, or paraphrase, or it reads as
command-not-found.
Matching is substring, so a distinctive verbatim slice is fine; never append
`...`, and a slice that also sits in another command you ran is
AMBIGUOUS_COMMAND. Do not paste the output — the verifier supplies the real
recorded output itself.

VERIFIABLE / conversation — recorded in the session transcript, checkable by
re-reading it (NOT a check against your source files):
    %%CITE_CONTEXT%%
Memory and project rules (CLAUDE.md, MEMORY.md) are INJECTED at session start —
you did NOT read them from disk this turn, and some may not be on disk. Cite
them as `context — memory: <fact>`, NOT Read(...). To make one filesystem-
checkable, actually Read the file this session, then cite Read(path:line). A
memory store reached over MCP is an MCP(...) citation.

Never invent a path, line, command, output, URL, tool result, or citation.

UNVERIFIED — nothing external backs it: recalled knowledge, inference, or guess
(you cannot tell these apart, so present none of them as fact). Mark it:
    %%CITE_UNVERIFIED%%
Inference is not a third source: a conclusion you reasoned to is unverified —
mark it — but cite the premises (Read(...), etc.) so they stay checkable.
Marking a claim unverified is encouraged and never penalized; the only thing
policed is a VERIFIED claim that does not check out. When in doubt, mark it
unverified — or better, verify it and cite that.

Rules:
1. Cite at the smallest unit that fully supports the claim (a paragraph, bullet,
   row, code change, or decision — not every sentence), tag kept next to it.
2. For a code or doc change, cite BOTH the trigger and the check that confirms it.
3. Number footnotes in first-use order and reuse a number for an identical atom,
   so each distinct source is defined exactly once.
4. If you cannot back a claim: drop it if the answer does not need it; if it does,
   keep it and mark it ⚠️ unverified (never silently delete something needed);
   best of all, verify it and cite that.

A downstream verifier re-checks your filesystem-checkable citations
(%%CHECKED_LIST%%) against the current files and what you opened or changed;
fabricated, out-of-range, or never-opened citations are flagged. It WARNS by
default (and can be set to bounce a flag back for a fix). Grep/Glob and
transcript citations are not auto-checked, so the absence of a flag is not
confirmation. Cite only what holds.

Trivial acknowledgments, transitions, and formatting text are exempt.
"""


def _check():
    rx = file_cite_regex()
    for t in TOOLS:
        if t.checked:
            assert t.atom, "%s is checked but has no citation atom" % t.name
            sample = t.atom.replace("path", "f.py").replace(":line", ":1")
            assert rx.search(sample), "regex fails to match checked atom %s" % t.atom
        if t.touch not in (None, "ranges", "all"):
            raise AssertionError("%s has bad touch=%r" % (t.name, t.touch))
    # every atom must be unique
    atoms = [t.atom for t in TOOLS if t.atom]
    assert len(atoms) == len(set(atoms)), "duplicate atoms"
    # footnote regexes round-trip: a rendered definition parses back to its atom,
    # is counted exactly once, and both it and the inline marker / unverified
    # warning trip the any-citation detector. A bare atom in a grade list must
    # still be matchable by FILE_CITE.
    sample_atom = "Read(f.py:1)"
    deff = footnote(1, sample_atom)
    assert FOOT_DEF_RE.search(deff), "footnote def regex fails on %r" % deff
    fm = CITE_FULL_RE.search(deff)
    assert fm.group(1) == "1", "footnote number capture wrong"
    assert fm.group(2) == sample_atom, "atom capture wrong"
    assert len(CITE_CHIP_RE.findall(deff)) == 1, "footnote not counted once"
    assert ANY_CITATION.search(mark(1)), "inline marker not detected"
    assert ANY_CITATION.search(warn("x")), "unverified warning not detected"
    assert rx.search(cite("Read(f.py:1)")), "FILE_CITE fails on code-span atom"
    # ATOM_HEAD finds the head of every citable atom (any grade), and the
    # (?<![A-Za-z]) boundary keeps a short token from matching inside a longer one.
    for t in TOOLS:
        if t.atom:
            assert ATOM_HEAD.search(t.name + "("), "ATOM_HEAD misses %s" % t.name
    assert ATOM_HEAD.search("MultiEdit(").group(1) == "MultiEdit", "ATOM_HEAD picked short token"
    # backticked-span extraction: the shared verbatim-quote convention
    spans = BACKTICK_SPAN.findall("Bash(x) — `Ran 5 tests`, `OK`")
    assert spans == ["Ran 5 tests", "OK"], "BACKTICK_SPAN extraction wrong: %r" % spans
    assert BACKTICK_SPAN.findall("no backticks here") == [], "BACKTICK_SPAN matched prose"
    print("grounding_spec OK — %d tools; checked=%s; range=%s; all=%s"
          % (len(TOOLS), ",".join(CHECKED), ",".join(RANGE_TOOLS), ",".join(ALL_TOOLS)))


# --- on/off toggle -----------------------------------------------------------
# A global flag file (not per-session) both halves consult: the --emit-policy
# injector emits nothing when off, and the verifier no-ops when off. Absent
# file = enabled (the plugin's default). Lives under the Claude config dir
# (~/.claude, or $CLAUDE_CONFIG_DIR) so the path is identical for the slash
# command and the hooks, which run as separate processes. NOT under
# CLAUDE_PLUGIN_DATA: that var is per-plugin-context and is not guaranteed to
# resolve to this plugin's dir across those processes, so a write from one and
# a read from another would miss each other and the toggle would never turn off.
_OFF_VALUES = ("off", "0", "false", "no")


def _flag_path():
    base = (os.environ.get("CLAUDE_CONFIG_DIR")
            or os.path.join(os.path.expanduser("~"), ".claude"))
    return os.path.join(base, "grounding-attribution", "grounding-enabled")


def is_enabled():
    """True unless the flag file says off. Absent/unreadable file = enabled."""
    try:
        with open(_flag_path()) as f:
            return f.read().strip().lower() not in _OFF_VALUES
    except OSError:
        return True


def set_enabled(on):
    """Write the global flag so both halves see the new state on their next run."""
    path = _flag_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("on" if on else "off")


def main(argv):
    if "--emit-policy" in argv:
        if is_enabled():
            sys.stdout.write(render_policy())
        return 0
    if "--check" in argv:
        _check()
        return 0
    if "--set" in argv:
        i = argv.index("--set")
        value = argv[i + 1].strip().lower() if i + 1 < len(argv) else ""
        if value in ("on", "enable", "enabled"):
            set_enabled(True)
        elif value in ("off", "disable", "disabled"):
            set_enabled(False)
        elif value == "toggle":
            set_enabled(not is_enabled())
        # "" or anything else -> report current state without changing it
        print("grounding attribution: %s" % ("on" if is_enabled() else "off"))
        return 0
    sys.stderr.write(
        "usage: grounding_spec.py [--emit-policy | --check | --set on|off|toggle]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
