"""Turn a plain-text script into an ordered list of speaking cues."""

import re

# ALICE: hello there
INLINE = re.compile(r"^\s*([^:\n]{1,48}?)\s*:\s*(.*)$")
# ALICE  (a name on its own line, screenplay style)
CUE_HEAD = re.compile(r"^\s*([A-Z0-9][A-Z0-9 '._\-]{0,40})(\s*\([^)]*\))?\s*$")
PARENTHETICAL = re.compile(r"\([^)]*\)")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+(?=[\"'\u201c\u2018(\[]?[A-Z0-9])")

DIRECTIVE = re.compile(r"^\s*[\[#]")

# INT. HOUSE - DAY  /  EXT./INT. CAR - NIGHT
SCENE_HEADING = re.compile(r"^\s*(INT|EXT|INT\.?/EXT|EXT\.?/INT|I/E)[.\s]", re.I)
# a run of leading parentheticals: (CONT'D) (V.O.) (beat) ...
PAREN_RUN = re.compile(r"^((?:\s*\([^)]*\))+)")
# (52) or (40s) after a name means a character introduction, i.e. action
AGE_PAREN = re.compile(r"^\(\s*\d+\s*(?:s|'s)?\s*\)$")
INITIALS = re.compile(r"^(?:[A-Z]\.)+$")
# XML forbids these, and SAPI refuses the whole line if one is present
ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
BOM = "\ufeff"


def _rows(script):
    """Split a script into rows, tolerating CRLF, a lone CR and a leading BOM."""
    text = str(script or "").replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith(BOM):          # a file Notepad saved: the BOM would
        text = text[1:]               # otherwise glue itself onto the first name
    return ILLEGAL.sub(" ", text).split("\n")


class Cue(object):
    __slots__ = ("index", "speaker", "text", "line_no")

    def __init__(self, index, speaker, text, line_no):
        self.index = index
        self.speaker = speaker
        self.text = text
        self.line_no = line_no

    def __repr__(self):
        return "Cue(%d, %r, %r)" % (self.index, self.speaker, self.text[:30])


def parse(script, default_speaker="NARRATOR", strip_parentheticals=True,
          split_sentences=False, max_chars=0, mode="auto", keep_action=False,
          scene_out=None):
    """Parse `script` into Cue objects.

    Three layouts are understood and `mode="auto"` picks between them:

      simple      `NAME: line`, or a name alone on a line with the dialogue
                  underneath. Un-attributed prose becomes `default_speaker`.
      screenplay  `NAME (CONT'D) dialogue all on one line`, as real scripts
                  come out of a word processor. Action and description are not
                  spoken by anyone, so by default they are dropped; pass
                  `keep_action=True` to give them to `default_speaker`.

    INT./EXT. scene headings are never dialogue. Pass a list as `scene_out` to
    collect them as (line_no, heading) pairs. Lines starting with # or [ are
    notes and are skipped either way.
    """
    cues = []
    current = None          # (speaker, [lines], line_no, open_for_more)
    lines = _rows(script)
    if mode == "auto":
        mode = detect_format(script)
    known = _screenplay_names(lines) if mode == "screenplay" else set()

    def flush():
        if current is None:
            return
        speaker, buf, line_no = current[:3]
        text = " ".join(x.strip() for x in buf if x.strip()).strip()
        if text:
            cues.append(Cue(len(cues), speaker, text, line_no))
        elif len(current) > 4 and current[4]:
            # An ALL-CAPS row we read as a speaker name turned out to have no
            # dialogue under it (an act break, a shouted line, a title card).
            # Speak it rather than dropping it - it is a real line of the file.
            cues.append(Cue(len(cues), default_speaker, current[4], line_no))

    for i, raw in enumerate(lines, 1):
        line = raw.rstrip()
        if not line.strip():
            flush()
            current = None
            continue
        if DIRECTIVE.match(line):
            continue
        if SCENE_HEADING.match(line):
            flush()
            current = None
            head, sep, tail = _split_heading(line)
            if scene_out is not None:
                scene_out.append((i, head))
            if sep:                           # "INT. HOUSE - DAY: words" - the
                current = (default_speaker, [tail], i, False, None)  # words stay
            continue

        if mode == "screenplay":
            hit = _screenplay_cue(line, known)
            if hit:
                flush()
                current = (hit[0], [hit[1]], i, False, None)
                continue
            if _is_upper_name(line) and CUE_HEAD.match(line) \
                    and _norm(CUE_HEAD.match(line).group(1)) in known:
                flush()                       # a bare name, dialogue underneath
                current = (_norm(CUE_HEAD.match(line).group(1)), [], i, True,
                           line.strip())
                continue
            if current is not None and current[3]:
                current = (current[0], current[1] + [line], current[2], True, None)
                continue
            flush()                           # action / description
            current = (default_speaker, [line], i, False, None) if keep_action else None
            continue

        m = INLINE.match(line)
        if m and _looks_like_name(m.group(1)):
            flush()
            current = (_norm(m.group(1)), [m.group(2)], i, False, None)
            continue

        if CUE_HEAD.match(line) and _looks_like_name(line) and _is_upper_name(line):
            flush()
            current = (_norm(CUE_HEAD.match(line).group(1)), [], i, True, line.strip())
            continue

        if current is None:
            current = (default_speaker, [line], i, True, None)
        else:
            current = (current[0], current[1] + [line], current[2], True, None)

    flush()

    out = []
    for c in cues:
        text = c.text
        if strip_parentheticals:
            text = PARENTHETICAL.sub(" ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        max_chars = max(0, int(max_chars or 0))
        chunks = [text]
        if split_sentences or max_chars:
            chunks = _chunk(text, max_chars or 0, split_sentences)
        for ch in chunks:
            out.append(Cue(len(out), c.speaker, ch, c.line_no))
    return out


def speakers(cues):
    """Unique speaker names in first-appearance order."""
    seen = []
    for c in cues:
        if c.speaker not in seen:
            seen.append(c.speaker)
    return seen


def scenes(script):
    """The INT./EXT. headings in `script`, as (line_no, heading) pairs."""
    out = []
    lines = _rows(script)
    for i, line in enumerate(lines, 1):
        if SCENE_HEADING.match(line):
            out.append((i, _split_heading(line)[0]))
    return out


def detect_format(script):
    """"screenplay" if the script puts the speaker and the line on one row."""
    lines = _rows(script)
    inline = 0
    for line in lines:
        m = INLINE.match(line)
        if m and _looks_like_name(m.group(1)) and not SCENE_HEADING.match(line):
            inline += 1
    same_row = len(_screenplay_names(lines, unique=False))
    if same_row <= inline:
        return "simple"
    # Three cues was too many to ask of a short script or a pasted excerpt: a
    # five-line scene would come back as one block of narration. Two is enough
    # on its own, and one will do when it is a real share of the script rather
    # than a stray capitalised word in a paragraph of prose.
    if same_row >= 2:
        return "screenplay"
    spoken = [l for l in lines
              if l.strip() and not SCENE_HEADING.match(l) and not DIRECTIVE.match(l)]
    return "screenplay" if same_row and len(spoken) <= 4 else "simple"


# ------------------------------------------------------- screenplay internals

def _name_token(tok):
    """Is `tok` a word that could be part of an ALL-CAPS speaker name?"""
    if len(tok) < 2 or any(c.islower() for c in tok):
        return False                          # "REEVE I know." -> name is REEVE
    if "(" in tok or ")" in tok or not any(c.isalpha() for c in tok):
        return False
    if tok.endswith((".", "!", "?", ",", ";", ":")) and not INITIALS.match(tok):
        return False                          # "VICTOR NO!" -> name is VICTOR
    return True


def _split_head(line, max_words=4):
    """"NORA (CONT'D) Hi there." -> ("NORA", "(CONT'D)", "Hi there.")"""
    toks = line.split()
    n = 0
    while n < len(toks) and n < max_words and _name_token(toks[n]):
        n += 1
    if not n:
        return None
    rest = line.split(None, n)
    rest = rest[n] if len(rest) > n else ""
    m = PAREN_RUN.match(rest)
    parens = m.group(1).strip() if m else ""
    if m:
        rest = rest[m.end():].lstrip()
    return " ".join(toks[:n]), parens, rest


def _is_action(parens, rest):
    """Prose, not dialogue: "REEVE stands..." or "SCOTT (28) shoulders out..."."""
    if not rest or rest[0].islower():
        return True
    return any(AGE_PAREN.match(p) for p in PARENTHETICAL.findall(parens))


# Only a speaker's line carries these, so the name in front of one is a
# speaker even when the dialogue itself is shouted in capitals.
SPEECH_MARKER = re.compile(r"\((?:cont'?d|v\.?o\.?|o\.?s\.?|o\.?c\.?|filtered|"
                           r"on (?:the )?phone|into (?:the )?phone)\)", re.I)


def _screenplay_names(lines, unique=True):
    """Speaker names from the rows we can read without knowing the cast yet.

    A row counts when the dialogue after the name is mixed case, which keeps
    ALL-CAPS act breaks and stage directions out of the cast list - or when the
    name is followed by a speech marker like (CONT'D), which nothing but a
    character cue ever carries.
    """
    found = []
    for line in lines:
        if SCENE_HEADING.match(line) or DIRECTIVE.match(line):
            continue
        h = _split_head(line)
        if not h or _is_action(h[1], h[2]):
            continue
        if any(c.islower() for c in h[2]) or SPEECH_MARKER.search(h[1] or ""):
            found.append(h[0])
    return set(found) if unique else found


def _screenplay_cue(line, known):
    """(speaker, dialogue) if `line` is a same-row cue, else None."""
    h = _split_head(line)
    if not h:
        return None
    name, parens, rest = h
    if _is_action(parens, rest):
        return None
    if name in known or any(c.islower() for c in rest):
        return name, rest
    # ALL-CAPS dialogue ("VICTOR THAT'S MY KID") - the name ran into the line,
    # so trust the longest prefix we already know is a character.
    toks = name.split()
    for k in range(len(toks) - 1, 0, -1):
        cand = " ".join(toks[:k])
        if cand in known:
            tail = " ".join(toks[k:] + ([parens] if parens else []) + [rest]).strip()
            return (cand, tail) if not _is_action("", tail) else None
    return None


def _split_heading(line):
    """("INT. HOUSE - DAY", ":", "spoken words") - the tail is normally empty."""
    head, sep, tail = line.partition(":")
    if sep and tail.strip():
        return head.strip(), sep, tail
    return line.strip(), "", ""


def _chunk(text, max_chars, split_sentences):
    max_chars = max(0, int(max_chars or 0))   # a negative width would never close
    sents = SENTENCE_SPLIT.split(text) if (split_sentences or max_chars) else [text]
    if not max_chars:
        return [s.strip() for s in sents if s.strip()]
    out, buf = [], ""
    for s in sents:
        s = s.strip()
        if not s:
            continue
        while len(s) > max_chars:                 # hard-wrap a monster sentence
            cut = s.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if not buf:
            buf = s
        elif len(buf) + 1 + len(s) <= max_chars:
            buf += " " + s
        else:
            out.append(buf)
            buf = s
    if buf:
        out.append(buf)
    return out


def _norm(name):
    # "NORA (CONT'D)" and "NORA" are the same person
    return re.sub(r"\s+", " ", PARENTHETICAL.sub(" ", name)).strip().upper()


def _looks_like_name(s):
    s = PARENTHETICAL.sub("", s).strip()
    if not s or len(s) > 40:
        return False
    if s.endswith((".", ",", "!", "?", ";")):
        return False
    return len(s.split()) <= 5


def _is_upper_name(s):
    letters = [c for c in PARENTHETICAL.sub("", s) if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)
