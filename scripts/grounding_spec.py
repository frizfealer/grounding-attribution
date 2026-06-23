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
    Tool("Bash",      RECORDED, "Bash(<exact command>)", False, None, "+ the output"),
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
# is tolerated). group(1) captures the atom. This is the authoritative citation
# list — the verifier COUNTS these (one per distinct source) and reads each atom
# from them. Both CITE_CHIP_RE (count) and CITE_FULL_RE (per-atom listing) are
# this same pattern, so the count and the listing can never disagree.
_MARK_PAT = re.escape(MARK_L) + r"\d+" + re.escape(MARK_R)
FOOT_DEF_RE = re.compile(r"(?m)^[ \t]*(?:[-*+][ \t]+)?" + _MARK_PAT + r"[ \t]+(.+?)[ \t]*$")
CITE_CHIP_RE = FOOT_DEF_RE   # len(findall) = number of footnote definitions
CITE_FULL_RE = FOOT_DEF_RE   # finditer -> group(1) = the cited atom

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

Only one thing makes a claim checkable: a pointer to an artifact that exists
OUTSIDE you and can be re-inspected — a file location, a command plus its
output, or a retrieved URL. Whether a claim "feels like" memory, inference, or
recalled context is not externally verifiable, so those are NOT grounding
labels and must not be used as if they were.

Sort every non-trivial claim into VERIFIABLE or UNVERIFIED. A claim is
verifiable only if it points at evidence someone else can re-inspect. Verifiable
evidence comes in grades, by what the source leaves behind — the tool's NAME
doesn't matter, only whether its result can be re-inspected, so new tools and any
mcp__server__tool slot into the same grades:

Write each citation as a numbered FOOTNOTE. At the claim, drop a tinted marker —
an inline-code span around a bracketed number, e.g. %%CITE_MARK%% — then DEFINE
each number once, in a short block at the END of your reply, as the number plus
its source atom:
    %%CITE_FOOT1%%
    %%CITE_FOOT2%%
Reuse the SAME number for the SAME source: if two claims both rest on %%CITE_MARK%%,
both carry %%CITE_MARK%% and it is defined once. Begin a footnote with its atom —
the verifier reads the LEADING atom — after which a filesystem footnote may add a
short description and a recorded-output footnote its captured output. Mark
unverified claims inline with a yellow ⚠️ — no number,
since they have no re-inspectable atom (shown below). The verifier reads the atom
in each footnote regardless of styling.

VERIFIABLE / filesystem-checkable — the effect is on disk, re-readable NOW:
%%FS_LIST%%
  Cite a real path, a line that exists, a file you opened/changed THIS session
  with the Read/Edit/Write tools — if grep or a search gave you the line instead,
  that's a Grep(pattern) or Bash(<cmd>) citation, NOT Read(path:line).
  You MAY also quote the exact cited line content in backticks for a stronger
  check — but ONLY if it is already in your context; never re-read a file just
  to quote it.

VERIFIABLE / recorded-output — the call happened and you captured its result,
but it is NOT safely or deterministically re-runnable. Cite the call AND show
the relevant output you actually got:
%%RECORDED_LIST%%

For a Bash citation, wrap the exact output you are claiming in backticks (the
output, not the command); the verifier confirms each backticked span is a
verbatim substring of the recorded output (output-verified). Prose you do not
backtick is never checked.

VERIFIABLE / conversation — recorded in the session transcript (the JSONL on
disk at transcript_path, which the hook already reads), so it is checkable by
re-reading that transcript — the same mechanism that confirms a recorded-output
call happened, NOT a check against your source files:
    %%CITE_CONTEXT%%
Memory and project rules (CLAUDE.md at any level, or a memory store such as
MEMORY.md) are INJECTED into your context at session start — you did NOT read
them from disk this turn, and some may not exist on disk at all. Cite them as
`context — memory: <fact>` (injected context), NOT Read(...). They are real
files, so to make the citation filesystem-checkable, actually open the file with
the Read tool this session and THEN cite Read(path:line). (A memory store
reached over MCP is an MCP(...) citation.)

Never invent a path, line, command, output, URL, tool result, or citation.

UNVERIFIED — no external artifact backs it. This is everything from your own
weights or reasoning — recalled general knowledge, inferences, and guesses
alike; you cannot tell those apart and must not pretend to. Mark it; never
present it as fact:
    %%CITE_UNVERIFIED%%
Inference is not a third source. A conclusion you REASONED to is unverified as a
conclusion — mark it — but cite the premises you reasoned from (Read(...), etc.).
That keeps the premises checkable even though the inference step is not, which is
strictly more honest than a bare "inference" label that asks the reader to trust
the leap.
Marking a claim unverified is ENCOURAGED and costs you nothing — it is the
honest move and is never penalized. The only thing policed is a claim dressed
up as VERIFIED that does not check out. So when in doubt, mark it unverified
rather than reaching for a citation you cannot stand behind. Better still: go
read the file or run the check and turn it into VERIFIABLE.

Rules:
1. Cite at the smallest readable unit that fully supports the claim — per
   paragraph, bullet, table row, code change, or decision. Not per sentence.
2. Keep the source tag adjacent to the claim it supports.
3. For a code or doc change, cite BOTH:
     - the trigger  (the Read / Bash / test / issue that motivated it)
     - the check    (the Read / Bash / test result confirming it works)
4. Number footnotes in first-use order and reuse a number whenever the source
   atom is identical, so every distinct source is defined exactly once at the end
   — that reuse IS the footnote system, not an extra step.
5. If you cannot back a claim, choose by whether it is load-bearing: drop it if
   the answer does not need it; if the answer DOES need it, keep it but mark it
   ⚠️ unverified (never silently delete something the user needs); best of
   all, take the step that verifies it and cite that. These do not conflict —
   it is one rule applied to claims of different importance.

A downstream verifier re-checks your filesystem-checkable citations
(%%CHECKED_LIST%%) against the current files and against what you
actually opened or changed. Fabricated, out-of-range, or never-opened citations
are flagged. By default it only WARNS; it can be configured to bounce a flagged
citation back to you for a fix. (Grep/Glob and the transcript-checkable
citations are not auto-checked yet — so the absence of a flag on those is not
confirmation.) Cite only what holds.

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
    assert CITE_FULL_RE.search(deff).group(1) == sample_atom, "atom capture wrong"
    assert len(CITE_CHIP_RE.findall(deff)) == 1, "footnote not counted once"
    assert ANY_CITATION.search(mark(1)), "inline marker not detected"
    assert ANY_CITATION.search(warn("x")), "unverified warning not detected"
    assert rx.search(cite("Read(f.py:1)")), "FILE_CITE fails on code-span atom"
    # backticked-span extraction: the shared verbatim-quote convention
    spans = BACKTICK_SPAN.findall("Bash(x) — `Ran 5 tests`, `OK`")
    assert spans == ["Ran 5 tests", "OK"], "BACKTICK_SPAN extraction wrong: %r" % spans
    assert BACKTICK_SPAN.findall("no backticks here") == [], "BACKTICK_SPAN matched prose"
    print("grounding_spec OK — %d tools; checked=%s; range=%s; all=%s"
          % (len(TOOLS), ",".join(CHECKED), ",".join(RANGE_TOOLS), ",".join(ALL_TOOLS)))


def main(argv):
    if "--emit-policy" in argv:
        sys.stdout.write(render_policy())
        return 0
    if "--check" in argv:
        _check()
        return 0
    sys.stderr.write("usage: grounding_spec.py [--emit-policy | --check]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
