"""ScriptVoice - an offline AI film studio in a single file.

A local LLM casts the film from your premise, judges each character against the
plot, and a local ComfyUI renders their portraits, a 360 turnaround, their voices,
and finally the movie itself. Nothing leaves the machine.

    python ScriptVoice.py

Requires: Python 3.8+ with tkinter (both ship with python.org installers).
Optional: Pillow for nicer image scaling and animated turnaround GIFs;
          ffmpeg on PATH to mux the final movie.mp4.

This file is generated from the scriptvoice/ package by build_single.py -
edit there and rebuild, or edit here and keep it, whichever you prefer.
"""


# ==========================================================================
# audio: WAV stitching with the standard library only.
# ==========================================================================

"""WAV stitching with the standard library only."""

import os
import wave


class AudioError(RuntimeError):
    pass


def trailing_silence(frames, params, threshold=0.02):
    """How many frames of near-silence sit at the end of a clip.

    Speech engines pad every utterance. Left alone that pad stacks on top of
    the gap between lines, so a film ends up mostly dead air.
    """
    if params.sampwidth != 2 or not frames:
        return 0
    step = params.nchannels * 2
    limit = int(32768 * threshold)
    end = len(frames) - (len(frames) % step)
    quiet = 0
    for i in range(end - step, -1, -step):
        sample = int.from_bytes(frames[i:i + 2], "little", signed=True)
        if abs(sample) > limit:
            break
        quiet += 1
    return quiet


def trim_tail(frames, params, keep_seconds=0.12):
    """Drop a clip's trailing pad, keeping a short natural tail."""
    quiet = trailing_silence(frames, params)
    keep = int(params.framerate * keep_seconds)
    drop = max(0, quiet - keep)
    if not drop:
        return frames
    step = params.nchannels * params.sampwidth
    return frames[:len(frames) - drop * step]


def concat_wavs(paths, dest, gap_seconds=0.35, trim=False):
    """Join WAV files into `dest`, inserting silence between them.

    With `trim`, each clip's trailing silence is cut back first so the pause
    between lines is the one asked for rather than the engine's pad plus it.
    """
    paths = [p for p in paths if p and os.path.exists(p)]
    if not paths:
        raise AudioError("Nothing to stitch together.")

    params = None
    with wave.open(paths[0], "rb") as w:
        params = w.getparams()

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    gap = b""
    with wave.open(dest, "wb") as out:
        out.setparams(params)
        if gap_seconds > 0:
            frames = int(params.framerate * gap_seconds)
            gap = b"\x00" * (frames * params.sampwidth * params.nchannels)
        for i, p in enumerate(paths):
            with wave.open(p, "rb") as w:
                pw = w.getparams()
                if (pw.nchannels, pw.sampwidth, pw.framerate) != (
                        params.nchannels, params.sampwidth, params.framerate):
                    raise AudioError(
                        "%s is %dch/%dbit/%dHz but the first clip is %dch/%dbit/%dHz. "
                        "Make the workflow's save node use one consistent format."
                        % (os.path.basename(p), pw.nchannels, pw.sampwidth * 8, pw.framerate,
                           params.nchannels, params.sampwidth * 8, params.framerate))
                frames = w.readframes(w.getnframes())
                out.writeframes(trim_tail(frames, params) if trim else frames)
            if gap and i < len(paths) - 1:
                out.writeframes(gap)
    return dest


def duration(path):
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0


# ==========================================================================
# runtime: Estimating how long a script will run once it is spoken.
# ==========================================================================

"""Estimating how long a script will run once it is spoken.

Drafted by the local model through the bionic skill, then corrected: the
line-count maths rounded the wrong way and the runtime formatter dropped
minutes whenever the hours part was non-zero.
"""

import math

WORDS_PER_MINUTE = 150
SENTENCE_PAUSE = 0.35
MIN_LINE_SECONDS = 0.5
FALLBACK_LINE_SECONDS = 4.0


def estimate_line(text, wpm=WORDS_PER_MINUTE):
    """Seconds needed to speak one line, including its sentence pauses."""
    words = len(str(text).split())
    seconds = (words / float(wpm)) * 60.0
    seconds += sum(1 for c in str(text) if c in ".!?") * SENTENCE_PAUSE
    return max(seconds, MIN_LINE_SECONDS)


def estimate_total(cues, gap_seconds=SENTENCE_PAUSE, wpm=WORDS_PER_MINUTE):
    """Seconds for a whole list of cues, with a gap between each one."""
    if not cues:
        return 0.0
    spoken = sum(estimate_line(c.text, wpm) for c in cues)
    return spoken + (len(cues) - 1) * float(gap_seconds)


def format_runtime(seconds):
    """'1h 32m 10s', dropping the hours part only when there are none."""
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%dh %dm %ds" % (hours, minutes, secs)
    if minutes:
        return "%dm %ds" % (minutes, secs)
    return "%ds" % secs


def target_gap(cues, target_minutes, gap_seconds=SENTENCE_PAUSE, wpm=WORDS_PER_MINUTE):
    """How much more dialogue a script needs to reach a target runtime.

    `lines_needed` rounds up: asking for 90 minutes and landing at 89 would
    defeat the point of asking.
    """
    current = estimate_total(cues, gap_seconds, wpm)
    target = float(target_minutes) * 60.0
    if cues:
        average = sum(estimate_line(c.text, wpm) for c in cues) / float(len(cues))
    else:
        average = FALLBACK_LINE_SECONDS
    average = max(average, MIN_LINE_SECONDS)
    short_by = target - current
    needed = int(math.ceil(short_by / (average + float(gap_seconds)))) if short_by > 0 else 0
    return {
        "current_seconds": current,
        "target_seconds": target,
        "average_line_seconds": average,
        "lines_needed": needed,
    }


# ==========================================================================
# speech: Speaking lines with the voices Windows already has.
# ==========================================================================

"""Speaking lines with the voices Windows already has.

This is the fallback for when ComfyUI has no text-to-speech nodes installed:
it drives the built-in SAPI engine through PowerShell. The voices are robotic,
but they are free, instant, and always there - enough to hear a cut of the film
before committing to a neural TTS setup.

Started from a local-model draft (bionic) and corrected: the SSML root element
must be <speak>, the voice name has to be interpolated into the PowerShell
command rather than left as an unset shell variable, negative pitch cannot be
written "+-20%", and the temp file needs a text mode to accept an encoding.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

SPEECH_AVAILABLE_CACHE = None
VOICE_CACHE = None

RATE_RANGE = (-10, 10)
PITCH_RANGE = (-50, 50)
PROBE = ("Add-Type -AssemblyName System.Speech; "
         "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
         "$s.Dispose(); Write-Output ok")
LIST = ("Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "foreach ($v in $s.GetInstalledVoices()) "
        "{ Write-Output ($v.VoiceInfo.Name + '|' + $v.VoiceInfo.Gender) }; "
        "$s.Dispose()")

# The casting step writes a vocal range for every character. Two SAPI voices
# can't act, but they can at least be the right register.
RANGES = [
    ("bass", "Male", -15),
    ("baritone", "Male", -8),
    ("tenor", "Male", 10),
    ("countertenor", "Male", 16),
    ("contralto", "Female", -18),
    ("alto", "Female", -10),
    ("mezzo", "Female", 0),
    ("soprano", "Female", 14),
]
GENDER_WORDS = [("Female", ("female", "woman", "feminine", "she ")),
                ("Male", ("male", "man", "masculine", "he "))]


class SpeechError(RuntimeError):
    pass


def available():
    """True when Windows can speak for us. The answer is remembered."""
    global SPEECH_AVAILABLE_CACHE
    if SPEECH_AVAILABLE_CACHE is not None:
        return SPEECH_AVAILABLE_CACHE
    ok = False
    if sys.platform.startswith("win") and _powershell():
        try:
            out = _powershell_run(PROBE, timeout=30)
            ok = "ok" in out
        except SpeechError:
            ok = False
    SPEECH_AVAILABLE_CACHE = ok
    return ok


def voices():
    """The SAPI voice names installed on this machine, best-effort."""
    return [v["name"] for v in voice_table()]


def voice_table():
    """[{name, gender}] for every installed voice."""
    global VOICE_CACHE
    if VOICE_CACHE is not None:
        return VOICE_CACHE
    table = []
    if available():
        try:
            for line in _powershell_run(LIST, timeout=30).splitlines():
                line = line.strip()
                if not line:
                    continue
                name, _, gender = line.partition("|")
                table.append({"name": name.strip(), "gender": gender.strip() or "Neutral"})
        except SpeechError:
            table = []
    VOICE_CACHE = table
    return table


def speak_to_wav(text, dest_path, voice=None, rate=0, pitch=0):
    """Speak `text` into a WAV file and return its path.

    `rate` is SAPI's own -10..10 speed. `pitch` is a percentage applied through
    SSML, because the System.Speech API exposes no pitch property.
    """
    if not available():
        raise SpeechError(
            "Windows speech isn't available here, so the system voice can't be used.")
    if not str(text).strip():
        raise SpeechError("Nothing to speak.")
    text = _printable(text)
    if voice:
        installed = voices()
        if installed and voice not in installed:
            # SAPI silently falls back to the default voice here, which would
            # ship a whole film in one voice without ever saying so.
            raise SpeechError(
                "This machine has no voice called %r.\nInstalled voices: %s"
                % (voice, ", ".join(installed)))

    rate = _clamp(rate, RATE_RANGE)
    pitch = _clamp(pitch, PITCH_RANGE)
    ssml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xml:lang="en-US">'
        '<prosody pitch="%s%%">%s</prosody>'
        '</speak>' % (_signed(pitch), escape(str(text))))

    dest_path = os.path.abspath(dest_path)
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)

    handle, temp_path = tempfile.mkstemp(suffix=".xml")
    os.close(handle)
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(ssml)

        select = ("$s.SelectVoice('%s'); " % _ps_quote(voice)) if voice else ""
        command = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "%s"
            "$s.Rate = %d; "
            "$s.SetOutputToWaveFile('%s'); "
            "$s.SpeakSsml((Get-Content -LiteralPath '%s' -Raw -Encoding UTF8)); "
            "$s.Dispose()"
            % (select, rate, _ps_quote(dest_path), _ps_quote(temp_path)))
        _powershell_run(command, timeout=180)
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    # isfile, not exists: pointed at a directory the size check would pass and
    # a folder would be handed on to the stitcher as if it were audio.
    if not os.path.isfile(dest_path) or os.path.getsize(dest_path) < 128:
        raise SpeechError("Windows produced no audio for: %.60s" % text)
    return dest_path


def stable_seed(*parts):
    """A seed derived from who the character is, not from this process.

    The obvious spelling is hash(), but Python randomises string hashing per
    run, so a character would be given a different voice on every launch and
    no cached clip would ever be re-used.
    """
    blob = "|".join(str(p) for p in parts).encode("utf-8")
    return int(hashlib.sha1(blob).hexdigest()[:8], 16) % (2 ** 31 - 1)


def assign_voice(seed, voice_list=None, hint=""):
    """Pick a repeatable voice, speed and pitch for one character.

    `hint` is the character's written voice description. If it names a vocal
    range ("warm dry American alto"), that decides which installed voice and
    what base pitch to use - casting a baritone as the female voice is worse
    than any amount of variety. The seed only adds the variation on top, so the
    same character always sounds the same.
    """
    table = voice_table() if voice_list is None else _as_table(voice_list)
    if not table:
        return {"voice": None, "rate": 0, "pitch": 0}
    seed = abs(int(seed))

    wanted_gender, base_pitch = _range_of(hint)
    pool = [v for v in table if v["gender"] == wanted_gender] if wanted_gender else []
    if not pool:
        pool = table
        if wanted_gender:                       # no voice of that gender installed
            base_pitch += 12 if wanted_gender == "Female" else -12
    n = len(pool)
    # Pitch carries the variety, because it costs nothing in listenability.
    # Rate does not: SAPI +3 measures about 275 words a minute, and a lead with
    # a third of the film's lines at that speed is unlistenable. Speed only
    # moves when the character was written as fast or slow.
    jitter = ((seed // n) % 7 - 3) * 7          # -21..21
    return {
        "voice": pool[seed % n]["name"],
        "rate": _pace_of(hint, seed, n),
        "pitch": _clamp(base_pitch + jitter, PITCH_RANGE),
    }


FAST_WORDS = ("fast", "quick", "rapid", "clipped", "brisk", "hurried", "urgent")
SLOW_WORDS = ("slow", "unhurried", "measured", "drawl", "deliberate", "languid", "weary")


def _pace_of(hint, seed, n):
    """Speaking rate, taken from the character description where it says one.

    Matched on whole words: "unhurried" contains "hurried", and reading that as
    a fast talker gets the pacing exactly backwards.
    """
    words = set(re.findall(r"[a-z]+", str(hint or "").lower()))
    if words & set(FAST_WORDS):
        return 2
    if words & set(SLOW_WORDS):
        return -2
    return (seed // (n * 7)) % 3 - 1            # -1..1: audible, never a gabble


def _range_of(hint):
    """(gender, base pitch) implied by a written voice description."""
    low = " %s " % str(hint or "").lower()
    for word, gender, pitch in RANGES:
        if word in low:
            return gender, pitch
    for gender, words in GENDER_WORDS:
        if any(w in low for w in words):
            return gender, 0
    return None, 0


def _as_table(voice_list):
    if voice_list and isinstance(voice_list[0], dict):
        return voice_list
    known = {v["name"]: v["gender"] for v in (VOICE_CACHE or [])}
    return [{"name": v, "gender": known.get(v, "Neutral")} for v in voice_list]


def describe_voice(settings):
    """A short human label for an assigned voice."""
    if not settings or not settings.get("voice"):
        return "system voice"
    return "%s  rate %+d  pitch %+d%%" % (settings["voice"], settings.get("rate", 0),
                                          settings.get("pitch", 0))


# ------------------------------------------------------------------- internals

def _powershell():
    return shutil.which("powershell.exe") or shutil.which("powershell")


def _powershell_run(command, timeout=60):
    exe = _powershell()
    if not exe:
        raise SpeechError("PowerShell wasn't found, so Windows speech can't be driven.")
    try:
        p = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
            creationflags=0x08000000 if sys.platform.startswith("win") else 0)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SpeechError("Windows speech failed to run: %s" % e)
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", "replace").strip()
        raise SpeechError("Windows speech failed:\n%s" % err[:400])
    return (p.stdout or b"").decode("utf-8", "replace")


def _printable(text):
    """Drop the control characters XML forbids.

    One stray NUL anywhere in a line makes SpeakSsml reject the whole document,
    which would fail a render over a byte nobody can hear.
    """
    return "".join(" " if (ord(c) < 32 and c not in "\t\n\r") or ord(c) == 127
                   else c for c in str(text))


def _ps_quote(value):
    """PowerShell single-quoted strings escape a quote by doubling it."""
    return str(value).replace("'", "''")


def _signed(value):
    return "+%d" % value if value >= 0 else "%d" % value


def _clamp(value, bounds):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 0
    return max(bounds[0], min(bounds[1], value))


# ==========================================================================
# script_parser: Turn a plain-text script into an ordered list of speaking cues.
# ==========================================================================

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


# ==========================================================================
# llm: Local LLM client. Speaks the OpenAI /v1 chat protocol, which LM Studio,
# ==========================================================================

"""Local LLM client. Speaks the OpenAI /v1 chat protocol, which LM Studio,
Ollama, llama.cpp, Jan, vLLM and text-generation-webui all serve locally.

Nothing here talks to the internet - only 127.0.0.1.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

# (label, port) pairs probed by discover(); all OpenAI-compatible on /v1.
CANDIDATES = [
    ("LM Studio", 1234),
    ("Ollama", 11434),
    ("llama.cpp / vLLM", 8080),
    ("Jan", 1337),
    ("text-generation-webui", 5000),
    ("KoboldCpp", 5001),
]


class LLMError(RuntimeError):
    pass


# LM Studio ships a command-line tool that can start its server for us, so a
# stopped server is a question to the user rather than a dead end.
LMS_PATHS = (
    os.path.join("~", ".cache", "lm-studio", "bin", "lms.exe"),
    os.path.join("~", ".lmstudio", "bin", "lms.exe"),
    os.path.join("~", ".cache", "lm-studio", "bin", "lms"),
    os.path.join("~", ".lmstudio", "bin", "lms"),
)


def find_lmstudio_cli():
    """The path to LM Studio's `lms` tool, or "" if it isn't installed."""
    for p in LMS_PATHS:
        p = os.path.expanduser(p)
        if os.path.exists(p):
            return p
    found = shutil.which("lms")
    return found or ""


def unload_lmstudio(cli="", timeout=60):
    """Unload every loaded model so the card is free to draw. False if it can't."""
    cli = cli or find_lmstudio_cli()
    if not cli:
        return False
    try:
        r = subprocess.run([cli, "unload", "--all"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           timeout=timeout)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def start_lmstudio(wait=40, host="127.0.0.1", port=1234, cli=""):
    """Start LM Studio's server and wait for it to answer. Returns a base_url.

    Raises LLMError with something the user can act on, never a raw traceback.
    """
    cli = cli or find_lmstudio_cli()
    if not cli:
        raise LLMError(
            "LM Studio's command-line tool wasn't found, so it can't be started "
            "for you.\n\nOpen LM Studio and use Developer > Start Server.")
    base = "http://%s:%d/v1" % (host, int(port))
    try:
        proc = subprocess.Popen([cli, "server", "start"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
    except OSError as e:
        raise LLMError("Couldn't run %s: %s" % (cli, e))
    deadline = time.time() + max(5, wait)
    while time.time() < deadline:
        if port_open(host, port, timeout=0.5):
            try:
                if LocalLLM(base).models():
                    return base
            except LLMError:
                pass                      # server is up, models still enumerating
        time.sleep(0.5)
    out = ""
    try:
        proc.kill()
        out = (proc.communicate(timeout=3)[0] or b"").decode("utf-8", "replace")[-300:]
    except Exception:
        pass
    raise LLMError("LM Studio didn't come up within %d seconds.\n\n%s" % (wait, out.strip()))


def port_open(host, port, timeout=0.35):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, int(port))) == 0
    finally:
        s.close()


def discover(host="127.0.0.1"):
    """Find local OpenAI-compatible servers. Returns [(label, base_url, [models])]."""
    found = []
    for label, port in CANDIDATES:
        if not port_open(host, port):
            continue
        base = "http://%s:%d/v1" % (host, port)
        try:
            models = LocalLLM(base).models()
        except LLMError:
            models = []
        found.append((label, base, models))
    return found


class LocalLLM(object):
    def __init__(self, base_url="http://127.0.0.1:1234/v1", model="", timeout=600,
                 temperature=0.8):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or ""
        self.timeout = timeout
        self.temperature = temperature

    # ---------------------------------------------------------------- plumbing

    def _request(self, path, payload=None, method="GET", timeout=None):
        url = self.base_url + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:800]
            raise LLMError("The local model server returned %s for %s:\n%s" % (e.code, path, body))
        except urllib.error.URLError as e:
            raise LLMError(
                "Couldn't reach a local model at %s (%s).\n\n"
                "Start LM Studio (Developer > Start Server) or `ollama serve`, "
                "then press Detect again." % (self.base_url, e.reason))
        except ValueError as e:
            raise LLMError("The model server sent something that isn't JSON: %s" % e)

    def models(self):
        """Chat-capable models, best first. Embedding models are dropped."""
        data = self._request("/models", timeout=5)
        ids = [m.get("id", "") for m in (data.get("data") or []) if m.get("id")]
        return rank_models(ids)

    def ready(self):
        """Return a short status string or raise LLMError."""
        ms = self.models()
        if not ms:
            raise LLMError("Server is up at %s but has no model loaded." % self.base_url)
        if not self.model:
            self.model = ms[0]
        return "%s (%d model%s available)" % (self.model, len(ms), "" if len(ms) == 1 else "s")

    # ------------------------------------------------------------------- chat

    def chat(self, messages, temperature=None, max_tokens=2048, stop=None, cancel=None):
        if not self.model:
            self.ready()
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if stop:
            payload["stop"] = stop
        if cancel is not None and cancel():
            raise LLMError("Cancelled")
        data = self._request("/chat/completions", payload, method="POST")
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise LLMError("Unexpected reply from the model: %s" % json.dumps(data)[:400])

    def chat_json(self, system, user, expect="object", retries=2, temperature=None,
                  max_tokens=2048, cancel=None):
        """Ask for JSON and actually get JSON back, even from small local models."""
        messages = [
            {"role": "system", "content": system.strip() +
             "\n\nReply with JSON only. No commentary, no markdown fences."},
            {"role": "user", "content": user.strip()},
        ]
        last = ""
        for attempt in range(retries + 1):
            raw = self.chat(messages, temperature=temperature, max_tokens=max_tokens,
                            cancel=cancel)
            last = raw
            parsed = extract_json(raw, expect)
            if parsed is not None:
                return parsed
            messages = messages[:2] + [
                {"role": "assistant", "content": raw[:1500]},
                {"role": "user", "content":
                 "That wasn't valid JSON. Reply again with a single valid JSON %s and nothing "
                 "else." % expect},
            ]
        raise LLMError(
            "The model didn't return usable JSON after %d tries. Last reply:\n\n%s"
            % (retries + 1, last[:600]))


# ------------------------------------------------------------- model selection

NOT_CHAT = ("embed", "rerank", "whisper", "clip-", "-tts", "stable-diffusion", "sdxl")
NARROW = ("coder", "code-", "-code", "codestral", "starcoder", "math", "vision", "vl-")
SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.I)


def rank_models(ids):
    """Sort a server's model list so the best casting model comes first.

    A coder or embedding model happens to be first on plenty of installs, and
    writing a cast is neither of those jobs.
    """
    def key(name):
        low = name.lower()
        if any(w in low for w in NOT_CHAT):
            return None                       # can't hold a conversation at all
        narrow = any(w in low for w in NARROW)
        m = SIZE.search(low.replace("-", " "))
        billions = float(m.group(1)) if m else 0.0
        return (narrow, -billions, low)       # general first, then biggest

    scored = [(key(n), n) for n in ids]
    return [n for k, n in sorted((s for s in scored if s[0] is not None))]


# ------------------------------------------------------------------ json repair

FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
TRAILING_COMMA = re.compile(r",\s*([}\]])")
THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def extract_json(text, expect="object"):
    """Pull the first JSON object/array out of a model reply, or None."""
    if not text:
        return None
    text = THINK.sub(" ", text)
    m = FENCE.search(text)
    if m:
        text = m.group(1)
    opener, closer = ("{", "}") if expect == "object" else ("[", "]")

    candidates = []
    start = text.find(opener)
    if start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    candidates.append(text.strip())

    for cand in candidates:
        for attempt in (cand, TRAILING_COMMA.sub(r"\1", cand)):
            try:
                val = json.loads(attempt)
            except ValueError:
                continue
            if expect == "object" and isinstance(val, dict):
                return val
            if expect == "array" and isinstance(val, list):
                return val
            if expect == "array" and isinstance(val, dict):
                for key in ("characters", "cast", "actors", "items", "results", "shots"):
                    if isinstance(val.get(key), list):
                        return val[key]
    return None


# ==========================================================================
# comfy: Minimal ComfyUI HTTP client. Stdlib only, fully offline (localhost).
# ==========================================================================

"""Minimal ComfyUI HTTP client. Stdlib only, fully offline (localhost)."""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class ComfyError(RuntimeError):
    pass


# 8188 is the classic default; the desktop build often lands on 8000.
COMMON_PORTS = (8188, 8000, 8189, 8288, 3000)


def find_server(host="127.0.0.1", ports=None, timeout=1.5):
    """The first port on `host` that answers like ComfyUI, or 0.

    Worth doing automatically: a wrong port produces a bare socket error that
    tells the user nothing about where their server actually is.

    `ports` defaults to COMMON_PORTS at call time rather than in the signature,
    so the list can be changed (or narrowed by a test) and actually be honoured.
    """
    for port in (ports or COMMON_PORTS):
        try:
            ComfyClient(host, port, timeout=timeout).ping()
            return int(port)
        except ComfyError:
            continue
    return 0


class ComfyClient:
    def __init__(self, host="127.0.0.1", port=8188, timeout=10):
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self.client_id = str(uuid.uuid4())

    @property
    def base(self):
        return "http://%s:%d" % (self.host, self.port)

    # ---------- low level ----------

    def _get(self, path, params=None, raw=False):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                data = r.read()
        except urllib.error.URLError as e:
            raise ComfyError(
                "Couldn't reach ComfyUI at %s.\n\n%s\n\n"
                "Is ComfyUI running? If it is, it may be on a different port - "
                "press Test connection on the Setup tab and it will look."
                % (self.base, getattr(e, "reason", e)))
        return data if raw else json.loads(data.decode("utf-8"))

    def _post(self, path, payload):
        url = self.base + path
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8").strip()
            # /free and /interrupt answer 200 with no body at all. That is a
            # success, not malformed JSON.
            return json.loads(raw) if raw else {}
        except ValueError as e:
            raise ComfyError("ComfyUI sent something that isn't JSON for %s: %s" % (path, e))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise ComfyError("ComfyUI rejected the job (%s):\n%s" % (e.code, detail[:2000]))
        except urllib.error.URLError as e:
            raise ComfyError("POST %s failed: %s" % (url, e))

    # ---------- api ----------

    def ping(self):
        """Return a short status string, or raise ComfyError."""
        stats = self._get("/system_stats")
        dev = ""
        for d in stats.get("devices", []):
            dev = d.get("name", "")
            break
        return "Connected to ComfyUI at %s  %s" % (self.base, dev)

    def object_info(self):
        return self._get("/object_info")

    def queue_prompt(self, workflow):
        res = self._post("/prompt", {"prompt": workflow, "client_id": self.client_id})
        pid = res.get("prompt_id")
        if not pid:
            raise ComfyError("No prompt_id in response: %r" % res)
        node_errors = res.get("node_errors") or {}
        if node_errors:
            raise ComfyError("Workflow node errors: %s" % json.dumps(node_errors)[:2000])
        return pid

    def free(self, unload_models=True, free_memory=True):
        """Ask ComfyUI to drop its models and give the VRAM back.

        Never fatal: freeing is an optimisation, and an old build without /free
        should not stop the job that was about to run.
        """
        try:
            self._post("/free", {"unload_models": bool(unload_models),
                                 "free_memory": bool(free_memory)})
            return True
        except Exception:      # freeing is an optimisation, never a reason to stop
            return False

    def interrupt(self):
        try:
            self._post("/interrupt", {})
        except ComfyError:
            pass

    def history(self, prompt_id):
        return self._get("/history/%s" % prompt_id)

    def wait_for(self, prompt_id, poll=0.5, timeout=1800, cancel=None):
        """Block until the prompt finishes. Returns its history entry."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cancel is not None and cancel():
                raise ComfyError("Cancelled")
            hist = self.history(prompt_id)
            entry = hist.get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("completed") or entry.get("outputs"):
                    if status.get("status_str") == "error":
                        raise ComfyError(_format_exec_error(status))
                    return entry
                if status.get("status_str") == "error":
                    raise ComfyError(_format_exec_error(status))
            time.sleep(poll)
        raise ComfyError("Timed out after %ss waiting for ComfyUI." % timeout)

    AUDIO_KEYS = ("audio", "audios")
    IMAGE_KEYS = ("images", "gifs", "video", "videos", "animated")

    def file_outputs(self, history_entry, keys=None):
        """Every file a finished prompt produced, in node order."""
        keys = tuple(keys or (self.AUDIO_KEYS + self.IMAGE_KEYS + ("result",)))
        found = []
        outputs = history_entry.get("outputs", {})
        for node_id in sorted(outputs, key=_int_key):
            node_out = outputs[node_id]
            for key in keys:
                for item in node_out.get(key, []) or []:
                    if isinstance(item, dict) and item.get("filename"):
                        found.append(item)
        return found

    def audio_outputs(self, history_entry):
        return self.file_outputs(history_entry, self.AUDIO_KEYS + ("result",))

    def image_outputs(self, history_entry):
        return self.file_outputs(history_entry, self.IMAGE_KEYS)

    def download(self, file_ref, dest_path):
        params = {
            "filename": file_ref["filename"],
            "subfolder": file_ref.get("subfolder", ""),
            "type": file_ref.get("type", "output"),
        }
        data = self._get("/view", params, raw=True)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
        return dest_path

    def upload_audio(self, path, subfolder="scriptvoice"):
        """Push a local reference-voice file into ComfyUI's input folder.

        Returns the name the workflow should use in a LoadAudio node.
        """
        name = os.path.basename(path)
        with open(path, "rb") as f:
            content = f.read()
        boundary = "----scriptvoice%s" % uuid.uuid4().hex
        parts = []

        def field(k, v):
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode())

        field("type", "input")
        field("subfolder", subfolder)
        field("overwrite", "true")
        parts.append(
            ("--%s\r\nContent-Disposition: form-data; name=\"image\"; filename=\"%s\"\r\n"
             "Content-Type: application/octet-stream\r\n\r\n" % (boundary, name)).encode()
        )
        parts.append(content)
        parts.append(("\r\n--%s--\r\n" % boundary).encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            self.base + "/upload/image",
            data=body,
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
        )
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 60)) as r:
                res = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise ComfyError("Upload of %s failed (%s): %s" % (name, e.code, e.read()[:500]))
        except urllib.error.URLError as e:
            raise ComfyError("Upload of %s failed: %s" % (name, e))
        sub = res.get("subfolder") or ""
        fn = res.get("name") or name
        return "%s/%s" % (sub, fn) if sub else fn


def _int_key(s):
    try:
        return (0, int(s))
    except (TypeError, ValueError):
        return (1, str(s))


def _format_exec_error(status):
    msgs = []
    for m in status.get("messages", []) or []:
        if isinstance(m, (list, tuple)) and len(m) == 2 and m[0] == "execution_error":
            d = m[1] or {}
            msgs.append("%s in node %s (%s): %s" % (
                d.get("exception_type", "Error"),
                d.get("node_id"), d.get("node_type"),
                d.get("exception_message", ""),
            ))
    return "\n".join(msgs) or "ComfyUI reported an execution error."


# ==========================================================================
# casting: The casting director: a local LLM turns a premise into a locked-down cast,
# ==========================================================================

"""The casting director: a local LLM turns a premise into a locked-down cast,
judges whether each actor fits the plot, and plans the shots for the movie.

Every actor record carries a fixed appearance description and a fixed seed, so
the same character looks and sounds the same in every image and every line.
"""

import random
import re

# (flattened) from . import script_parser
# (flattened) from .llm import LLMError

MAX_ACTORS = 8

CAST_SYSTEM = """\
You are a casting director and character designer for a film.
From the user's premise you invent the cast: the small set of characters the story
actually needs. Each character must be visually specific and unmistakable, so the
same person can be drawn identically in every shot.

Return a JSON array. Each element:
{
  "name": "SHORT NAME IN CAPS",
  "role": "protagonist | antagonist | supporting | narrator",
  "one_line": "who they are in one sentence",
  "age_range": "e.g. late 30s",
  "appearance": "a dense visual description for an image generator: build, hair, face, skin, distinguishing marks. No camera or lighting words.",
  "wardrobe": "what they wear, specific and consistent",
  "distinguishing": "the two or three details that must never change",
  "personality": "how they behave, in one sentence",
  "voice_type": "how the voice sounds: pitch, texture, pace, accent",
  "voice_direction": "how they deliver lines, in a few words",
  "sample_line": "one line of dialogue in their voice"
}
No character should be describable as 'generic'. Do not include the narrator unless
the premise calls for narration."""

JUDGE_SYSTEM = """\
You are a script editor reviewing a proposed cast against the premise.
For each character decide honestly whether they earn their place in this plot.
Be willing to fail a character: a cast where everyone scores 90 is a useless review.

Return a JSON array, one element per character, in the same order:
{
  "name": "SHORT NAME IN CAPS",
  "score": 0-100,
  "verdict": "fits | weak | cut",
  "reason": "one or two sentences of concrete justification",
  "fix": "the single change that would most improve this character"
}
Use 'fits' for 70+, 'weak' for 40-69, 'cut' below 40."""

RECAST_SYSTEM = """\
You are recasting one character in an existing ensemble. Keep the character's
narrative function, but make them a genuinely different person: different look,
different voice, different energy. Do not reuse the previous appearance or name.

Return a single JSON object with the same fields as the rest of the cast:
name, role, one_line, age_range, appearance, wardrobe, distinguishing,
personality, voice_type, voice_direction, sample_line."""

REVOICE_SYSTEM = """\
You are a voice director. Re-imagine only the VOICE of this character - the body,
the look and the role stay exactly as they are. Give a clearly different vocal
identity from the one described.

Return a single JSON object:
{"voice_type": "...", "voice_direction": "...", "sample_line": "..."}"""

SCRIPT_SYSTEM = """\
You are a screenwriter. Write the screenplay for the user's premise using ONLY the
cast you are given - no new speaking characters.

Format, exactly:
[SCENE: short location and time]
NAME: a line of dialogue
NAME: a line of dialogue

Rules: every spoken line is one 'NAME: line' on a single line. Scene headers in
square brackets. No action paragraphs, no parentheses, no camera directions.
Keep dialogue speakable: plain sentences, no lists."""

SHOTS_SYSTEM = """\
You are a storyboard artist. For each numbered line of dialogue, describe the single
image the audience sees while that line is spoken.

Every line is preceded by its scene heading in [brackets]. THAT IS WHERE THE LINE
HAPPENS. It is taken from the script and it is not negotiable:
- A line under [EXT. LAKE - DAY] happens outdoors on the lake in daylight. It cannot
  be an office, a desk, or a room.
- Put people where the heading says they are, doing what the dialogue implies they
  are doing there.
- "setting" must restate that heading's location. Never move a line somewhere else
  because it would suit the conversation better.

Clothes belong to the scene, not to the character. Someone on a lake in summer is
in swimwear or a wetsuit; the same person in a boardroom is in a suit. Say what they
are wearing HERE, for this location and this activity.

"wardrobe" is an OBJECT KEYED BY CHARACTER NAME, and each value is only the
garments that person wears - no name, no verb:
  "wardrobe": {"VICTOR": "faded polo shirt and swim shorts", "NORA": "sundress"}
Never write one sentence covering everybody. Never put one character's clothes
under another character's name.

Return a JSON array, one element per line, in order:
{"n": <line number>, "shot": "shot size and framing, what is in frame, the action",
 "subject": "NAME of the one character the camera is on",
 "setting": "where this happens - the heading's location",
 "wardrobe": {"NAME": "garments only"},
 "mood": "lighting and mood"}

"subject" is who we SEE, which is often not who is speaking. A reaction shot of
the listener during someone else's line is normal and good - just say whose face
is in frame. Use a name from the cast, exactly as spelled there.
Describe the visible world only. Never name a character's appearance - refer to them
by NAME, the renderer already knows what they look like."""

ANGLES = [
    (0, "front view, facing the camera directly"),
    (45, "three-quarter view turned slightly to their left"),
    (90, "full left profile view"),
    (135, "three-quarter rear view from their left"),
    (180, "back view, facing away from the camera"),
    (225, "three-quarter rear view from their right"),
    (270, "full right profile view"),
    (315, "three-quarter view turned slightly to their right"),
]

TURNAROUND_STYLE = ("full body character turnaround sheet, standing neutral A-pose, "
                    "even flat studio lighting, plain seamless light grey background, "
                    "whole body in frame from head to feet, consistent character design")

PORTRAIT_STYLE = ("cinematic character portrait, head and shoulders, soft key light, "
                  "shallow depth of field, neutral background")

FIELDS = ("name", "role", "one_line", "age_range", "appearance", "wardrobe",
          "distinguishing", "personality", "voice_type", "voice_direction", "sample_line")


# --------------------------------------------------------------------- helpers

def blank_actor(name="NEW CHARACTER"):
    return {
        "name": name, "role": "supporting", "one_line": "", "age_range": "",
        "appearance": "", "wardrobe": "", "distinguishing": "", "personality": "",
        "voice_type": "", "voice_direction": "", "sample_line": "",
        "fit": {}, "approved": False, "look_seed": random.randint(0, 2 ** 31 - 1),
    }


def _clean_name(raw, fallback="CHARACTER"):
    name = re.sub(r"[^A-Za-z0-9 '._-]", " ", str(raw or "")).strip()
    name = re.sub(r"\s+", " ", name).upper()
    return name[:32] or fallback


def normalise_actor(raw, fallback_name="CHARACTER"):
    """Coerce whatever the model produced into a complete actor record."""
    a = blank_actor()
    if not isinstance(raw, dict):
        raw = {}
    for f in FIELDS:
        v = raw.get(f, "")
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v)
        a[f] = str(v or "").strip()
    a["name"] = _clean_name(a["name"], fallback_name)
    role = a["role"].lower()
    a["role"] = role if role in ("protagonist", "antagonist", "supporting", "narrator") \
        else "supporting"
    a["look_seed"] = int(raw.get("look_seed") or random.randint(0, 2 ** 31 - 1))
    return a


def _uniquify(actors):
    seen, out = set(), []
    for a in actors:
        name, n = a["name"], 2
        while name in seen:
            name = "%s %d" % (a["name"], n)
            n += 1
        a["name"] = name
        seen.add(name)
        out.append(a)
    return out


def _cast_digest(actors, skip=None):
    lines = []
    for a in actors:
        if skip and a["name"] == skip:
            continue
        lines.append("- %s (%s): %s | looks: %s | voice: %s"
                     % (a["name"], "LEAD" if a.get("lead") else a.get("role", ""),
                        a.get("one_line", ""),
                        a.get("appearance", "")[:160], a.get("voice_type", "")))
    return "\n".join(lines) or "(none yet)"


# ------------------------------------------------------------------- the steps

def derive_cast(llm, premise, max_actors=5, cancel=None):
    """Premise -> cast list."""
    if not (premise or "").strip():
        raise LLMError("Write the premise first - one or two sentences is enough.")
    user = ("PREMISE:\n%s\n\nInvent at most %d characters. Fewer is better if the story "
            "only needs a few." % (premise.strip(), int(max_actors)))
    raw = llm.chat_json(CAST_SYSTEM, user, expect="array", max_tokens=3000, cancel=cancel)
    actors = [normalise_actor(x, "CHARACTER %d" % (i + 1)) for i, x in enumerate(raw)]
    actors = [a for a in actors if a["appearance"] or a["one_line"]][:MAX_ACTORS]
    if not actors:
        raise LLMError("The model returned an empty cast. Try a longer premise.")
    return _uniquify(actors)


ROLE_SYSTEM = """\
You are a script supervisor writing a cast list from a finished script.
For each character you are given the scenes they appear in and every line they
speak. Say in ONE plain sentence what that character does in this story.

THE ONLY SOURCE IS THE LINES YOU ARE GIVEN. This is the whole job:
- Use only what the lines and scene headings actually show.
- Never invent a profession, a relationship, a backstory or a motive. If the
  lines do not say someone is a lawyer, they are not a lawyer.
- Do not carry anything over from the premise or from the character's name if
  the lines contradict it.
- If the lines are too few to tell, say exactly that and describe only what
  happens in them: "Speaks three lines closing a property sale." That is a
  correct answer, not a failure.
- Never judge them, score them, or suggest changes.

Return a JSON array, one element per character, in the same order:
{"name": "SHORT NAME IN CAPS", "role": "one sentence, no more"}"""


def _scene_at(scene_list, line_no):
    """The INT./EXT. heading a line falls under, so a line has a place."""
    here = ""
    for at, head in scene_list:
        if at <= line_no:
            here = head
        else:
            break
    return here


def _lines_digest(cues, name, script="", limit=24, chars=160):
    """Everything this character says, under the scene heading it happens in.

    The old version passed six truncated lines with no context, which is how a
    man closing a property sale became "a sharp-tongued legal mind".
    """
    scene_list = script_parser.scenes(script) if script else []
    said = [c for c in cues if c.speaker == name]
    out, last = [], None
    for c in said[:limit]:
        head = _scene_at(scene_list, c.line_no)
        if head and head != last:
            out.append("[%s]" % head)
            last = head
        out.append('"%s"' % c.text[:chars])
    if len(said) > limit:
        out.append("(...and %d more lines)" % (len(said) - limit))
    return " ".join(out) or "(never speaks)"


def describe_roles(llm, premise, actors, cues, script="", cancel=None):
    """What each character does in the script. Returns {name: sentence}.

    `premise` is accepted for call compatibility and deliberately not used: the
    script is the only evidence this description is allowed to rest on.
    """
    if not actors:
        return {}
    block = "\n\n".join(
        "- %s speaks %d line(s):\n  %s"
        % (a["name"], sum(1 for c in cues if c.speaker == a["name"]),
           _lines_digest(cues, a["name"], script))
        for a in actors)
    # The premise is deliberately not sent. It is what the model was filling the
    # gaps with, and the script is the only thing that is actually true.
    user = ("CHARACTERS, THE SCENES THEY APPEAR IN, AND EVERY LINE THEY SPEAK:\n%s"
            % block)
    raw = llm.chat_json(ROLE_SYSTEM, user, expect="array", max_tokens=1600,
                        temperature=0.1, cancel=cancel)
    out = {}
    names = [a["name"] for a in actors]
    for i, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        name = _clean_name(item.get("name"), "")
        if name not in names and i < len(names):
            name = names[i]                # models drop or mangle the name
        role = str(item.get("role") or "").strip()
        if name and role:
            out[name] = role
    return out


def script_places(cues, name):
    """Where this character appears: (line count, first line_no, [line numbers])."""
    hits = [c.line_no for c in cues if c.speaker == name]
    return len(hits), (hits[0] if hits else 0), hits


def judge_cast(llm, premise, actors, cancel=None):
    """Score every actor against the plot. Returns {name: fit_dict}."""
    if not actors:
        return {}
    leads = [a["name"] for a in actors if a.get("lead")]
    user = "PREMISE:\n%s\n\nPROPOSED CAST:\n%s%s" % (
        premise.strip(), _cast_digest(actors),
        ("\n\nThe director has cast %s as the lead. That is settled - judge them as "
         "the lead, and never suggest demoting them to supporting."
         % " and ".join(leads)) if leads else "")
    raw = llm.chat_json(JUDGE_SYSTEM, user, expect="array", max_tokens=2000,
                        temperature=0.4, cancel=cancel)
    by_name = {}
    for i, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        name = _clean_name(item.get("name"), "")
        if name not in [a["name"] for a in actors] and i < len(actors):
            name = actors[i]["name"]          # models often drop or mangle the name
        try:
            score = max(0, min(100, int(float(item.get("score", 0)))))
        except (TypeError, ValueError):
            score = 0
        verdict = str(item.get("verdict", "")).lower().strip()
        if verdict not in ("fits", "weak", "cut"):
            verdict = "fits" if score >= 70 else ("weak" if score >= 40 else "cut")
        by_name[name] = {
            "score": score, "verdict": verdict,
            "reason": str(item.get("reason", "")).strip(),
            "fix": str(item.get("fix", "")).strip(),
        }
    return by_name


def recast_actor(llm, premise, actor, others, note="", cues=None, script="",
                 cancel=None):
    """Regenerate one actor, keeping their function in the story.

    Pass `cues` so the model sees what this character actually says. Without it
    a recast is written from the premise alone and simply invents a job.
    """
    user = ("PREMISE:\n%s\n\nTHE REST OF THE CAST (do not duplicate these):\n%s\n\n"
            "CHARACTER TO REPLACE:\nname: %s\nrole: %s\nfunction: %s\n"
            "previous look: %s\nprevious voice: %s\n%s"
            % (premise.strip(), _cast_digest(others, skip=actor.get("name")),
               actor.get("name", ""),
               "LEAD - the main character of the film" if actor.get("lead")
               else actor.get("role", ""), actor.get("one_line", ""),
               actor.get("appearance", ""), actor.get("voice_type", ""),
               ("\nDIRECTOR'S NOTE: " + note.strip()) if note.strip() else ""))
    if cues:
        user += ("\n\nEVERY LINE THIS CHARACTER SPEAKS IN THE SCRIPT - the new "
                 "version must still be the person who says these, so do not give "
                 "them a job or a history the lines contradict:\n%s"
                 % _lines_digest(cues, actor.get("name", ""), script))
    raw = llm.chat_json(RECAST_SYSTEM, user, expect="object", max_tokens=1200,
                        temperature=1.0, cancel=cancel)
    fresh = normalise_actor(raw, actor.get("name", "CHARACTER"))
    fresh["look_seed"] = random.randint(0, 2 ** 31 - 1)   # new look = new seed
    fresh["approved"] = False
    return fresh


def revoice_actor(llm, premise, actor, note="", cancel=None):
    """Regenerate only the voice of an actor. Returns the changed fields."""
    user = ("PREMISE:\n%s\n\nCHARACTER: %s - %s (%s)\nlooks like: %s\n"
            "current voice: %s / %s\n%s"
            % (premise.strip(), actor.get("name", ""), actor.get("one_line", ""),
               actor.get("age_range", ""), actor.get("appearance", "")[:200],
               actor.get("voice_type", ""), actor.get("voice_direction", ""),
               ("\nDIRECTOR'S NOTE: " + note.strip()) if note.strip() else ""))
    raw = llm.chat_json(REVOICE_SYSTEM, user, expect="object", max_tokens=600,
                        temperature=1.0, cancel=cancel)
    if not isinstance(raw, dict):
        raw = {}
    return {
        "voice_type": str(raw.get("voice_type", "") or actor.get("voice_type", "")).strip(),
        "voice_direction": str(raw.get("voice_direction", "")
                               or actor.get("voice_direction", "")).strip(),
        "sample_line": str(raw.get("sample_line", "") or actor.get("sample_line", "")).strip(),
    }


def write_script(llm, premise, actors, scenes=4, cancel=None):
    """Premise + locked cast -> a screenplay ScriptVoice can parse."""
    cast = "\n".join("- %s (%s): %s. Speaks like: %s"
                     % (a["name"], a["role"], a.get("one_line", ""),
                        a.get("voice_direction", "") or a.get("voice_type", ""))
                     for a in actors)
    user = ("PREMISE:\n%s\n\nCAST (use exactly these names):\n%s\n\n"
            "Write about %d scenes. Aim for 6 to 12 spoken lines per scene."
            % (premise.strip(), cast, int(scenes)))
    text = llm.chat(
        [{"role": "system", "content": SCRIPT_SYSTEM}, {"role": "user", "content": user}],
        temperature=0.9, max_tokens=4000, cancel=cancel)
    return _tidy_script(text, [a["name"] for a in actors])


_WEARS = re.compile(r"^\s*(?:\w[\w' -]{0,30}?)\s+(?:is\s+)?(?:wears|wearing|"
                    r"has\s+on|in)\s+", re.I)


def shot_text(shot):
    """What this shot shows: the user's words if they wrote any, else the AI's."""
    return (str((shot or {}).get("shot_override", "")).strip()
            or str((shot or {}).get("shot", "")).strip())


def shot_subject(shot, cue, cast_names=()):
    """Whose face this shot is of. The speaker is only the last resort.

    Order: what the user chose, then what the model said, then the first cast
    member the shot description actually names, then the speaker.
    """
    names = [str(n).strip().upper() for n in cast_names if str(n).strip()]
    for key in ("subject_override", "subject"):
        want = str((shot or {}).get(key, "")).strip().upper()
        if want and want in names:
            return want
    text = shot_text(shot)
    if text:
        # Longest first, so REEVE HALLOWAY wins over REEVE.
        best, at = "", len(text) + 1
        for n in sorted(names, key=len, reverse=True):
            m = re.search(r"\b%s\b" % re.escape(n), text, re.I)
            if m and (m.start() < at or (m.start() == at and len(n) > len(best))):
                best, at = n, m.start()
        if best:
            return best
    return str(getattr(cue, "speaker", "") or "").strip().upper()


def wardrobe_for(shot, speaker, cast_names=()):
    """The garments THIS speaker wears in this shot, or "".

    A model asked for wardrobe will often answer with one sentence about
    everyone in frame. Handing that to the renderer after the word "wearing"
    puts another character's clothes on this one, so anything that cannot be
    tied to this speaker is thrown away rather than guessed at.
    """
    raw = (shot or {}).get("wardrobe", "")
    speaker = str(speaker or "").strip().upper()

    if isinstance(raw, dict):                       # what we asked for
        for key, value in raw.items():
            if str(key).strip().upper() == speaker:
                return _WEARS.sub("", str(value or "").strip(" ,.")).strip()
        return ""

    text = str(raw or "").strip()
    if not text:
        return ""

    others = set(n.strip().upper() for n in cast_names) - {speaker}
    # Split on sentences and on ", NAME wears ..." style joins.
    clauses = [c.strip(" ,.") for c in re.split(r"(?<=[.;])\s+|\s+(?=\b[A-Z][a-z]+\s+(?:is\s+)?(?:wears|wearing)\b)", text) if c.strip()]
    mine = [c for c in clauses if speaker in c.upper()]
    if mine:
        picked = " ".join(mine)
    elif len(clauses) == 1 and not any(o in text.upper() for o in others):
        picked = clauses[0]                         # one outfit, nobody else named
    else:
        return ""                                  # it is about other people
    if any(o in picked.upper() for o in others):
        return ""                                  # still mentions someone else
    return _WEARS.sub("", picked).strip(" ,.")


def scene_phrase(heading):
    """"EXT. MARINA DOCK - DAY" -> "exterior, marina dock, daytime".

    A raw slug line is not a good image prompt; the words in it are.
    """
    text = str(heading or "").strip()
    if not text:
        return ""
    where = text
    inout = ""
    m = re.match(r"^\s*(INT\.?/EXT\.?|EXT\.?/INT\.?|I/E|INT|EXT)[.\s]+(.*)$", text, re.I)
    if m:
        head = m.group(1).upper().replace(".", "")
        inout = {"INT": "interior", "EXT": "exterior"}.get(head, "interior and exterior")
        where = m.group(2)
    when = ""
    parts = [p.strip() for p in re.split(r"\s+[-\u2013]\s+", where) if p.strip()]
    if len(parts) > 1:
        tail = parts[-1].lower()
        # CONTINUOUS and LATER are continuity notes for the crew. They say
        # nothing about what the picture looks like, so they are dropped rather
        # than fed to the renderer as if they were a time of day.
        times = {"day": "daytime", "night": "at night", "dawn": "at dawn",
                 "dusk": "at dusk", "morning": "in the morning",
                 "evening": "in the evening", "afternoon": "in the afternoon",
                 "sunset": "at sunset", "sunrise": "at sunrise"}
        if tail in times:
            when = times[tail]
            parts = parts[:-1]
        elif tail in ("continuous", "later", "moments later", "same", "same time"):
            parts = parts[:-1]
    where = ", ".join(parts).lower()
    return ", ".join(p for p in (inout, where, when) if p)


def plan_shots(llm, premise, actors, cues, script="", cancel=None):
    """One visual description per spoken line, for the movie render."""
    scene_list = script_parser.scenes(script) if script else []
    rows, last = [], None
    for i, c in enumerate(cues):
        head = _scene_at(scene_list, c.line_no)
        rows.append("%d. [%s] %s: %s"
                    % (i + 1, head or last or "no heading given", c.speaker, c.text))
        last = head or last
    numbered = "\n".join(rows)
    user = ("PREMISE:\n%s\n\nCAST:\n%s\n\nDIALOGUE, EACH UNDER THE SCENE HEADING "
            "IT HAPPENS IN:\n%s" % (premise.strip(), _cast_digest(actors), numbered))
    raw = llm.chat_json(SHOTS_SYSTEM, user, expect="array",
                        max_tokens=min(6000, 200 + 90 * len(cues)),
                        temperature=0.6, cancel=cancel)
    shots = {}
    for i, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        try:
            n = int(item.get("n", i + 1)) - 1
        except (TypeError, ValueError):
            n = i
        if 0 <= n < len(cues):
            shots[n] = {
                "shot": str(item.get("shot", "")).strip(),
                "subject": str(item.get("subject", "")).strip().upper(),
                "setting": str(item.get("setting", "")).strip(),
                # kept as-is: a dict keyed by name, or the sentence a smaller
                # model returns instead. wardrobe_for() sorts out which.
                "wardrobe": item.get("wardrobe", ""),
                "mood": str(item.get("mood", "")).strip(),
            }
    return shots


# ------------------------------------------------------------ prompt builders

def look_prompt(actor, extra="", style=PORTRAIT_STYLE, wardrobe=True):
    """The prompt that pins this character's appearance. Always the same words."""
    # When the user has written the look themselves it replaces the model's
    # description rather than joining it - otherwise the two contradict each
    # other in the same prompt ("young woman" next to "dark stubble") and the
    # picture splits the difference.
    if (actor.get("look_note") or "").strip():
        bits = [actor["look_note"]]
    else:
        bits = [actor.get("appearance", ""),
                actor.get("wardrobe", "") if wardrobe else "",
                actor.get("distinguishing", "")]
    if actor.get("age_range"):
        bits.insert(0, "%s, %s" % (actor.get("name", "").title(), actor["age_range"]))
    bits = [b.strip(" ,") for b in bits if b and b.strip()]
    if extra:
        bits.append(extra)
    if style:
        bits.append(style)
    return ", ".join(bits)


def turnaround_prompts(actor, angles=None):
    """[(angle, prompt)] for a full spin. Same seed + same words = same person."""
    angles = angles or ANGLES
    return [(deg, look_prompt(actor, phrase, TURNAROUND_STYLE)) for deg, phrase in angles]


def shot_prompt(actor_map, cue, shot, style="cinematic film still, 35mm, natural lighting"):
    """The prompt for one movie shot, with the speaker's fixed look folded in."""
    subject = shot_subject(shot, cue, actor_map.keys())
    actor = actor_map.get(subject) or {}
    # The scene supplies the clothes when it has an opinion, so the character's
    # one fixed outfit does not follow them onto the lake.
    scene_dress = wardrobe_for(shot, subject, actor_map.keys())
    who = (look_prompt(actor, style="", wardrobe=not scene_dress)
           if actor else subject.title())
    # The scene heading comes from the script, so it outranks whatever the model
    # decided the setting was.
    where = scene_phrase(shot.get("scene", "")) or shot.get("setting", "")
    dress = scene_dress or actor.get("wardrobe", "")
    parts = [shot_text(shot) or "medium shot of %s speaking" % subject.title(),
             ("in " + where) if where else "",
             who, ("wearing " + dress) if dress else "",
             shot.get("mood", ""), style]
    return ", ".join(p.strip(" ,") for p in parts if p and p.strip())


NEGATIVE = ("text, watermark, signature, extra limbs, deformed hands, blurry, "
            "lowres, duplicate person, cropped head")


# -------------------------------------------------------------- script tidying

_SCENE = re.compile(r"^\s*\[(.+?)\]\s*$")
_LINE = re.compile(r"^\s*([A-Za-z0-9 '._-]{1,40})\s*:\s*(.+)$")


def _tidy_script(text, names):
    """Keep scene headers and 'NAME: line' pairs; snap names to the real cast."""
    upper = {n.upper(): n for n in names}
    out = []
    for raw in (text or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            out.append("")
            continue
        m = _SCENE.match(line)
        if m:
            out.append("[%s]" % m.group(1).strip())
            continue
        m = _LINE.match(line)
        if m:
            name = re.sub(r"\s+", " ", m.group(1)).strip().upper()
            name = upper.get(name, name)
            body = m.group(2).strip()
            if body:
                out.append("%s: %s" % (name, body))
            continue
        if line.startswith(("#", "[")):
            out.append(line)
    cleaned = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip() + "\n"


# ==========================================================================
# project: Project file (.svproj) plus ComfyUI API-workflow introspection.
# ==========================================================================

"""Project file (.svproj) plus ComfyUI API-workflow introspection.

A project holds the premise, the locked cast, the script, and one ComfyUI
workflow per job slot (voice / portrait / turnaround / shot).
"""

import copy
import json
import os
import random

VERSION = 2

# Each slot is a workflow ScriptVoice drives, and the inputs it needs to find in it.
WORKFLOW_SLOTS = {
    "voice": {
        "label": "Voice (text to speech)",
        "keys": [
            ("text", "the line of dialogue to speak", True),
            ("voice", "reference audio or speaker preset", False),
            ("seed", "seed", False),
        ],
    },
    "portrait": {
        "label": "Actor portrait (text to image)",
        "keys": [
            ("prompt", "positive prompt", True),
            ("negative", "negative prompt", False),
            ("seed", "seed", False),
        ],
    },
    "turnaround": {
        "label": "Actor turnaround (text to image, one frame per angle)",
        "keys": [
            ("prompt", "positive prompt", True),
            ("negative", "negative prompt", False),
            ("seed", "seed", False),
            ("image", "reference image, if the workflow takes one", False),
        ],
    },
    "shot": {
        "label": "Movie shot (text to image or video)",
        "keys": [
            ("prompt", "positive prompt", True),
            ("negative", "negative prompt", False),
            ("seed", "seed", False),
            ("image", "reference image, if the workflow takes one", False),
        ],
    },
}

HINTS = {
    "text": ("text", "string", "speech", "transcript", "sentence", "dialogue"),
    "prompt": ("text", "prompt", "positive", "string", "clip"),
    "negative": ("negative", "neg"),
    # no bare "sample": it matches KSampler.sampler_name on image workflows
    "voice": ("audio", "voice", "reference", "ref_audio", "speaker", "clone"),
    "image": ("image", "reference", "init", "ipadapter"),
    "seed": ("seed", "noise_seed"),
}
NEGATIVE_WORDS = ("negative", "neg_", "_neg")


# Shipped next to the program: a plain SDXL-Turbo text-to-image graph. A new
# project adopts it so the draw buttons work before the user has been to Setup.
DEFAULT_WORKFLOW = "sdxl_turbo_actor_api.json"
DEFAULT_SLOTS = ("portrait", "turnaround", "shot")


def bundled_workflow(name=DEFAULT_WORKFLOW):
    """The full path to a workflow shipped with the program, or ""."""
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (here, os.path.dirname(here)):
        path = os.path.join(base, "workflows", name)
        if os.path.exists(path):
            return path
    return ""


def adopt_default_workflows(p):
    """Fill empty picture slots from the bundled workflow. Returns the slots filled.

    Only ever fills a slot that is empty, so an opened project and anything the
    user chose by hand are left exactly as they are.
    """
    path = bundled_workflow()
    if not path:
        return []
    try:
        wf = load_workflow(path)
    except Exception:
        return []
    filled = []
    for slot in DEFAULT_SLOTS:
        cfg = (p.setdefault("workflows", {})
                .setdefault(slot, {"path": "", "mapping": {}}))
        if not cfg.get("path"):
            cfg["path"] = path
            cfg["mapping"] = guess_mapping(wf, slot)
            filled.append(slot)
    return filled


def new_project():
    return {
        "version": VERSION,
        "name": "Untitled",
        "premise": "",
        "server": {"host": "127.0.0.1", "port": 8188},
        "llm": {"base_url": "", "model": "", "temperature": 0.85},
        "workflows": {slot: {"path": "", "mapping": {}} for slot in WORKFLOW_SLOTS},
        "characters": {},
        "cast_order": [],
        "script": "",
        "shots": {},
        "options": {
            "default_speaker": "NARRATOR",
            "strip_parentheticals": True,
            "split_sentences": False,
            "max_chars": 0,
            "gap_seconds": 0.35,
            "upload_voices": True,
            "voice_backend": "comfyui",     # or "system": the Windows SAPI voices
            "reuse_unchanged": True,
            # Writing and drawing never run at the same moment, but both models
            # stay resident, which is what makes a big model unusable on a small
            # card. When on, each step evicts the other one first.
            "free_gpu": False,
            "output_dir": "",
            "max_actors": 5,
            "scene_count": 4,
            "turnaround_frames": 8,
            "global_params": {},
        },
    }


def alias_map(project):
    """{other name: real name} for every character that has absorbed another.

    A screenplay calls the same person SAM in one scene and MAN in the next.
    One map, applied wherever a script is parsed, keeps them one character.
    """
    out = {}
    for name, rec in (project.get("characters") or {}).items():
        for alias in (rec.get("aliases") or []):
            alias = str(alias).strip().upper()
            if alias and alias != name:
                out[alias] = name
    return out


def apply_aliases(project, cues):
    """Rewrite cue speakers through the alias map, in place. Returns the cues."""
    amap = alias_map(project)
    if amap:
        for c in cues:
            if c.speaker in amap:
                c.speaker = amap[c.speaker]
    return cues


def new_character(name):
    """One actor: who they are, how they look, how they sound, and their state."""
    return {
        # identity, written by the casting director
        "name": name,
        "role": "supporting",
        # The two fields below belong to the user, not the casting director.
        # Nothing the model writes is ever allowed to replace them.
        "lead": False,          # "Main character" - a tick box, not a guess
        "script_role": "",      # what they actually do in the script, in a sentence
        "aliases": [],          # other script names that are really this person
        # A picture of this character to condition every render on. Yours if you
        # set one, otherwise the portrait the program drew.
        "reference_image": "",
        "look_note": "",        # words that go straight into the image prompt
        "one_line": "",
        "age_range": "",
        "appearance": "",
        "wardrobe": "",
        "distinguishing": "",
        "personality": "",
        "voice_type": "",
        "voice_direction": "",
        "sample_line": "",
        # locked so the character stays the same person everywhere
        "look_seed": random.randint(0, 2 ** 31 - 1),
        "seed": -1,             # voice seed; -1 = random per line
        # how the voice is actually produced
        "voice_file": "",       # local reference clip to clone
        "voice_value": "",      # or a preset / speaker name
        "system_voice": {},     # {voice, rate, pitch} override for the Windows voices
        "params": {},           # "node_id.input" -> value, this character only
        # generated assets and review state
        "fit": {},              # {score, verdict, reason, fix}
        "portrait": "",
        "turnaround": [],
        "voice_sample": "",
        "approved": False,
        "notes": "",
    }


def load(path):
    """Open a .svproj, falling back to defaults for anything unreadable.

    A project file is edited by hand, half-written by a crash, or carried over
    from an older version, so nothing in here may raise: a damaged field costs
    that field, never the whole file.
    """
    data = None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:   # -sig: tolerate a BOM
            data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        data = None
    if not isinstance(data, dict):                         # empty, truncated, a list
        data = {}

    base = new_project()
    for k in base:
        if k in data:
            base[k] = data[k]
    for key in ("options", "server", "llm"):
        section = data.get(key)
        base[key] = dict(new_project()[key],
                         **(section if isinstance(section, dict) else {}))
    base["options"] = _sane_options(base["options"])
    base["script"] = base["script"] if isinstance(base["script"], str) else ""
    base["premise"] = base["premise"] if isinstance(base["premise"], str) else ""
    base["shots"] = base["shots"] if isinstance(base["shots"], dict) else {}

    wfs = {slot: {"path": "", "mapping": {}} for slot in WORKFLOW_SLOTS}
    for slot, cfg in _items(data.get("workflows")):
        if slot in wfs and isinstance(cfg, dict):
            path_val = cfg.get("path", "")
            wfs[slot] = {"path": path_val if isinstance(path_val, str) else "",
                         "mapping": _str_map(cfg.get("mapping"))}
    if isinstance(data.get("workflow_path"), str) and data["workflow_path"]:
        wfs["voice"] = {"path": data["workflow_path"],   # v1 project: one TTS workflow
                        "mapping": _str_map(data.get("mapping"))}
    base["workflows"] = wfs

    chars = {}
    for name, c in _items(data.get("characters")):
        merged = new_character(str(name))
        if isinstance(c, dict):
            merged.update(c)
        merged["name"] = str(name)
        merged["turnaround"] = list(merged.get("turnaround") or []) \
            if isinstance(merged.get("turnaround"), (list, tuple)) else []
        merged["fit"] = merged["fit"] if isinstance(merged.get("fit"), dict) else {}
        merged["params"] = merged["params"] if isinstance(merged.get("params"), dict) else {}
        merged["system_voice"] = merged["system_voice"] \
            if isinstance(merged.get("system_voice"), dict) else {}
        chars[str(name)] = merged
    base["characters"] = chars
    order = data.get("cast_order")
    order = order if isinstance(order, (list, tuple)) else []
    base["cast_order"] = [n for n in order if n in chars] or list(chars.keys())
    base["version"] = VERSION
    return base


def _items(value):
    """(key, value) pairs of `value` if it really is a mapping, else nothing."""
    return list(value.items()) if isinstance(value, dict) else []


def _str_map(value):
    return dict((str(k), v) for k, v in _items(value) if isinstance(v, str))


def _sane_options(options):
    """Coerce the options a render does arithmetic on; a bad one costs its default."""
    defaults = new_project()["options"]
    for key, cast_to in (("max_chars", int), ("gap_seconds", float),
                         ("max_actors", int), ("scene_count", int),
                         ("turnaround_frames", int)):
        try:
            options[key] = cast_to(options.get(key, defaults[key]))
        except (TypeError, ValueError):
            options[key] = defaults[key]
        if options[key] < 0:                  # a negative width never stops chunking
            options[key] = defaults[key]
    if not isinstance(options.get("global_params"), dict):
        options["global_params"] = {}
    return options


def save(project, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(project, f, indent=2, ensure_ascii=False)
    return path


def cast(project):
    """Actors in cast order."""
    chars = project.get("characters") or {}
    order = [n for n in (project.get("cast_order") or []) if n in chars]
    order += [n for n in chars if n not in order]
    return [chars[n] for n in order]


def num_option(options, key, default):
    """One numeric option, never raising - projects are hand-edited."""
    try:
        value = float((options or {}).get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value == value else default          # NaN


def workflow_cfg(project, slot):
    return (project.get("workflows") or {}).get(slot) or {"path": "", "mapping": {}}


# ---------------- workflow handling ----------------

def load_workflow(path):
    """Load a ComfyUI workflow saved in *API format* and validate it."""
    with open(path, "r", encoding="utf-8") as f:
        wf = json.load(f)
    if isinstance(wf, dict) and "nodes" in wf and "links" in wf:
        raise ValueError(
            "That looks like a UI workflow, not an API workflow.\n\n"
            "In ComfyUI enable Settings > Dev mode, then use "
            "'Workflow > Export (API)' and pick that file instead.")
    if not isinstance(wf, dict) or not wf:
        raise ValueError("Not a ComfyUI API workflow.")
    for k, v in wf.items():
        if not isinstance(v, dict) or "class_type" not in v:
            raise ValueError("Node %s has no class_type - not an API workflow." % k)
    return wf


def widget_inputs(workflow):
    """[(target, class_type, input_name, value)] for every editable widget input.

    `target` is the "node_id.input_name" string used in mappings. Inputs wired
    from other nodes (list values) are skipped - they can't be overridden.
    """
    rows = []
    for node_id in sorted(workflow, key=_int_key):
        node = workflow[node_id]
        cls = node.get("class_type", "?")
        for name, val in (node.get("inputs") or {}).items():
            if isinstance(val, list):
                continue
            rows.append(("%s.%s" % (node_id, name), cls, name, val))
    return rows


def describe(workflow, target):
    if not target:
        return ""
    node_id, _, name = target.partition(".")
    node = (workflow or {}).get(node_id) or {}
    return "%s  #%s  .%s" % (node.get("class_type", "?"), node_id, name)


def guess_mapping(workflow, slot="voice"):
    """Best-effort auto-detect of the inputs a slot needs."""
    rows = widget_inputs(workflow)
    keys = [k for k, _, _ in WORKFLOW_SLOTS[slot]["keys"]]
    guess = dict((k, "") for k in keys)

    def score_row(key, target, cls, name, val):
        n, c = name.lower(), cls.lower()
        negative = any(w in n for w in NEGATIVE_WORDS)
        if key == "negative" and not negative:
            return -1
        if key in ("text", "prompt") and negative:
            return -1
        # A file-valued input must be named for its job. Matching on the class
        # alone lands on things like EmptyLatentImage.width, which would be
        # overwritten with a filename and break the render.
        if key in ("voice", "image"):
            if not isinstance(val, str):
                return -1
            if not any(h in n for h in HINTS[key]):
                return -1

        score = 0
        for h in HINTS.get(key, ()):
            if n == h:
                score += 6
            elif h in n:
                score += 3
            if h in c and key not in ("voice", "image"):
                score += 1
        if score <= 0:
            return -1
        if key == "seed":
            return score + 4 if isinstance(val, (int, float)) and not isinstance(val, bool) else -1
        if key in ("text", "prompt", "negative"):
            if not isinstance(val, str):
                return -1
            score += 2 + min(3, len(val) // 24)
        return score

    used = set()
    # A sampler names its own conditioning: follow those links rather than guessing.
    linked = _linked_prompts(workflow)
    if "prompt" in keys and linked.get("positive"):
        guess["prompt"] = linked["positive"]
        used.add(linked["positive"])
    if "negative" in keys and linked.get("negative"):
        guess["negative"] = linked["negative"]
        used.add(linked["negative"])

    for key in ("negative", "seed", "voice", "image", "text", "prompt"):
        if guess.get(key):
            continue
        if key not in keys:
            continue
        best, best_score = "", 0
        for target, cls, name, val in rows:
            if target in used:
                continue
            s = score_row(key, target, cls, name, val)
            if s > best_score:
                best, best_score = target, s
        if best:
            guess[key] = best
            used.add(best)
    return guess


def _linked_prompts(workflow):
    """Find the positive / negative text boxes by following the sampler's links.

    In a normal image workflow both prompts sit in identical `text` widgets, so
    the only reliable signal is which one the sampler calls 'negative'.
    """
    out = {}
    for node in workflow.values():
        for side in ("positive", "negative"):
            link = (node.get("inputs") or {}).get(side)
            if not (isinstance(link, list) and link and isinstance(link[0], str)):
                continue
            target = _first_text_widget(workflow, link[0])
            if target and side not in out:
                out[side] = target
    return out


def _first_text_widget(workflow, node_id, depth=0):
    """The text widget of a node, following one hop through wrapper nodes."""
    node = workflow.get(node_id) or {}
    for name, val in (node.get("inputs") or {}).items():
        if isinstance(val, str) and name in ("text", "string", "prompt", "text_g", "text_l"):
            return "%s.%s" % (node_id, name)
    if depth < 2:
        for val in (node.get("inputs") or {}).values():
            if isinstance(val, list) and val and isinstance(val[0], str):
                found = _first_text_widget(workflow, val[0], depth + 1)
                if found:
                    return found
    return ""


def apply_value(workflow, target, value):
    """Set node_id.input = value. Returns True if the input existed."""
    if not target:
        return False
    node_id, _, name = target.partition(".")
    node = workflow.get(node_id)
    if not node or "inputs" not in node or name not in node["inputs"]:
        return False
    current = node["inputs"].get(name)
    if isinstance(current, bool):
        pass
    elif isinstance(current, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            pass
    elif isinstance(current, float):
        try:
            value = float(value)
        except (TypeError, ValueError):
            pass
    node["inputs"][name] = value
    return True


def clone(workflow):
    return copy.deepcopy(workflow)


def _int_key(s):
    try:
        return (0, int(s))
    except (TypeError, ValueError):
        return (1, str(s))


# ==========================================================================
# jobs: One queued ComfyUI prompt, for any slot (voice, portrait, turnaround, shot).
# ==========================================================================

"""One queued ComfyUI prompt, for any slot (voice, portrait, turnaround, shot)."""

import os
import random

# (flattened) from . import project as proj
# (flattened) from .comfy import ComfyError

AUDIO = ("audio",)
IMAGE = ("images", "gifs", "video", "videos", "animated")


class SlotRunner(object):
    """Loads a slot's workflow once, then renders values through it repeatedly."""

    def __init__(self, client, project, slot):
        self.client = client
        self.slot = slot
        cfg = proj.workflow_cfg(project, slot)
        self.path = cfg.get("path") or ""
        self.mapping = dict(cfg.get("mapping") or {})
        self.label = proj.WORKFLOW_SLOTS[slot]["label"]
        if not self.path or not os.path.exists(self.path):
            raise ComfyError(
                "No workflow set for '%s'.\nPick one on the Workflows tab." % self.label)
        self.workflow = proj.load_workflow(self.path)
        missing = [k for k, _, required in proj.WORKFLOW_SLOTS[slot]["keys"]
                   if required and not self.mapping.get(k)]
        if missing:
            raise ComfyError("The '%s' workflow has no %s input mapped yet."
                             % (self.label, " or ".join(missing)))
        self._uploads = {}

    # ------------------------------------------------------------------ inputs

    def upload(self, local_path):
        """Put a local file in ComfyUI's input folder once; return its Comfy name."""
        if not local_path or not os.path.exists(local_path):
            return ""
        if local_path not in self._uploads:
            self._uploads[local_path] = self.client.upload_audio(local_path)
        return self._uploads[local_path]

    def has(self, key):
        return bool(self.mapping.get(key))

    def _apply(self, job, values, params):
        """Write a slot's values into a copy of the workflow.

        A mapping is saved once and the workflow can be re-exported later with
        that node gone. Silently dropping the value would leave the workflow's
        own placeholder text in place, so the render would succeed and be wrong.
        """
        stale = []
        for key, value in (values or {}).items():
            target = self.mapping.get(key)
            if target and value not in (None, ""):
                if not proj.apply_value(job, target, value):
                    stale.append("%s -> %s" % (key, target))
        if self.has("seed"):
            seed = (values or {}).get("seed")
            try:
                seed = int(seed)
            except (TypeError, ValueError):
                seed = -1
            if seed < 0:
                seed = random.randint(0, 2 ** 31 - 1)
            if not proj.apply_value(job, self.mapping["seed"], seed):
                stale.append("seed -> %s" % self.mapping["seed"])
        if stale:
            raise ComfyError(
                "The '%s' workflow no longer has the input%s this project is mapped to:\n"
                "  %s\nRe-map it on the Workflows tab."
                % (self.label, "" if len(stale) == 1 else "s", "\n  ".join(stale)))
        for target, value in (params or {}).items():
            proj.apply_value(job, target, value)      # extras are best-effort
        return job

    # ------------------------------------------------------------------- render

    def run(self, values, dest_no_ext, kinds, params=None, cancel=None, index=0):
        """Queue one prompt and save its first matching output next to dest_no_ext.

        `values` is keyed by slot key ("prompt", "text", "seed", ...). Returns the
        path actually written (the extension comes from ComfyUI's output).
        """
        job = self._apply(proj.clone(self.workflow), values, params)

        pid = self.client.queue_prompt(job)
        entry = self.client.wait_for(pid, cancel=cancel)
        outs = self.client.file_outputs(entry, kinds)
        if not outs:
            raise ComfyError(
                "The '%s' workflow produced no %s output.\nIt needs a Save or Preview node "
                "at the end." % (self.label, "audio" if kinds is AUDIO else "image"))
        ref = outs[min(index, len(outs) - 1)]
        ext = os.path.splitext(ref["filename"])[1] or (".wav" if kinds is AUDIO else ".png")
        dest = dest_no_ext + ext
        self.client.download(ref, dest)
        return dest

    def run_many(self, values, dest_no_ext, kinds, params=None, cancel=None):
        """Like run(), but keeps every output of the prompt (multi-view workflows)."""
        job = self._apply(proj.clone(self.workflow), values, params)

        pid = self.client.queue_prompt(job)
        entry = self.client.wait_for(pid, cancel=cancel)
        outs = self.client.file_outputs(entry, kinds)
        saved = []
        for i, ref in enumerate(outs):
            ext = os.path.splitext(ref["filename"])[1] or ".png"
            dest = "%s_%02d%s" % (dest_no_ext, i, ext)
            self.client.download(ref, dest)
            saved.append(dest)
        return saved


# ==========================================================================
# worker: Base class for the background jobs the GUI runs (render, casting, movie).
# ==========================================================================

"""Base class for the background jobs the GUI runs (render, casting, movie)."""

import threading
import traceback


class Worker(threading.Thread):
    """A cancellable job that reports progress through a single callback.

    Every callback fires on the worker thread; the GUI queues them and handles
    them on the main loop.
    """

    daemon = True
    kind = "job"

    def __init__(self, on_event):
        threading.Thread.__init__(self)
        self.on_event = on_event
        self._cancel = threading.Event()
        self.error = None
        self.result = {}

    # ----------------------------------------------------------- cancellation

    def cancel(self):
        self._cancel.set()

    def cancelled(self):
        return self._cancel.is_set()

    # --------------------------------------------------------------- reporting

    def emit(self, kind, **kw):
        kw["kind"] = kind
        kw.setdefault("job", self.kind)
        try:
            self.on_event(kw)
        except Exception:
            pass

    def log(self, msg):
        self.emit("log", message=str(msg))

    def step(self, label, done=0, total=0):
        self.emit("step", label=label, done=done, total=total)

    # -------------------------------------------------------------- execution

    def execute(self):                                    # pragma: no cover
        raise NotImplementedError

    def run(self):
        try:
            self.execute()
            self.emit("finished", **self.result)
        except Exception as e:                            # surfaced in the GUI
            if self.cancelled() or "cancel" in str(e).lower():
                self.emit("failed", message="Cancelled.", cancelled=True)
                return
            # ComfyError / LLMError messages are already written for humans
            self.error = str(e) if isinstance(e, RuntimeError) else "%s: %s" % (
                type(e).__name__, e)
            self.log(traceback.format_exc(limit=4).strip().splitlines()[-1])
            self.emit("failed", message=self.error, cancelled=False)


# ==========================================================================
# visuals: Actor visuals: the portrait, and the full spin-around turnaround.
# ==========================================================================

"""Actor visuals: the portrait, and the full spin-around turnaround."""

import os
import re

# (flattened) from . import casting
# (flattened) from .jobs import IMAGE

try:                                   # optional; only used for the animated GIF
    from PIL import Image
    HAVE_PIL = True
except Exception:                      # pragma: no cover - Pillow is not required
    HAVE_PIL = False


def slug(name):
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name)).strip("_")
    return (s or "actor")[:32]


def actor_dir(out_dir, name):
    d = os.path.join(out_dir, "cast", slug(name))
    os.makedirs(d, exist_ok=True)
    return d


def identity_image(actor):
    """The picture that says who this character is, or "".

    A reference the user chose wins over anything generated - it is the whole
    point of setting one. Otherwise the locked portrait, and failing that the
    first turnaround frame, which is the same face from the front.
    """
    for path in (actor.get("reference_image", ""),
                 actor.get("portrait", ""),
                 (actor.get("turnaround") or [""])[0]):
        if path and os.path.exists(path):
            return path
    return ""


def render_portrait(runner, actor, out_dir, cancel=None):
    """One locked portrait: same appearance words + same look seed every time."""
    d = actor_dir(out_dir, actor["name"])
    return runner.run(
        {"prompt": casting.look_prompt(actor),
         "negative": casting.NEGATIVE,
         "seed": actor.get("look_seed", -1)},
        os.path.join(d, "portrait"), IMAGE, cancel=cancel)


def render_turnaround(runner, actor, out_dir, frames=8, cancel=None, on_frame=None,
                      reference=""):
    """A full spin around the character: one frame per angle, one fixed seed.

    If the workflow is a real multi-view / orbit workflow (it returns several
    images from a single prompt), those images are used as the spin instead.
    """
    d = actor_dir(out_dir, actor["name"])
    angles = _angles(frames)
    prompts = casting.turnaround_prompts(actor, angles)
    seed = actor.get("look_seed", -1)

    ref_name = ""
    if runner.has("image") and reference and os.path.exists(reference):
        ref_name = runner.upload(reference)

    first_values = {"prompt": prompts[0][1], "negative": casting.NEGATIVE, "seed": seed}
    if ref_name:
        first_values["image"] = ref_name
    produced = runner.run_many(first_values, os.path.join(d, "spin_000"), IMAGE, cancel=cancel)

    if len(produced) >= 4:                       # the workflow orbits by itself
        out = []
        for i, src in enumerate(produced):
            dest = os.path.join(d, "spin_%03d%s" % (i, os.path.splitext(src)[1]))
            if os.path.abspath(src) != os.path.abspath(dest):
                os.replace(src, dest)
            else:
                dest = src
            out.append(dest)
            if on_frame:
                on_frame(i, len(produced), dest)
        return out

    files = []
    if produced:
        first = os.path.join(d, "spin_000%s" % os.path.splitext(produced[0])[1])
        if os.path.abspath(produced[0]) != os.path.abspath(first):
            os.replace(produced[0], first)
        files.append(first)
        if on_frame:
            on_frame(0, len(prompts), first)
        for extra in produced[1:]:
            try:
                os.remove(extra)
            except OSError:
                pass

    for i, (deg, prompt) in enumerate(prompts[1:], start=1):
        if cancel is not None and cancel():
            break
        values = {"prompt": prompt, "negative": casting.NEGATIVE, "seed": seed}
        if ref_name:
            values["image"] = ref_name
        path = runner.run(values, os.path.join(d, "spin_%03d" % i), IMAGE, cancel=cancel)
        files.append(path)
        if on_frame:
            on_frame(i, len(prompts), path)
    return files


def make_gif(frames, dest, ms=120):
    """Animated GIF of the spin. Needs Pillow; returns "" without it."""
    frames = [f for f in frames if f and os.path.exists(f)]
    if not HAVE_PIL or len(frames) < 2:
        return ""
    imgs = []
    for f in frames:
        try:
            im = Image.open(f).convert("RGB")
        except Exception:
            continue
        im.thumbnail((512, 512))
        imgs.append(im)
    if len(imgs) < 2:
        return ""
    imgs[0].save(dest, save_all=True, append_images=imgs[1:], duration=ms, loop=0,
                 optimize=True)
    return dest


def _angles(frames):
    frames = max(2, min(24, int(frames or 8)))
    if frames == len(casting.ANGLES):
        return casting.ANGLES
    step = 360.0 / frames
    out = []
    for i in range(frames):
        deg = int(round(i * step))
        out.append((deg, _phrase(deg)))
    return out


def _phrase(deg):
    deg %= 360
    for lo, hi, text in (
            (338, 361, "front view, facing the camera directly"),
            (0, 23, "front view, facing the camera directly"),
            (23, 68, "three-quarter view turned slightly to their left"),
            (68, 113, "full left profile view"),
            (113, 158, "three-quarter rear view from their left"),
            (158, 203, "back view, facing away from the camera"),
            (203, 248, "three-quarter rear view from their right"),
            (248, 293, "full right profile view"),
            (293, 338, "three-quarter view turned slightly to their right")):
        if lo <= deg < hi:
            return text
    return "front view, facing the camera directly"


# ==========================================================================
# movie: Assembling the finished movie: one segment per spoken line, then a concat.
# ==========================================================================

"""Assembling the finished movie: one segment per spoken line, then a concat.

Assembly uses ffmpeg when it can be found. Without it, the audio, the shots and
an edit list (movie.edl.json) are still written, so nothing is lost - the project
can be assembled later, or opened in any editor.
"""

import json
import os
import shutil
import subprocess
import sys

# (flattened) from . import audio

VIDEO_EXT = (".mp4", ".webm", ".mkv", ".mov", ".gif", ".webp")
FRAME_SIZE = (1280, 720)


def find_ffmpeg(extra_hints=()):
    """Look on PATH, then in the usual ComfyUI / imageio places."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    hints = list(extra_hints) + [
        r"C:\ComfyUI", r"C:\ComfyUI_windows_portable",
        os.path.expanduser(r"~\ComfyUI"),
        os.path.expanduser(r"~\Documents\ComfyUI"),
        os.path.expanduser(r"~\AppData\Local\Programs\@comfyorgcomfyui-electron"),
    ]
    exe = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    for root in hints:
        if not root or not os.path.isdir(root):
            continue
        for sub in ("", "ffmpeg", "bin", os.path.join("python_embeded", "Scripts"),
                    os.path.join("venv", "Scripts")):
            cand = os.path.join(root, sub, exe)
            if os.path.exists(cand):
                return cand
    return ""


def have_pyav():
    """PyAV carries its own ffmpeg libraries, so no binary need be installed."""
    try:
        import av                                  # noqa: F401
        return True
    except Exception:
        return False


def assemble_pyav(entries, dest, gap_seconds=0.35, size=FRAME_SIZE, fps=25, log=None,
                  cancel=None):
    """Cut the movie with PyAV: each line's picture held for the length of its clip.

    Same result as the ffmpeg path without shelling out - one video stream of
    still frames, one audio stream, muxed straight to mp4.
    """
    import av

    log = log or (lambda m: None)
    usable = [e for e in entries if e.get("audio") and os.path.exists(e["audio"])]
    if not usable:
        raise RuntimeError("No dialogue to cut - render the audio first.")

    rate = _first_audio_rate(usable)
    container = av.open(dest, "w")
    try:
        vs = container.add_stream("libx264", rate=fps)
        vs.width, vs.height = size
        vs.pix_fmt = "yuv420p"
        vs.options = {"crf": "20", "preset": "veryfast"}
        aud = container.add_stream("aac", rate=rate, layout="mono")

        frame_index = 0
        resampler = av.audio.resampler.AudioResampler(format="fltp", layout="mono", rate=rate)
        # AAC only accepts frames of exactly frame_size samples, so decoded
        # audio goes through a fifo rather than straight into the encoder.
        fifo = av.audio.fifo.AudioFifo()
        chunk = aud.frame_size or 1024

        def drain(partial=False):
            while True:
                block = fifo.read(chunk, partial=partial) if not partial else fifo.read_many(chunk, partial=True)
                if block is None:
                    return
                for one in (block if isinstance(block, list) else [block]):
                    for packet in aud.encode(one):
                        container.mux(packet)
                if partial:
                    return

        for n, e in enumerate(usable):
            if cancel is not None and cancel():
                raise RuntimeError("Cancelled")
            seconds = audio.duration(e["audio"]) + max(0.0, float(gap_seconds))
            picture = _still(e.get("image", ""), size)
            for _ in range(max(1, int(round(seconds * fps)))):
                picture.pts = frame_index
                frame_index += 1
                for packet in vs.encode(picture):
                    container.mux(packet)
            for frame in _audio_frames(e["audio"], resampler, seconds, rate):
                fifo.write(frame)
            drain()
            if n % 25 == 0:
                log("cut %d/%d" % (n + 1, len(usable)))

        drain(partial=True)
        for packet in vs.encode():
            container.mux(packet)
        for packet in aud.encode():
            container.mux(packet)
    finally:
        container.close()
    return dest


def _first_audio_rate(entries):
    import wave
    for e in entries:
        try:
            with wave.open(e["audio"]) as w:
                return w.getframerate()
        except Exception:
            continue
    return 22050


def _still(path, size):
    """One video frame: the shot, letterboxed, or black when there is no shot."""
    import av
    if path and os.path.exists(path) and os.path.splitext(path)[1].lower() not in VIDEO_EXT:
        with av.open(path) as src:
            for frame in src.decode(video=0):
                # Rebuilt from its pixels rather than reformatted in place: a
                # decoded still carries the time base of the file it came from,
                # which makes every pts we set invalid, and time_base cannot be
                # cleared on a frame. A fresh frame simply has none.
                pixels = frame.to_ndarray(format="rgb24")
                return av.VideoFrame.from_ndarray(pixels, format="rgb24").reformat(
                    width=size[0], height=size[1], format="yuv420p")
    return black_frame(size)


def black_frame(size):
    """A genuinely black frame.

    av.VideoFrame(w, h, fmt) allocates without initialising, so using one
    directly puts uninitialised memory - static - on screen.
    """
    import av
    import numpy as np
    return av.VideoFrame.from_ndarray(
        np.zeros((size[1], size[0], 3), dtype="uint8"), format="rgb24").reformat(
            format="yuv420p")


def _audio_frames(path, resampler, seconds=0.0, rate=22050):
    """The clip's audio, then silence to fill the gap the picture holds for."""
    import av
    import numpy as np

    spoken = 0
    with av.open(path) as src:
        for frame in src.decode(audio=0):
            for out in resampler.resample(frame):
                spoken += out.samples
                out.pts = None
                yield out
    pad = int(round(seconds * rate)) - spoken
    while pad > 0:
        block = min(pad, 1024)
        silence = av.AudioFrame.from_ndarray(
            np.zeros((1, block), dtype="float32"), format="fltp", layout="mono")
        silence.rate = rate
        silence.pts = None
        yield silence
        pad -= block


def write_edl(path, entries, full_audio, fps=25):
    """A plain edit list: what plays when, in order. Readable by humans and code."""
    t = 0.0
    rows = []
    for e in entries:
        dur = float(e.get("seconds") or 0.0)
        rows.append({
            "index": e.get("index"),
            "speaker": e.get("speaker"),
            "line": e.get("text"),
            "shot": e.get("shot", ""),
            "image": e.get("image", ""),
            "audio": e.get("audio", ""),
            "start": round(t, 3),
            "duration": round(dur, 3),
        })
        t += dur
    data = {"fps": fps, "total_seconds": round(t, 3), "audio": full_audio, "shots": rows}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def assemble(ffmpeg, entries, dest, gap_seconds=0.35, size=FRAME_SIZE, fps=25, log=None,
             cancel=None):
    """Build one segment per line (visual + its own audio), then concatenate.

    Returns the finished file path. Raises RuntimeError with ffmpeg's own message
    if a step fails.
    """
    log = log or (lambda m: None)
    work = os.path.join(os.path.dirname(dest), "_segments")
    os.makedirs(work, exist_ok=True)
    segments = []

    for e in entries:
        if cancel is not None and cancel():
            raise RuntimeError("Cancelled")
        clip = e.get("audio", "")
        visual = e.get("image", "")
        if not clip or not os.path.exists(clip):
            continue
        dur = audio.duration(clip) + max(0.0, float(gap_seconds))
        seg = os.path.join(work, "seg_%04d.mp4" % (e.get("index", 0) + 1))
        vf = ("scale=%d:%d:force_original_aspect_ratio=decrease,"
              "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
              % (size[0], size[1], size[0], size[1]))

        if visual and os.path.exists(visual):
            if os.path.splitext(visual)[1].lower() in VIDEO_EXT:
                cmd = [ffmpeg, "-y", "-stream_loop", "-1", "-i", visual, "-i", clip,
                       "-t", "%.3f" % dur, "-vf", vf, "-r", str(fps)]
            else:
                cmd = [ffmpeg, "-y", "-loop", "1", "-i", visual, "-i", clip,
                       "-t", "%.3f" % dur, "-vf", vf, "-r", str(fps)]
        else:                                   # audio only: hold on black
            cmd = [ffmpeg, "-y", "-f", "lavfi", "-i",
                   "color=c=black:s=%dx%d:r=%d" % (size[0], size[1], fps),
                   "-i", clip, "-t", "%.3f" % dur]

        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                "-shortest", "-movflags", "+faststart", seg]
        _run(cmd, "segment %d" % (e.get("index", 0) + 1))
        segments.append(seg)
        log("segment %d/%d" % (len(segments), len(entries)))

    if not segments:
        raise RuntimeError("No segments to assemble - render the dialogue first.")

    list_path = os.path.join(work, "segments.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for s in segments:
            f.write("file '%s'\n" % s.replace("\\", "/").replace("'", "'\\''"))
    _run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
          "-c", "copy", "-movflags", "+faststart", dest], "final concat")
    return dest


def _run(cmd, what):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           creationflags=_no_window())
    except OSError as e:
        raise RuntimeError("Couldn't start ffmpeg for %s: %s" % (what, e))
    if p.returncode != 0:
        tail = (p.stdout or b"").decode("utf-8", "replace").strip().splitlines()[-8:]
        raise RuntimeError("ffmpeg failed on %s:\n%s" % (what, "\n".join(tail)))


def _no_window():
    return 0x08000000 if sys.platform.startswith("win") else 0     # CREATE_NO_WINDOW


# ==========================================================================
# render: Runs a parsed script through ComfyUI's TTS workflow, one cue at a time.
# ==========================================================================

"""Runs a parsed script through ComfyUI's TTS workflow, one cue at a time."""

import hashlib
import json
import os
import re
import wave

# (flattened) from . import audio, project as proj, speech
# (flattened) from .comfy import ComfyClient, ComfyError
# (flattened) from .jobs import AUDIO, SlotRunner
# (flattened) from .worker import Worker


def cue_key(cue, character, mapping, workflow_sig):
    """Stable fingerprint of everything that affects this cue's audio.

    On the Windows-voice backend the audio is decided by the *resolved* voice
    settings, which come from `voice_type` and from any `system_voice` override
    - neither of which appears in the fields below. `mapping` carries those
    resolved settings for that backend (RenderJob passes them in), so changing
    a character's range re-renders the clip instead of re-using the old voice.
    """
    blob = json.dumps([
        cue.speaker, cue.text,
        character.get("voice_file", ""), character.get("voice_value", ""),
        character.get("seed", -1), character.get("params", {}),
        mapping, workflow_sig,
    ], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def system_voice(character):
    """The Windows voice settings for one character, honouring any override."""
    override = character.get("system_voice") or {}
    if override.get("voice"):
        return {"voice": override["voice"], "rate": int(override.get("rate", 0)),
                "pitch": int(override.get("pitch", 0))}
    seed = character.get("seed", -1)
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = -1
    if seed < 0:
        seed = speech.stable_seed(character.get("name", ""),
                                  character.get("voice_type", ""))
    return speech.assign_voice(seed, hint=character.get("voice_type", ""))


class RenderJob(Worker):
    """Renders every cue to its own clip, then stitches the full take."""

    kind = "voice"

    def __init__(self, project, cues, out_dir, on_event):
        Worker.__init__(self, on_event)
        self.project = project
        self.cues = cues
        self.out_dir = out_dir
        self.files = []
        self.stitched = None
        self.quiet = False          # set by MovieJob: skip the final stitch chatter

    def execute(self):
        p = self.project
        opts = p.get("options") or {}
        use_system = (opts.get("voice_backend") or "comfyui") == "system"

        runner = None
        if use_system:
            if not speech.available():
                raise ComfyError(
                    "The system voice was chosen, but Windows speech isn't available here.")
            self.log("Speaking with the built-in Windows voices: %s"
                     % ", ".join(speech.voices()))
        else:
            client = ComfyClient(p["server"]["host"], p["server"]["port"])
            self.log(client.ping())
            runner = SlotRunner(client, p, "voice")

        os.makedirs(self.out_dir, exist_ok=True)
        manifest_path = os.path.join(self.out_dir, "manifest.json")
        manifest = _read_json(manifest_path)
        workflow_sig = "system" if use_system else hashlib.sha1(
            json.dumps(runner.workflow, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        reuse = bool(opts.get("reuse_unchanged", True))

        # each character's reference clip goes up once
        uploaded = {}
        if runner and opts.get("upload_voices", True) and runner.has("voice"):
            for name, ch in (p.get("characters") or {}).items():
                vf = ch.get("voice_file")
                if vf and os.path.exists(vf) and vf not in uploaded:
                    self.log("Uploading voice reference for %s: %s"
                             % (name, os.path.basename(vf)))
                    uploaded[vf] = runner.upload(vf)

        total = len(self.cues)
        for i, cue in enumerate(self.cues):
            if self.cancelled():
                raise ComfyError("Cancelled")
            ch = (p.get("characters") or {}).get(cue.speaker) or proj.new_character(cue.speaker)
            voice = system_voice(ch) if use_system else None
            if not str(cue.text).strip():
                # A blank cue is nothing to say, not a reason to lose the take.
                self.log("Cue %d has no text - skipping it." % (i + 1))
                self.emit("cue_done", index=i, total=total, speaker=cue.speaker,
                          text=cue.text, file="", cached=True, skipped=True)
                continue
            if use_system:
                # The resolved voice, plus what it was resolved from: a clip
                # cached under the old range must not survive a re-cast.
                sig = dict(voice, voice_type=ch.get("voice_type", ""),
                           system_voice=ch.get("system_voice") or {})
            else:
                sig = runner.mapping
            key = cue_key(cue, ch, sig, workflow_sig)
            prev = manifest.get(str(i))
            if reuse and prev and prev.get("key") == key and os.path.exists(prev.get("file", "")):
                self.files.append(prev["file"])
                self.emit("cue_done", index=i, total=total, speaker=cue.speaker,
                          text=cue.text, file=prev["file"], cached=True)
                continue

            stem = os.path.join(self.out_dir, "%04d_%s" % (i + 1, clip_slug(cue.speaker)))
            self.emit("cue_start", index=i, total=total, speaker=cue.speaker, text=cue.text)

            if use_system:
                dest = speech.speak_to_wav(cue.text, stem + ".wav", voice=voice["voice"],
                                           rate=voice["rate"], pitch=voice["pitch"])
            else:
                values = {"text": cue.text, "seed": ch.get("seed", -1)}
                if runner.has("voice"):
                    vf = ch.get("voice_file") or ""
                    if vf and vf in uploaded:
                        values["voice"] = uploaded[vf]
                    elif ch.get("voice_value"):
                        values["voice"] = ch["voice_value"]
                params = dict(opts.get("global_params") or {})
                params.update(ch.get("params") or {})
                dest = runner.run(values, stem, AUDIO, params=params, cancel=self.cancelled)

            self.files.append(dest)
            manifest[str(i)] = {"key": key, "file": dest, "speaker": cue.speaker,
                                "text": cue.text}
            _write_json(manifest_path, manifest)
            self.emit("cue_done", index=i, total=total, speaker=cue.speaker,
                      text=cue.text, file=dest, cached=False, seconds=audio.duration(dest))

        if self.files:
            out = os.path.join(self.out_dir, "full_take.wav")
            try:
                audio.concat_wavs(self.files, out,
                                  proj.num_option(opts, "gap_seconds", 0.35),
                                  trim=use_system)
                self.stitched = out
                self.log("Stitched %d clips -> %s (%.1fs)"
                         % (len(self.files), out, audio.duration(out)))
            except (audio.AudioError, ValueError, OSError, wave.Error) as e:
                self.log("Individual clips are fine, but stitching failed: %s" % e)
        self.result = {"files": self.files, "stitched": self.stitched}


def clip_slug(name):
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name)).strip("_")
    return (s or "LINE")[:24]


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ==========================================================================
# pipeline: The AI jobs: build the cast from a premise, regenerate one actor or one
# ==========================================================================

"""The AI jobs: build the cast from a premise, regenerate one actor or one
voice, and render the finished movie once the cast is approved."""

import hashlib
import json
import random
import os
import re

# (flattened) from . import casting, movie, project as proj, script_parser, speech, visuals
# (flattened) from .audio import duration
# (flattened) from . import comfy as comfy_mod
# (flattened) from .comfy import ComfyClient, ComfyError
# (flattened) from .jobs import AUDIO, IMAGE, SlotRunner
# (flattened) from . import llm as llm_mod
# (flattened) from .llm import LLMError, LocalLLM
# (flattened) from .render import RenderJob
# (flattened) from .worker import Worker

CAST_STEPS = ("cast", "roles", "portrait", "spin", "voice")


def make_llm(project):
    cfg = project.get("llm") or {}
    if not cfg.get("base_url"):
        raise LLMError(
            "No local model server selected yet.\n\n"
            "Start LM Studio (Developer > Start Server) or run `ollama serve`, "
            "then press Detect on the Premise tab.")
    return LocalLLM(cfg["base_url"], cfg.get("model", ""),
                    temperature=float(cfg.get("temperature", 0.85)))


def stable_voice_seed(actor):
    """A voice seed derived from who the character is: same actor, same voice."""
    return speech.stable_seed(actor.get("name", ""), actor.get("voice_type", ""))


def _voice_input_wants_text(runner):
    """True when the workflow's voice input is a prose description, not a file."""
    target = runner.mapping.get("voice")
    if not target:
        return False
    node_id, _, name = target.partition(".")
    cur = (runner.workflow.get(node_id) or {}).get("inputs", {}).get(name)
    if not isinstance(cur, str):
        return False
    if re.search(r"\.(wav|mp3|flac|ogg|m4a)$", cur.strip(), re.I):
        return False
    return len(cur.strip()) > 24


class _VisualMixin(object):
    """Shared portrait / turnaround / voice-sample rendering."""

    def _client(self, required=True):
        """Connect to ComfyUI, looking on the usual ports before giving up.

        With required=False the caller carries on without pictures rather than
        throwing away the text work it has already done.
        """
        p = self.project
        host = p["server"]["host"]
        port = p["server"]["port"]
        try:
            c = ComfyClient(host, port)
            c.ping()
            return c
        except ComfyError as first:
            found = comfy_mod.find_server(host)
            if found and found != port:
                self.log("ComfyUI wasn't on port %s; found it on %d." % (port, found))
                p["server"]["port"] = found
                c = ComfyClient(host, found)
                c.ping()
                return c
            if required:
                raise
            self.log("ComfyUI is not answering on %s:%s, so nothing was drawn or "
                     "spoken. %s" % (host, port, str(first).splitlines()[0]))
            return None

    def _runner(self, client, slot, required=True):
        try:
            return SlotRunner(client, self.project, slot)
        except ComfyError as e:
            if required:
                raise
            self.log("Skipping %s: %s" % (slot, str(e).splitlines()[0]))
            return None

    def progress(self, name, label, done=0, total=0):
        """Per-actor progress, for the bar on that actor's card."""
        self.emit("actor_progress", name=name, label=label, done=done, total=total)

    def render_portrait(self, runner, actor):
        self.progress(actor["name"], "Drawing the portrait...")
        path = visuals.render_portrait(runner, actor, self.out_dir, cancel=self.cancelled)
        actor["portrait"] = path
        self.emit("asset", name=actor["name"], asset="portrait", path=path)
        return path

    def render_spin(self, runner, actor, frames):
        self.progress(actor["name"], "Turnaround frame 1 of %d" % max(1, frames), 0, frames)

        def on_frame(i, total, path):
            self.emit("asset", name=actor["name"], asset="spin_frame", path=path,
                      index=i, total=total)
            self.step("%s turnaround" % actor["name"], i + 1, total)
            self.progress(actor["name"], "Turnaround frame %d of %d" % (i + 1, total),
                          i + 1, total)

        files = visuals.render_turnaround(
            runner, actor, self.out_dir, frames=frames, cancel=self.cancelled,
            on_frame=on_frame, reference=visuals.identity_image(actor))
        actor["turnaround"] = files
        gif = visuals.make_gif(
            files, os.path.join(visuals.actor_dir(self.out_dir, actor["name"]), "spin.gif"))
        if gif:
            actor["spin_gif"] = gif
        self.emit("asset", name=actor["name"], asset="spin", path=gif or "", files=files)
        return files

    def render_voice_sample(self, runner, actor):
        self.progress(actor["name"], "Recording the voice sample...")
        line = actor.get("sample_line") or "Hello. This is how I sound in this film."
        if actor.get("seed", -1) is None or int(actor.get("seed", -1)) < 0:
            actor["seed"] = stable_voice_seed(actor)
        if (not actor.get("voice_file") and not actor.get("voice_value")
                and _voice_input_wants_text(runner) and actor.get("voice_type")):
            actor["voice_value"] = "%s. %s" % (actor.get("voice_type", ""),
                                               actor.get("voice_direction", "")).strip()
        values = {"text": line, "seed": actor["seed"]}
        if runner.has("voice"):
            if actor.get("voice_file") and os.path.exists(actor["voice_file"]):
                values["voice"] = runner.upload(actor["voice_file"])
            elif actor.get("voice_value"):
                values["voice"] = actor["voice_value"]
        d = visuals.actor_dir(self.out_dir, actor["name"])
        path = runner.run(values, os.path.join(d, "voice_sample"), AUDIO,
                          params=actor.get("params") or {}, cancel=self.cancelled)
        actor["voice_sample"] = path
        self.emit("asset", name=actor["name"], asset="voice_sample", path=path)
        return path


class _GpuMixin(object):
    """Hand the graphics card from the writer to the painter and back."""

    def _free_gpu_wanted(self):
        return bool((self.project.get("options") or {}).get("free_gpu"))

    def free_for_writing(self):
        """Drop ComfyUI's models so the language model has room to load."""
        if not self._free_gpu_wanted():
            return False
        try:
            client = ComfyClient(self.project["server"]["host"],
                                 self.project["server"]["port"])
            if client.free():
                self.log("Freed ComfyUI's VRAM for the model to write in.")
                return True
        except ComfyError:
            pass                      # ComfyUI not running is not a problem here
        return False

    def free_for_drawing(self):
        """Unload the language model so ComfyUI has room to draw."""
        if not self._free_gpu_wanted():
            return False
        if llm_mod.unload_lmstudio():
            self.log("Unloaded the language model to free the card for drawing.")
            return True
        return False


class CastJob(Worker, _VisualMixin, _GpuMixin):
    """Premise -> cast -> judgement -> portraits -> turnarounds -> voices."""

    kind = "cast"

    def __init__(self, project, out_dir, on_event, steps=CAST_STEPS, only=None):
        Worker.__init__(self, on_event)
        self.project = project
        self.out_dir = out_dir
        self.steps = tuple(steps)
        self.only = only              # limit visual work to these character names

    def _targets(self):
        actors = proj.cast(self.project)
        if self.only:
            actors = [a for a in actors if a["name"] in self.only]
        return actors

    def execute(self):
        p = self.project
        opts = p.get("options") or {}

        if "cast" in self.steps or "roles" in self.steps:
            self.free_for_writing()

        if "cast" in self.steps:
            llm = make_llm(p)
            self.step("Reading the premise")
            self.log("Casting with %s" % llm.ready())
            actors = casting.derive_cast(llm, p.get("premise", ""),
                                         max_actors=int(opts.get("max_actors", 5)),
                                         cancel=self.cancelled)
            chars = {}
            for a in actors:
                rec = proj.new_character(a["name"])
                rec.update(a)
                chars[a["name"]] = rec
            p["characters"] = chars
            p["cast_order"] = [a["name"] for a in actors]
            self.log("Cast: " + ", ".join(a["name"] for a in actors))
            self.emit("cast_updated")

        if "roles" in self.steps and p.get("characters"):
            llm = make_llm(p)
            self.step("Working out what each character does")
            cues = script_parser.parse(p.get("script", ""),
                                       default_speaker=(p.get("options") or {}).get(
                                           "default_speaker", "NARRATOR"))
            roles = casting.describe_roles(llm, p.get("premise", ""), proj.cast(p), cues,
                                           script=p.get("script", ""),
                                           cancel=self.cancelled)
            for name, role in roles.items():
                if name in p["characters"]:
                    p["characters"][name]["one_line"] = role
                    self.log("%-14s %s" % (name, role[:90]))
            self.emit("cast_updated")

        wants_visuals = ("portrait" in self.steps or "spin" in self.steps
                         or "voice" in self.steps)
        if not wants_visuals:
            self.result = {"cast": [a["name"] for a in proj.cast(p)]}
            return

        self.free_for_drawing()
        client = self._client(required=False)
        if client is None:
            # The writing is done and worth keeping; only the pictures are lost.
            self.result = {"cast": [a["name"] for a in proj.cast(p)], "visuals": False}
            return
        portrait = self._runner(client, "portrait", required=False) \
            if "portrait" in self.steps else None
        spin = self._runner(client, "turnaround", required=False) \
            if "spin" in self.steps else None
        if spin is None and portrait is not None and "spin" in self.steps:
            spin = portrait                       # a plain text-to-image workflow spins fine
        voice = self._runner(client, "voice", required=False) \
            if "voice" in self.steps else None

        targets = self._targets()
        for n, actor in enumerate(targets):
            if self.cancelled():
                raise ComfyError("Cancelled")
            self.step("%s (%d of %d)" % (actor["name"], n + 1, len(targets)), n, len(targets))
            if portrait:
                self.log("Portrait: %s" % actor["name"])
                self.render_portrait(portrait, actor)
            if spin:
                self.log("Turnaround: %s" % actor["name"])
                self.render_spin(spin, actor, int(opts.get("turnaround_frames", 8)))
            if voice:
                self.log("Voice sample: %s" % actor["name"])
                self.render_voice_sample(voice, actor)
            self.progress(actor["name"], "")
            self.emit("actor_updated", name=actor["name"])
        self.result = {"cast": [a["name"] for a in targets]}


class RegenerateJob(Worker, _VisualMixin, _GpuMixin):
    """The regenerate button next to one actor, or next to one voice."""

    kind = "regen"

    def __init__(self, project, out_dir, on_event, name, what="actor", note="",
                 keep_name=True):
        Worker.__init__(self, on_event)
        self.project = project
        self.out_dir = out_dir
        self.name = name
        self.what = what              # actor | voice | look | spin
        self.note = note
        self.keep_name = keep_name

    def execute(self):
        p = self.project
        actor = (p.get("characters") or {}).get(self.name)
        if not actor:
            raise ComfyError("%s is not in the cast any more." % self.name)
        opts = p.get("options") or {}

        if self.what in ("actor", "voice", "role"):
            self.free_for_writing()

        if self.what == "actor":
            llm = make_llm(p)
            self.step("Recasting %s" % self.name)
            self.progress(self.name, "Writing a new take on %s..." % self.name)
            cues = script_parser.parse(p.get("script", ""),
                                       default_speaker=(p.get("options") or {}).get(
                                           "default_speaker", "NARRATOR"))
            fresh = casting.recast_actor(llm, p.get("premise", ""), actor,
                                         proj.cast(p), self.note, cues=cues,
                                         script=p.get("script", ""),
                                         cancel=self.cancelled)
            if self.keep_name:
                fresh["name"] = self.name
            for field in casting.FIELDS + ("look_seed",):
                if field in ("lead", "look_note"):
                    continue              # the user's, not the model's
                actor[field] = fresh.get(field, actor.get(field))
            if actor.get("lead"):
                actor["role"] = "lead"    # a ticked box outranks whatever it wrote
            actor.update({"approved": False, "portrait": "", "turnaround": [],
                          "voice_sample": "", "seed": stable_voice_seed(actor)})
            self.progress(actor["name"], "Judging them against the plot...")
            fits = casting.judge_cast(llm, p.get("premise", ""), [actor],
                                      cancel=self.cancelled)
            actor["fit"] = fits.get(actor["name"], {})
            self.log("%s recast: %s" % (actor["name"], actor.get("one_line", "")))
            self.emit("cast_updated")

        elif self.what == "role":
            llm = make_llm(p)
            self.step("Working out what %s does" % self.name)
            self.progress(self.name, "Reading their lines in the script...")
            cues = script_parser.parse(p.get("script", ""),
                                       default_speaker=(p.get("options") or {}).get(
                                           "default_speaker", "NARRATOR"))
            roles = casting.describe_roles(llm, p.get("premise", ""), [actor], cues,
                                           script=p.get("script", ""),
                                           cancel=self.cancelled)
            if roles.get(actor["name"]):
                actor["one_line"] = roles[actor["name"]]
                self.log("%s: %s" % (actor["name"], actor["one_line"]))
            self.emit("cast_updated")

        elif self.what == "voice":
            llm = make_llm(p)
            self.step("Re-voicing %s" % self.name)
            self.progress(self.name, "Choosing a new voice...")
            actor.update(casting.revoice_actor(llm, p.get("premise", ""), actor, self.note,
                                               cancel=self.cancelled))
            actor["seed"] = stable_voice_seed(actor) ^ 0x5f5f          # a different roll
            actor["voice_sample"] = ""
            if actor.get("voice_value"):
                actor["voice_value"] = ""       # let the new description take over
            self.log("%s new voice: %s" % (actor["name"], actor.get("voice_type", "")))
            self.emit("cast_updated")

        self.free_for_drawing()
        self.progress(actor["name"], "Looking for ComfyUI...")
        if self.what == "role":
            self.progress(actor["name"], "")
            self.emit("actor_updated", name=actor["name"])
            self.result = {"name": actor["name"], "what": self.what}
            return

        client = self._client(required=self.what in ("look", "spin"))
        if client is None:
            # "look" and "spin" are nothing but drawing, so with no server there
            # is no half-result worth keeping quiet about - the earlier code
            # returned success here and the button appeared to do nothing.
            self.progress(actor["name"], "")
            self.emit("actor_updated", name=actor["name"])
            self.result = {"name": actor["name"], "visuals": False, "what": self.what}
            return
        if self.what in ("actor", "look", "spin"):
            portrait = self._runner(client, "portrait", required=False)
            spin = self._runner(client, "turnaround", required=False) or portrait
            if not (portrait or spin):
                # ComfyUI is up but there is no graph to run. Saying nothing here
                # is what made the button look dead.
                raise ComfyError(
                    "ComfyUI is running, but no picture workflow is loaded, so there "
                    "is nothing to draw with.\n\nGo to the Setup tab, pick the "
                    "Portrait slot and load a workflow exported with "
                    "Workflow > Export (API). Opening a saved project restores the "
                    "workflows it was using.")
            if self.what == "look":
                actor["look_seed"] = (int(actor.get("look_seed", 0)) + 1) % (2 ** 31 - 1)
            if portrait and self.what in ("actor", "look"):
                self.render_portrait(portrait, actor)
            if spin:
                self.render_spin(spin, actor, int(opts.get("turnaround_frames", 8)))
        if self.what in ("actor", "voice"):
            voice = self._runner(client, "voice", required=False)
            if voice:
                self.render_voice_sample(voice, actor)

        actor["approved"] = False
        self.progress(actor["name"], "")
        self.emit("actor_updated", name=actor["name"])
        self.result = {"name": actor["name"], "what": self.what}


def shot_values(runner, actor_map, cue, shot, index):
    """Everything one storyboard/movie shot is rendered from.

    The storyboard and the movie used to build this separately, and they had
    drifted: only the movie passed a reference image, so the pictures being
    judged were not the pictures being filmed. One function, both callers.
    """
    prompt = casting.shot_prompt(actor_map, cue, shot)
    subject = casting.shot_subject(shot, cue, actor_map.keys())
    actor = actor_map.get(subject) or {}
    seed = int(actor.get("look_seed", 0)) + index
    values = {"prompt": prompt, "negative": casting.NEGATIVE, "seed": seed}
    ref = visuals.identity_image(actor) if runner.has("image") else ""
    if ref:
        values["image"] = runner.upload(ref)
    return values, prompt, seed, subject, ref


SHOT_BATCH = 20


def plan_shots_batched(llm, project, cues, cancel=None, log=None, batch=SHOT_BATCH):
    """Fill project["shots"], a chunk at a time.

    A small local model will not return two hundred coherent JSON entries in
    one reply, but it handles twenty comfortably.
    """
    log = log or (lambda m: None)
    shots = dict(project.get("shots") or {})
    scene_list = script_parser.scenes(project.get("script", ""))
    heads = {}
    last = ""
    for i, c in enumerate(cues):
        last = casting._scene_at(scene_list, c.line_no) or last
        heads[i] = last
    todo = [i for i, c in enumerate(cues)
            if (shots.get(str(i)) or {}).get("line") != c.text
            or (shots.get(str(i)) or {}).get("scene") != heads[i]]
    if not todo:
        return shots, 0

    cast_list = proj.cast(project)
    premise = project.get("premise", "")
    for start in range(0, len(todo), batch):
        if cancel is not None and cancel():
            raise ComfyError("Cancelled")
        chunk = todo[start:start + batch]
        try:
            planned = casting.plan_shots(llm, premise, cast_list,
                                         [cues[i] for i in chunk],
                                         script=project.get("script", ""),
                                         cancel=cancel)
        except LLMError as e:
            log("batch %d: %s - using plain coverage"
                % (start // batch + 1, str(e).splitlines()[0][:60]))
            planned = {}
        for local, real in enumerate(chunk):
            shot = planned.get(local) or {}
            if not shot.get("shot"):
                shot = {"shot": "medium shot of %s speaking" % cues[real].speaker.title(),
                        "setting": "", "mood": ""}
            shot["line"] = cues[real].text
            shot["scene"] = heads[real]          # from the script, not the model
            was = shots.get(str(real)) or {}
            for kept in ("subject_override", "shot_override"):
                # The user's own choices for this shot. Replanning the AI's
                # description must not quietly throw them away.
                if was.get(kept):
                    shot[kept] = was[kept]
            shots[str(real)] = shot
        project["shots"] = shots
        log("planned %d of %d shots" % (min(start + batch, len(todo)), len(todo)))
    return shots, len(todo)


class StoryboardJob(Worker, _VisualMixin, _GpuMixin):
    """Plan a shot for every line, then draw it. The step before the movie."""

    kind = "storyboard"

    def __init__(self, project, cues, out_dir, on_event, only=None):
        Worker.__init__(self, on_event)
        self.project = project
        self.cues = cues
        self.out_dir = out_dir
        self.only = only            # re-draw just these cue indexes
        self.images = {}

    def execute(self):
        p = self.project
        opts = p.get("options") or {}

        if self.only is None:
            self.free_for_writing()
            try:
                self.step("Planning the shots")
                _, planned = plan_shots_batched(make_llm(p), p, self.cues,
                                                cancel=self.cancelled, log=self.log)
                if planned:
                    self.log("Planned %d shots." % planned)
            except LLMError as e:
                self.log("No shot plan (%s). Drawing plain coverage instead."
                         % str(e).splitlines()[0])

        self.free_for_drawing()
        client = self._client()
        runner = self._runner(client, "shot", required=True)
        if not runner.has("image"):
            self.log("This shot workflow has no reference-image input, so every shot "
                     "is drawn from words alone and characters will drift. Load a "
                     "workflow with an IPAdapter or reference input to lock them.")
        shots = p.get("shots") or {}
        actor_map = {a["name"]: a for a in proj.cast(p)}
        shot_dir = os.path.join(self.out_dir, "shots")
        os.makedirs(shot_dir, exist_ok=True)
        man_path = os.path.join(shot_dir, "manifest.json")
        manifest = _read_json(man_path)
        reuse = bool(opts.get("reuse_unchanged", True)) and self.only is None

        targets = list(range(len(self.cues))) if self.only is None else list(self.only)
        for n, i in enumerate(targets):
            if self.cancelled():
                raise ComfyError("Cancelled")
            cue = self.cues[i]
            shot = shots.get(str(i)) or {}
            values, prompt, seed, subject, ref = shot_values(
                runner, actor_map, cue, shot, i)
            key = hashlib.sha1(("%s|%d|%s|%s" % (prompt, seed, cue.text, ref))
                               .encode("utf-8")).hexdigest()[:16]
            prev = manifest.get(str(i))
            self.step("Drawing shot %d of %d" % (n + 1, len(targets)), n, len(targets))
            if reuse and prev and prev.get("key") == key and os.path.exists(prev.get("file", "")):
                self.images[i] = prev["file"]
                self.emit("shot_done", index=i, total=len(self.cues), file=prev["file"],
                          cached=True, prompt=prompt)
                continue
            if self.only is not None:
                values["seed"] = seed + random.randint(1, 10 ** 6)   # a redraw differs
            path = runner.run(values, os.path.join(shot_dir, "%04d" % (i + 1)), IMAGE,
                              cancel=self.cancelled)
            self.images[i] = path
            manifest[str(i)] = {"key": key, "file": path, "prompt": prompt}
            _write_json(man_path, manifest)
            self.emit("shot_done", index=i, total=len(self.cues), file=path, cached=False,
                      prompt=prompt)

        self.result = {"shots": len(self.images), "drawn": len(targets)}


class MovieJob(Worker, _VisualMixin, _GpuMixin):
    """Approved cast + script -> shots + dialogue -> a finished movie."""

    kind = "movie"

    def __init__(self, project, cues, out_dir, on_event, with_visuals=True):
        Worker.__init__(self, on_event)
        self.project = project
        self.cues = cues
        self.out_dir = out_dir
        self.with_visuals = with_visuals
        self.movie_path = ""
        self.edl_path = ""

    def execute(self):
        p = self.project
        opts = p.get("options") or {}
        os.makedirs(self.out_dir, exist_ok=True)
        actor_map = {a["name"]: a for a in proj.cast(p)}

        # ---- 1. what does each line look like
        shots = dict(p.get("shots") or {})
        if self.with_visuals:
            # a line that has been edited needs a fresh shot, not the old one
            need_plan = any((shots.get(str(i)) or {}).get("line") != c.text
                            for i, c in enumerate(self.cues))
            if need_plan:
                self.free_for_writing()
                try:
                    self.step("Planning shots")
                    shots, planned = plan_shots_batched(
                        make_llm(p), p, self.cues, cancel=self.cancelled, log=self.log)
                    self.log("Planned %d shots." % planned)
                except LLMError as e:
                    self.log("No shot plan (%s). Falling back to plain coverage."
                             % str(e).splitlines()[0])

        self.free_for_drawing()

        # ---- 2. the dialogue
        self.step("Recording dialogue")
        audio_dir = os.path.join(self.out_dir, "audio")
        voice_job = RenderJob(p, self.cues, audio_dir, self.on_event)
        voice_job._cancel = self._cancel               # one cancel button for both
        voice_job.execute()
        clips = list(voice_job.files)
        if not clips:
            raise ComfyError("No dialogue was rendered, so there is no movie to cut.")

        # ---- 3. the pictures
        images = {}
        if self.with_visuals:
            client = self._client(required=False)
            runner = self._runner(client, "shot", required=False) if client else None
            if runner:
                shot_dir = os.path.join(self.out_dir, "shots")
                os.makedirs(shot_dir, exist_ok=True)
                man_path = os.path.join(shot_dir, "manifest.json")
                manifest = _read_json(man_path)
                reuse = bool(opts.get("reuse_unchanged", True))
                for i, cue in enumerate(self.cues):
                    if self.cancelled():
                        raise ComfyError("Cancelled")
                    shot = shots.get(str(i)) or {}
                    values, prompt, seed, subject, ref = shot_values(
                        runner, actor_map, cue, shot, i)
                    key = hashlib.sha1(("%s|%d|%s|%s" % (prompt, seed, cue.text, ref))
                                       .encode("utf-8")).hexdigest()[:16]
                    prev = manifest.get(str(i))
                    self.step("Shot %d of %d" % (i + 1, len(self.cues)), i, len(self.cues))
                    if reuse and prev and prev.get("key") == key and os.path.exists(
                            prev.get("file", "")):
                        images[i] = prev["file"]
                        self.emit("shot_done", index=i, total=len(self.cues),
                                  file=prev["file"], cached=True)
                        continue
                    path = runner.run(values, os.path.join(shot_dir, "%04d" % (i + 1)),
                                      IMAGE, cancel=self.cancelled)
                    images[i] = path
                    manifest[str(i)] = {"key": key, "file": path, "prompt": prompt}
                    _write_json(man_path, manifest)
                    self.emit("shot_done", index=i, total=len(self.cues), file=path,
                              cached=False)

        # ---- 4. the cut
        entries = []
        for i, cue in enumerate(self.cues):
            clip = clips[i] if i < len(clips) else ""
            entries.append({
                "index": i, "speaker": cue.speaker, "text": cue.text,
                "audio": clip, "image": images.get(i, ""),
                "shot": (shots.get(str(i)) or {}).get("shot", ""),
                "seconds": duration(clip) + float(opts.get("gap_seconds", 0.35)),
            })
        self.edl_path = movie.write_edl(os.path.join(self.out_dir, "movie.edl.json"),
                                        entries, voice_job.stitched or "")

        ffmpeg = movie.find_ffmpeg()
        if not ffmpeg and movie.have_pyav():
            self.step("Cutting the movie")
            self.log("No ffmpeg binary; cutting with PyAV's own libraries instead.")
            self.movie_path = movie.assemble_pyav(
                entries, os.path.join(self.out_dir, "movie.mp4"),
                gap_seconds=proj.num_option(opts, "gap_seconds", 0.35),
                log=self.log, cancel=self.cancelled)
            self.log("Movie: %s" % self.movie_path)
        elif ffmpeg:
            self.step("Cutting the movie")
            self.log("Using ffmpeg: %s" % ffmpeg)
            self.movie_path = movie.assemble(
                ffmpeg, entries, os.path.join(self.out_dir, "movie.mp4"),
                gap_seconds=float(opts.get("gap_seconds", 0.35)),
                log=self.log, cancel=self.cancelled)
            self.log("Movie: %s" % self.movie_path)
        else:
            self.log("ffmpeg wasn't found, so the movie file wasn't muxed. "
                     "Everything else is in the output folder: the full audio take, "
                     "every shot, and movie.edl.json listing what plays when.")

        self.result = {"movie": self.movie_path, "edl": self.edl_path,
                       "audio": voice_job.stitched, "shots": len(images),
                       "lines": len(self.cues)}


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ==========================================================================
# widgets: Reusable Tkinter pieces: image loading, a scrolling frame, the turnaround
# ==========================================================================

"""Reusable Tkinter pieces: image loading, a scrolling frame, the turnaround
viewer, and the actor card with its regenerate buttons."""

import os
import tkinter as tk
from tkinter import ttk

try:
    from PIL import Image, ImageTk
    HAVE_PIL = True
except Exception:                       # pragma: no cover - Pillow is optional
    HAVE_PIL = False

PLACEHOLDER = "#2c2f36"


def load_photo(path, max_w=220, max_h=280):
    """A Tk image scaled to fit, or None. Uses Pillow when it's installed."""
    if not path or not os.path.exists(path):
        return None
    try:
        if HAVE_PIL:
            im = Image.open(path)
            if getattr(im, "is_animated", False):
                im.seek(0)
            im = im.convert("RGB")
            im.thumbnail((max_w, max_h))
            return ImageTk.PhotoImage(im)
        photo = tk.PhotoImage(file=path)          # Tk 8.6 reads PNG and GIF
        factor = max(1, int(max(photo.width() / float(max_w),
                                photo.height() / float(max_h)) + 0.999))
        return photo.subsample(factor, factor) if factor > 1 else photo
    except Exception:
        return None


class ScrollFrame(ttk.Frame):
    """A vertically scrolling container. Put your widgets in `.body`."""

    def __init__(self, master, **kw):
        ttk.Frame.__init__(self, master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.sb.pack(side="right", fill="y")
        self.body = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", lambda e: self._wheel(True))
        self.canvas.bind("<Leave>", lambda e: self._wheel(False))

    def _on_body(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)

    def _wheel(self, on):
        if on:
            self.canvas.bind_all("<MouseWheel>", self._scroll)
        else:
            self.canvas.unbind_all("<MouseWheel>")

    def _scroll(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120), "units")

    def clear(self):
        for child in self.body.winfo_children():
            child.destroy()


class SpinViewer(ttk.Frame):
    """The 360 turnaround: drag to spin, or let it turn on its own."""

    def __init__(self, master, size=(320, 380), **kw):
        ttk.Frame.__init__(self, master, **kw)
        self.size = size
        self.frames = []          # file paths
        self.photos = []          # Tk images, kept alive
        self.index = 0
        self.playing = False
        self._after = None
        self._drag_x = None
        self.title = tk.StringVar(value="No turnaround yet")

        ttk.Label(self, textvariable=self.title, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.canvas = tk.Canvas(self, width=size[0], height=size[1],
                                background=PLACEHOLDER, highlightthickness=0)
        self.canvas.pack(pady=4)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag_x", None))

        row = ttk.Frame(self)
        row.pack(fill="x")
        self.btn_play = ttk.Button(row, text="Spin", width=7, command=self.toggle)
        self.btn_play.pack(side="left")
        ttk.Button(row, text="<", width=3, command=lambda: self.nudge(-1)).pack(side="left", padx=(6, 0))
        ttk.Button(row, text=">", width=3, command=lambda: self.nudge(1)).pack(side="left")
        self.pos = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.pos, foreground="#666").pack(side="left", padx=8)
        self._empty()

    # -------------------------------------------------------------- content

    def set_frames(self, paths, title=""):
        self.stop()
        self.frames = [p for p in (paths or []) if p and os.path.exists(p)]
        self.photos = [load_photo(p, self.size[0], self.size[1]) for p in self.frames]
        self.photos = [p for p in self.photos if p is not None]
        self.frames = self.frames[:len(self.photos)]
        self.index = 0
        self.title.set(title or ("%d frames" % len(self.frames)))
        if self.frames:
            self._draw()
            if len(self.frames) > 2:
                self.play()
        else:
            self._empty()

    def _empty(self):
        self.canvas.delete("all")
        self.canvas.create_text(self.size[0] // 2, self.size[1] // 2, fill="#8a8f99",
                                width=self.size[0] - 40, justify="center",
                                text="No turnaround yet.\n\nCreate the cast, or press\n"
                                     "New look on an actor.")
        self.pos.set("")

    def _draw(self):
        if not self.photos:
            return self._empty()
        self.index %= len(self.photos)
        photo = self.photos[self.index]
        self.canvas.delete("all")
        self.canvas.create_image(self.size[0] // 2, self.size[1] // 2, image=photo)
        self.pos.set("%d / %d" % (self.index + 1, len(self.photos)))

    # --------------------------------------------------------------- motion

    def nudge(self, step):
        if self.photos:
            self.index = (self.index + step) % len(self.photos)
            self._draw()

    def toggle(self):
        self.stop() if self.playing else self.play()

    def play(self):
        if len(self.photos) < 2:
            return
        self.playing = True
        self.btn_play.configure(text="Stop")
        self._tick()

    def stop(self):
        self.playing = False
        self.btn_play.configure(text="Spin")
        if self._after:
            try:
                self.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _tick(self):
        if not self.playing:
            return
        self.nudge(1)
        self._after = self.after(140, self._tick)

    def _press(self, e):
        self.stop()
        self._drag_x = e.x

    def _drag(self, e):
        if self._drag_x is None or not self.photos:
            return
        step = int((e.x - self._drag_x) / 18)
        if step:
            self.index = (self.index - step) % len(self.photos)
            self._drag_x = e.x
            self._draw()


ROLE_COLOR = "#1a4f9c"


class ActorCard(ttk.Frame):
    """One actor: portrait, who they are, what they do in the script, and the
    regenerate buttons for the actor and for the voice."""

    def __init__(self, master, actor, on_action, on_select, **kw):
        ttk.Frame.__init__(self, master, padding=8, relief="groove", borderwidth=1, **kw)
        self.actor = actor
        self.on_action = on_action
        self.name = actor["name"]
        self._photo = None

        self.thumb = tk.Canvas(self, width=120, height=150, background=PLACEHOLDER,
                               highlightthickness=0, cursor="hand2")
        self.thumb.grid(row=0, column=0, rowspan=3, sticky="nw")
        self.thumb.bind("<Button-1>", lambda e: on_select(self.name))

        head = ttk.Frame(self)
        head.grid(row=0, column=1, sticky="ew", padx=10)
        self.columnconfigure(1, weight=1)
        self.v_title = tk.StringVar()
        ttk.Label(head, textvariable=self.v_title, font=("Segoe UI", 11, "bold")).pack(side="left")
        self.v_where = tk.StringVar()
        self.lbl_where = ttk.Label(head, textvariable=self.v_where,
                                   font=("Segoe UI", 9), foreground=ROLE_COLOR)
        self.lbl_where.pack(side="left", padx=10)

        self.v_body = tk.StringVar()
        self.lbl_body = ttk.Label(self, textvariable=self.v_body, justify="left",
                                  wraplength=560, foreground="#333")
        self.lbl_body.grid(row=1, column=1, sticky="ew", padx=10, pady=(2, 6))
        # A fixed wrap width clipped the judgement text whenever the cast pane was
        # narrower than 560px, so wrap to whatever width the card actually has.
        self.bind("<Configure>", self._rewrap)

        btns = ttk.Frame(self)
        btns.grid(row=2, column=1, sticky="w", padx=10)
        self._build_progress()
        self.v_approved = tk.BooleanVar(value=bool(actor.get("approved")))
        ttk.Checkbutton(btns, text="Approved", variable=self.v_approved,
                        command=lambda: on_action(self.name, "approve", self.v_approved.get())
                        ).pack(side="left", padx=(0, 10))
        self.v_lead = tk.BooleanVar(value=bool(actor.get("lead")))
        ttk.Checkbutton(btns, text="Main character", variable=self.v_lead,
                        command=lambda: on_action(self.name, "lead", self.v_lead.get())
                        ).pack(side="left", padx=(0, 12))
        for label, action, hint in (
                ("Regenerate actor", "actor", "a different person in the same role"),
                ("Regenerate voice", "voice", "a different voice, same person"),
                ("New look", "look", "same person, new render"),
                ("Play voice", "play", "hear the sample line"),
                ("Find in script", "find", "jump to their lines"),
                ("Same as...", "same", "this is really another character"),
                ("Remove", "remove", "take them out of the cast")):
            ttk.Button(btns, text=label, command=lambda a=action: on_action(self.name, a, None)
                       ).pack(side="left", padx=2)
        self.refresh(actor)

    def _rewrap(self, event):
        # 120 thumbnail + 10 padx either side + the card's own border
        width = max(200, event.width - 160)
        if abs(width - int(self.lbl_body.cget("wraplength") or 0)) > 8:
            self.lbl_body.configure(wraplength=width)

    # ------------------------------------------------------------- progress

    def _build_progress(self):
        """A bar under the buttons, shown only while this actor is being made.

        Drawing a portrait and a turnaround is minutes of silent GPU work, so
        without this the only feedback is the fans.
        """
        self.prog_row = ttk.Frame(self)
        self.prog_row.grid(row=3, column=1, sticky="ew", padx=10, pady=(6, 0))
        self.prog_row.columnconfigure(1, weight=1)
        self.prog = ttk.Progressbar(self.prog_row, length=180, mode="determinate")
        self.prog.grid(row=0, column=0, sticky="w")
        self.v_prog = tk.StringVar(value="")
        ttk.Label(self.prog_row, textvariable=self.v_prog,
                  foreground="#1a4f9c").grid(row=0, column=1, sticky="w", padx=8)
        self._prog_mode = ""
        self.prog_row.grid_remove()

    def set_progress(self, label, done=0, total=0):
        """Show the bar. `total` of 0 means "no idea how long" - march instead."""
        if not label:
            return self.clear_progress()
        self.v_prog.set(label)
        want = "determinate" if total else "indeterminate"
        if want != self._prog_mode:
            if self._prog_mode == "indeterminate":
                self.prog.stop()
            self.prog.configure(mode=want)
            self._prog_mode = want
            if want == "indeterminate":
                self.prog.start(60)
        if total:
            self.prog.configure(maximum=total, value=min(done, total))
        self.prog_row.grid()

    def clear_progress(self):
        if self._prog_mode == "indeterminate":
            self.prog.stop()
        self._prog_mode = ""
        self.prog.configure(mode="determinate", value=0)
        self.v_prog.set("")
        self.prog_row.grid_remove()

    def refresh(self, actor):
        self.actor = actor
        role = "lead" if actor.get("lead") else actor.get("role", "")
        self.v_title.set("%s   (%s)" % (actor["name"], role) if role else actor["name"])
        said = int(actor.get("line_count", 0) or 0)
        self.v_where.set(("%d line%s in the script" % (said, "" if said == 1 else "s"))
                         if said else "")
        lines = []
        if actor.get("aliases"):
            lines.append("Also called: %s" % ", ".join(actor["aliases"]))
        if actor.get("one_line"):
            lines.append(actor["one_line"])
        if actor.get("look_note"):
            lines.append("Look you asked for: %s" % actor["look_note"])
        voice = ", ".join(x for x in (actor.get("voice_type", ""),
                                      actor.get("voice_direction", "")) if x)
        if voice:
            lines.append("Voice: %s" % voice)
        if actor.get("sample_line"):
            lines.append("“%s”" % actor["sample_line"])
        marks = []
        if actor.get("portrait"):
            marks.append("portrait")
        if actor.get("turnaround"):
            marks.append("%d-frame spin" % len(actor["turnaround"]))
        if actor.get("reference_image"):
            marks.append("your reference picture")
        if actor.get("voice_sample"):
            marks.append("voice sample")
        lines.append("Files: %s" % (", ".join(marks) if marks else "none yet"))
        self.v_body.set("\n".join(lines))
        self.v_approved.set(bool(actor.get("approved")))
        self.v_lead.set(bool(actor.get("lead")))

        self._photo = load_photo(actor.get("portrait", "")
                                 or (actor.get("turnaround") or [""])[0], 120, 150)
        self.thumb.delete("all")
        if self._photo:
            self.thumb.create_image(60, 75, image=self._photo)
        else:
            self.thumb.create_text(60, 75, text="no\nportrait", fill="#8a8f99",
                                   justify="center")


# ==========================================================================
# gui: ScriptVoice - an offline GUI that casts a film with a local LLM, renders the
# ==========================================================================

"""ScriptVoice - an offline GUI that casts a film with a local LLM, renders the
actors and their voices through ComfyUI, and cuts the finished movie."""

import json
import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

# (flattened) from . import casting, llm as llm_mod, movie as movie_mod, project as proj
# (flattened) from . import script_parser, speech, visuals
# (flattened) from . import comfy as comfy_mod
# (flattened) from .comfy import ComfyClient, ComfyError
# (flattened) from .pipeline import CastJob, MovieJob, RegenerateJob, StoryboardJob
# (flattened) from .render import RenderJob, system_voice
# (flattened) from .widgets import ActorCard, ScrollFrame, SpinViewer, load_photo
# (flattened) from .worker import Worker

APP = "ScriptVoice"
PROJ_EXT = ".svproj"

SAMPLE_PREMISE = (
    "A decommissioned lighthouse keeper and a marine biologist are trapped on the same "
    "island the night the light comes back on by itself.")

SAMPLE_SCRIPT = """\
# Lines starting with # are notes and are never spoken.
# Write either "NAME: dialogue" or screenplay style (NAME on its own line).

NARRATOR: The lighthouse had been dark for eleven years.

MAYA: You said the generator still worked.

RUBEN
It did. In 2014. (shrugs) Give it a minute.

MAYA: We don't have a minute.
"""


class App(ttk.Frame):
    def __init__(self, master):
        ttk.Frame.__init__(self, master, padding=0)
        self.pack(fill="both", expand=True)
        self.master.title(APP)
        self.master.geometry("1240x820")
        self.master.minsize(1040, 700)

        self.project = proj.new_project()
        self._adopted = proj.adopt_default_workflows(self.project)
        self.project["premise"] = SAMPLE_PREMISE
        self.project["script"] = SAMPLE_SCRIPT
        self.project_path = ""
        self.workflows = {}          # slot -> loaded workflow dict
        self.cues = []
        self.job = None
        self.events = queue.Queue()
        self.cards = {}
        self.selected_actor = ""
        self.dirty = False
        self._servers = []

        fit_rows_to_font(master)
        self._build_menu()
        self._build_body()
        self._load_project_into_ui()
        self.after(120, self._drain_events)
        self.master.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ chrome

    def _build_menu(self):
        m = tk.Menu(self.master)
        f = tk.Menu(m, tearoff=0)
        f.add_command(label="New project", accelerator="Ctrl+N", command=self.new_project)
        f.add_command(label="Open project...", accelerator="Ctrl+O", command=self.open_project)
        f.add_command(label="Save", accelerator="Ctrl+S", command=self.save_project)
        f.add_command(label="Save as...", command=self.save_project_as)
        f.add_separator()
        f.add_command(label="Import script from .txt...", command=self.import_script)
        f.add_command(label="Open output folder", command=self.open_output_folder)
        f.add_separator()
        f.add_command(label="Quit", command=self._on_close)
        m.add_cascade(label="File", menu=f)

        h = tk.Menu(m, tearoff=0)
        h.add_command(label="How this works", command=self.show_help)
        m.add_cascade(label="Help", menu=h)
        self.master.config(menu=m)

        self.master.bind("<Control-n>", lambda e: self.new_project())
        self.master.bind("<Control-o>", lambda e: self.open_project())
        self.master.bind("<Control-s>", lambda e: self.save_project())
        self.master.bind("<F5>", lambda e: self.make_movie())

    def _build_body(self):
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.tab_premise = ttk.Frame(self.nb, padding=10)
        self.tab_cast = ttk.Frame(self.nb, padding=10)
        self.tab_script = ttk.Frame(self.nb, padding=10)
        self.tab_board = ttk.Frame(self.nb, padding=10)
        self.tab_wf = ttk.Frame(self.nb, padding=10)
        self.tab_movie = ttk.Frame(self.nb, padding=10)
        for frame, label in ((self.tab_premise, "  1. Premise  "),
                             (self.tab_script, "  2. Script  "),
                             (self.tab_cast, "  3. Cast  "),
                             (self.tab_board, "  4. Storyboard  "),
                             (self.tab_movie, "  5. Movie  "),
                             (self.tab_wf, "  6. Setup  ")):
            self.nb.add(frame, text=label)
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._build_premise_tab()
        self._build_cast_tab()
        self._build_script_tab()
        self._build_board_tab()
        self._build_workflow_tab()
        self._build_movie_tab()

        bar = ttk.Frame(self, padding=(10, 6))
        bar.pack(fill="x")
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(bar, textvariable=self.status, anchor="w").pack(side="left", fill="x", expand=True)
        self.conn = tk.StringVar(value="ComfyUI: not checked   |   model: not checked")
        ttk.Label(bar, textvariable=self.conn, anchor="e").pack(side="right")

    # ------------------------------------------------------------ premise tab

    def _build_premise_tab(self):
        t = self.tab_premise
        ttk.Label(t, text="What is the film about?",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(t, foreground="#666", text=
                  "One or two sentences. The AI reads this, decides who the story needs, "
                  "and locks each character down so they stay the same person throughout."
                  ).pack(anchor="w", pady=(0, 8))

        wrap = ttk.Frame(t)
        wrap.pack(fill="both", expand=True)
        self.premise_text = tk.Text(wrap, wrap="word", height=8, undo=True,
                                    font=("Segoe UI", 11), padx=10, pady=8)
        sb = ttk.Scrollbar(wrap, command=self.premise_text.yview)
        self.premise_text.configure(yscrollcommand=sb.set)
        self.premise_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.premise_text.bind("<<Modified>>", self._on_premise_modified)

        box = ttk.LabelFrame(t, text="Local model (LM Studio, Ollama, llama.cpp...)", padding=10)
        box.pack(fill="x", pady=(12, 0))
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="Server:").grid(row=0, column=0, sticky="w")
        self.v_llm_url = tk.StringVar()
        self.cb_llm = ttk.Combobox(box, textvariable=self.v_llm_url, width=46)
        self.cb_llm.grid(row=0, column=1, sticky="ew", padx=6)
        self.cb_llm.bind("<<ComboboxSelected>>", lambda e: self.load_models())
        ttk.Button(box, text="Detect", command=self.detect_llm).grid(row=0, column=2)
        ttk.Label(box, text="Model:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.v_llm_model = tk.StringVar()
        self.cb_model = ttk.Combobox(box, textvariable=self.v_llm_model, width=46)
        self.cb_model.grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(box, text="Refresh", command=self.load_models).grid(row=1, column=2, pady=(6, 0))

        opts = ttk.Frame(box)
        opts.grid(row=2, column=1, sticky="w", padx=6, pady=(8, 0))
        self.v_max_actors = tk.StringVar()
        self.v_frames = tk.StringVar()
        self.v_scenes = tk.StringVar()
        ttk.Label(opts, text="Max characters:").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=8, width=4, textvariable=self.v_max_actors).pack(side="left", padx=(4, 14))
        ttk.Label(opts, text="Turnaround frames:").pack(side="left")
        ttk.Spinbox(opts, from_=2, to=24, width=4, textvariable=self.v_frames).pack(side="left", padx=(4, 14))
        ttk.Label(opts, text="Scenes to write:").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=20, width=4, textvariable=self.v_scenes).pack(side="left", padx=4)

        row = ttk.Frame(t)
        row.pack(fill="x", pady=(12, 0))
        self.btn_cast = ttk.Button(row, text="Create the cast  \u2192",
                                   command=self.create_cast)
        self.btn_cast.pack(side="left")
        ttk.Button(row, text="Cast without pictures", command=lambda: self.create_cast(
            steps=("cast", "roles", "voice"))).pack(side="left", padx=6)
        ttk.Button(row, text="Describe everyone's role", command=lambda: self.create_cast(
            steps=("roles",))).pack(side="left", padx=6)
        ttk.Label(row, foreground="#666",
                  text="   invents the characters, says what each one does in the script, "
                       "then renders their look, their spin and their voice").pack(side="left")

    def _on_premise_modified(self, _e=None):
        if self.premise_text.edit_modified():
            self.dirty = True
            self.premise_text.edit_modified(False)

    # --------------------------------------------------------------- cast tab

    def _build_cast_tab(self):
        t = self.tab_cast
        head = ttk.Frame(t)
        head.pack(fill="x")
        self.v_cast_summary = tk.StringVar(value="No cast yet.")
        ttk.Label(head, textvariable=self.v_cast_summary,
                  font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(head, text="Approve all", command=self.approve_all).pack(side="right")
        ttk.Button(head, text="Add a character...",
                   command=self.add_character).pack(side="right", padx=6)

        panes = ttk.Panedwindow(t, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=(8, 0))

        left = ttk.Frame(panes)
        self.cast_scroll = ScrollFrame(left)
        self.cast_scroll.pack(fill="both", expand=True)
        panes.add(left, weight=3)

        # This column holds the turnaround, the voice panel, the look box and the
        # workflow overrides - together taller than most windows, so it scrolls.
        right_outer = ttk.Frame(panes, padding=(10, 0, 0, 0))
        self.spin = SpinViewer(right_outer, size=(300, 330))
        self.spin.pack(anchor="n")
        # Everything about the selected actor, one click apart. Stacking these
        # pushed the lower ones off the bottom of the window as they grew.
        self.cast_side = ttk.Notebook(right_outer)
        self.cast_side.pack(fill="both", expand=True, pady=(8, 0))
        right = right_outer

        lbox = ttk.Frame(self.cast_side, padding=8)
        self.cast_side.add(lbox, text="  Look  ")
        self.look_text = tk.Text(lbox, height=3, wrap="word", font=("Segoe UI", 9))
        self.look_text.pack(fill="x")
        ttk.Label(lbox, foreground="#666",
                  text="e.g. bald, heavy jaw, broken nose, dockworker build. These words "
                       "lead the image prompt and the AI never rewrites them."
                  ).pack(anchor="w", pady=(4, 0))
        lrow = ttk.Frame(lbox)
        lrow.pack(fill="x", pady=(6, 0))
        ttk.Button(lrow, text="Save the look", command=self.save_look).pack(side="left")
        ttk.Button(lrow, text="Save and redraw",
                   command=self.save_look_and_redraw).pack(side="left", padx=6)

        rbox = ttk.Frame(self.cast_side, padding=8)
        self.cast_side.add(rbox, text="  Reference face  ")
        rrow = ttk.Frame(rbox)
        rrow.pack(fill="x")
        self.v_reference = tk.StringVar()
        ttk.Entry(rrow, textvariable=self.v_reference).pack(side="left", fill="x", expand=True)
        ttk.Button(rrow, text="...", width=3,
                   command=self.pick_reference).pack(side="left", padx=4)
        ttk.Button(rrow, text="Clear", width=6,
                   command=self.clear_reference).pack(side="left")
        self.v_reference_note = tk.StringVar(value="")
        ttk.Label(rbox, textvariable=self.v_reference_note, foreground="#666",
                  wraplength=360, justify="left").pack(anchor="w", pady=(4, 0))

        wbox = ttk.Frame(self.cast_side, padding=8)
        self.cast_side.add(wbox, text="  Who they are  ")
        self.who_text = tk.Text(wbox, height=3, wrap="word", font=("Segoe UI", 9))
        self.who_text.pack(fill="x")
        ttk.Label(wbox, foreground="#666",
                  text="One or two sentences. The judge, the shot planner and every "
                       "recast read this, so correcting it here corrects the film."
                  ).pack(anchor="w", pady=(4, 0))
        wrow = ttk.Frame(wbox)
        wrow.pack(fill="x", pady=(6, 0))
        ttk.Button(wrow, text="Save", command=self.save_who).pack(side="left")
        ttk.Button(wrow, text="Save and describe their role",
                   command=self.save_who_and_judge).pack(side="left", padx=6)
        ttk.Button(wrow, text="Split off a name...",
                   command=self.split_character).pack(side="left", padx=6)

        vbox = ttk.Frame(self.cast_side, padding=8)
        self.cast_side.add(vbox, text="  Voice  ")
        vbox.columnconfigure(1, weight=1)
        self.v_sel_name = tk.StringVar(value="(none selected)")
        ttk.Label(vbox, textvariable=self.v_sel_name,
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(vbox, text="Reference clip:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.v_voice_file = tk.StringVar()
        ttk.Entry(vbox, textvariable=self.v_voice_file, width=26).grid(
            row=1, column=1, sticky="ew", padx=4, pady=(6, 0))
        ttk.Button(vbox, text="...", width=3, command=self.pick_voice_file).grid(
            row=1, column=2, pady=(6, 0))
        ttk.Label(vbox, text="Preset / speaker:").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.v_voice_value = tk.StringVar()
        ttk.Entry(vbox, textvariable=self.v_voice_value).grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=4, pady=(4, 0))
        ttk.Label(vbox, text="Voice seed:").grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.v_seed = tk.StringVar()
        ttk.Entry(vbox, textvariable=self.v_seed, width=12).grid(
            row=3, column=1, sticky="w", padx=4, pady=(4, 0))
        ttk.Label(vbox, text="Windows voice:").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.v_sys_voice = tk.StringVar()
        self.cb_sys_voice = ttk.Combobox(vbox, textvariable=self.v_sys_voice,
                                         state="readonly", width=24)
        self.cb_sys_voice.grid(row=4, column=1, columnspan=2, sticky="ew", padx=4, pady=(8, 0))
        sysrow = ttk.Frame(vbox)
        sysrow.grid(row=5, column=1, columnspan=2, sticky="w", padx=4, pady=(4, 0))
        self.v_sys_rate = tk.StringVar()
        self.v_sys_pitch = tk.StringVar()
        ttk.Label(sysrow, text="rate").pack(side="left")
        ttk.Spinbox(sysrow, from_=-10, to=10, width=4,
                    textvariable=self.v_sys_rate).pack(side="left", padx=(2, 8))
        ttk.Label(sysrow, text="pitch %").pack(side="left")
        ttk.Spinbox(sysrow, from_=-50, to=50, increment=5, width=4,
                    textvariable=self.v_sys_pitch).pack(side="left", padx=2)
        self.v_sys_note = tk.StringVar(value="")
        ttk.Label(vbox, textvariable=self.v_sys_note, foreground="#666").grid(
            row=6, column=1, columnspan=2, sticky="w", padx=4)

        brow = ttk.Frame(vbox)
        brow.grid(row=7, column=1, columnspan=2, sticky="w", padx=4, pady=(8, 0))
        ttk.Button(brow, text="Save", command=self.save_voice_details).pack(side="left")
        ttk.Button(brow, text="Hear it", command=self.preview_system_voice).pack(side="left", padx=6)

        pbox = ttk.Frame(self.cast_side, padding=8)
        self.cast_side.add(pbox, text="  Advanced  ")
        self.param_tree = ttk.Treeview(pbox, columns=("value",), show="tree headings", height=4)
        self.param_tree.heading("#0", text="Input")
        self.param_tree.heading("value", text="Value")
        self.param_tree.column("#0", width=220)
        self.param_tree.column("value", width=80)
        self.param_tree.pack(fill="both", expand=True)
        prow = ttk.Frame(pbox)
        prow.pack(fill="x", pady=(4, 0))
        self.v_param_target = tk.StringVar()
        self.param_combo = ttk.Combobox(prow, textvariable=self.v_param_target,
                                        state="readonly", width=24)
        self.param_combo.pack(side="left")
        self.v_param_value = tk.StringVar()
        ttk.Entry(prow, textvariable=self.v_param_value, width=8).pack(side="left", padx=4)
        ttk.Button(prow, text="Set", width=5, command=self.set_param).pack(side="left")
        ttk.Button(prow, text="Del", width=5, command=self.remove_param).pack(side="left", padx=2)
        panes.add(right_outer, weight=2)

        foot = ttk.Frame(t)
        foot.pack(fill="x", pady=(10, 0))
        self.v_gate = tk.StringVar(value="")
        ttk.Label(foot, textvariable=self.v_gate, foreground="#666").pack(side="left")
        self.btn_movie_gate = ttk.Button(foot, text="Cast approved \u2192 draw the storyboard",
                                         command=self.make_storyboard, state="disabled")
        self.btn_movie_gate.pack(side="right")

    # ------------------------------------------------------------- script tab

    def _build_script_tab(self):
        t = self.tab_script
        head = ttk.Frame(t)
        head.pack(fill="x")
        ttk.Label(head, text='Script - "NAME: line" or screenplay format.',
                  font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(head, text="Write it with the AI", command=self.write_script).pack(side="right")

        bar = ttk.Frame(t)
        bar.pack(fill="x", pady=(8, 4))
        self.btn_compile = ttk.Button(bar, text="Compile script  \u2192  create characters",
                                      command=self.compile_script)
        self.btn_compile.pack(side="left")
        ttk.Button(bar, text="Load a script file...",
                   command=self.import_script).pack(side="left", padx=6)
        self.script_info = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.script_info,
                  foreground="#444").pack(side="left", padx=12)
        ttk.Label(t, foreground="#666",
                  text="Lines starting with # or [ are notes and are skipped. Press Compile "
                       "to read the script and add everyone who speaks to the cast."
                  ).pack(anchor="w", pady=(0, 8))

        wrap = ttk.Frame(t)
        wrap.pack(fill="both", expand=True)
        self.script_text = tk.Text(wrap, wrap="word", undo=True, font=("Consolas", 11),
                                   padx=10, pady=8)
        sb = ttk.Scrollbar(wrap, command=self.script_text.yview)
        self.script_text.configure(yscrollcommand=sb.set)
        self.script_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.script_text.bind("<<Modified>>", self._on_script_modified)

        opts = ttk.LabelFrame(t, text="Parsing", padding=8)
        opts.pack(fill="x", pady=(10, 0))
        self.v_default_speaker = tk.StringVar()
        self.v_strip = tk.BooleanVar()
        self.v_split = tk.BooleanVar()
        self.v_maxchars = tk.StringVar()
        ttk.Label(opts, text="Un-named prose is spoken by:").grid(row=0, column=0, sticky="w")
        ttk.Entry(opts, textvariable=self.v_default_speaker, width=18).grid(
            row=0, column=1, sticky="w", padx=(6, 18))
        ttk.Checkbutton(opts, text="Skip (parentheticals)", variable=self.v_strip).grid(
            row=0, column=2, sticky="w", padx=(0, 18))
        ttk.Checkbutton(opts, text="Split long lines into sentences",
                        variable=self.v_split).grid(row=0, column=3, sticky="w", padx=(0, 18))
        ttk.Label(opts, text="Max chars per chunk (0 = off):").grid(row=0, column=4, sticky="w")
        ttk.Entry(opts, textvariable=self.v_maxchars, width=6).grid(row=0, column=5, padx=6)



    def _on_script_modified(self, _e=None):
        if self.script_text.edit_modified():
            self.dirty = True
            self.script_text.edit_modified(False)

    # -------------------------------------------------------- storyboard tab

    def _build_board_tab(self):
        t = self.tab_board
        head = ttk.Frame(t)
        head.pack(fill="x")
        self.btn_board = ttk.Button(head, text="Draw the storyboard",
                                    command=self.make_storyboard)
        self.btn_board.pack(side="left")
        ttk.Button(head, text="Redraw this shot",
                   command=self.redraw_shot).pack(side="left", padx=6)
        self.v_board_info = tk.StringVar(value="No storyboard yet.")
        ttk.Label(head, textvariable=self.v_board_info,
                  foreground="#444").pack(side="left", padx=12)
        self.btn_board_movie = ttk.Button(head, text="Make the movie  \u2192",
                                          command=self.make_movie, state="disabled")
        self.btn_board_movie.pack(side="right")

        ttk.Label(t, foreground="#666",
                  text="One picture per line. Click a row to see it, and use Who is in "
                       "this shot if the camera should be on someone else."
                  ).pack(anchor="w", pady=(4, 8))

        self.board_progress = ttk.Progressbar(t, mode="determinate")
        self.board_progress.pack(fill="x", pady=(0, 8))

        panes = ttk.Panedwindow(t, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes)
        self.board_tree = ttk.Treeview(
            left, columns=("speaker", "who", "line", "shot", "state"),
            show="headings", height=18)
        for col, w, a in (("speaker", 100, "w"), ("who", 100, "w"), ("line", 260, "w"),
                          ("shot", 240, "w"), ("state", 70, "center")):
            self.board_tree.heading(col, text=col.title())
            self.board_tree.column(col, width=w, anchor=a)
        bsb = ttk.Scrollbar(left, command=self.board_tree.yview)
        self.board_tree.configure(yscrollcommand=bsb.set)
        self.board_tree.pack(side="left", fill="both", expand=True)
        bsb.pack(side="right", fill="y")
        self.board_tree.bind("<<TreeviewSelect>>", self._on_board_select)
        panes.add(left, weight=3)

        right = ttk.Frame(panes, padding=(10, 0, 0, 0))
        self.board_canvas = tk.Canvas(right, width=380, height=430, background="#2c2f36",
                                      highlightthickness=0)
        self.board_canvas.pack()
        subj = ttk.LabelFrame(right, text="Who is in this shot", padding=8)
        subj.pack(fill="x", pady=(8, 0))
        self.v_board_subject = tk.StringVar()
        self.cb_board_subject = ttk.Combobox(subj, textvariable=self.v_board_subject,
                                             state="readonly", width=24)
        self.cb_board_subject.pack(side="left")
        ttk.Button(subj, text="Use this face",
                   command=self.set_shot_subject).pack(side="left", padx=6)
        ttk.Button(subj, text="Use it and redraw",
                   command=self.set_shot_subject_and_redraw).pack(side="left")
        ttk.Label(right, foreground="#666", wraplength=380, justify="left",
                  text="The camera is often on the listener, not the speaker. This is "
                       "whose face gets drawn."
                  ).pack(anchor="w", pady=(4, 0))

        ovr = ttk.LabelFrame(right, text="Describe this shot yourself", padding=8)
        ovr.pack(fill="x", pady=(8, 0))
        self.shot_text_box = tk.Text(ovr, height=3, wrap="word", font=("Segoe UI", 9))
        self.shot_text_box.pack(fill="x")
        ttk.Label(ovr, foreground="#666", wraplength=360, justify="left",
                  text="Anything typed here replaces the AI's description for this "
                       "shot and is kept when the storyboard is planned again. "
                       "Empty it to hand the shot back to the AI."
                  ).pack(anchor="w", pady=(4, 0))
        orow = ttk.Frame(ovr)
        orow.pack(fill="x", pady=(6, 0))
        ttk.Button(orow, text="Save this shot",
                   command=self.save_shot_text).pack(side="left")
        ttk.Button(orow, text="Save and redraw",
                   command=self.save_shot_text_and_redraw).pack(side="left", padx=6)

        self.v_board_caption = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.v_board_caption, wraplength=380,
                  justify="left", foreground="#333").pack(anchor="w", pady=(6, 0))
        panes.add(right, weight=2)
        self._board_photo = None

    def _refresh_board(self):
        """Fill the shot list from the script and whatever has been drawn."""
        if not hasattr(self, "board_tree"):
            return
        self.board_tree.delete(*self.board_tree.get_children())
        shots = self.project.get("shots") or {}
        drawn = self._drawn_shots()
        for c in self.cues:
            shot = casting.shot_text(shots.get(str(c.index)) or {})
            rec = (shots.get(str(c.index)) or {})
            who = casting.shot_subject(rec, c, (self.project.get("characters") or {}).keys())
            self.board_tree.insert("", "end", iid=str(c.index),
                                   values=(c.speaker, who, _short(c.text, 90),
                                           _short(shot, 80),
                                           "drawn" if c.index in drawn else ""))
        self.v_board_info.set("%d lines | %d drawn" % (len(self.cues), len(drawn)))
        self.btn_board_movie.configure(state="normal" if drawn else "disabled")

    def _drawn_shots(self):
        """{cue index: image path} from the shots manifest on disk."""
        out = self.v_outdir.get() or self._default_outdir()
        found = {}
        try:
            with open(os.path.join(out, "shots", "manifest.json"), "r",
                      encoding="utf-8") as f:
                for k, v in (json.load(f) or {}).items():
                    if v.get("file") and os.path.exists(v["file"]):
                        found[int(k)] = v["file"]
        except Exception:
            pass
        return found

    def _on_board_select(self, _e=None):
        sel = self.board_tree.selection()
        if not sel:
            return
        index = int(sel[0])
        self._board_photo = load_photo(self._drawn_shots().get(index, ""), 380, 430)
        self.board_canvas.delete("all")
        if self._board_photo:
            self.board_canvas.create_image(190, 215, image=self._board_photo)
        else:
            self.board_canvas.create_text(190, 215, fill="#8a8f99", text="not drawn yet")
        if index < len(self.cues):
            cue = self.cues[index]
            shot = (self.project.get("shots") or {}).get(str(index)) or {}
            names = [a["name"] for a in proj.cast(self.project)]
            self.cb_board_subject["values"] = names
            self.v_board_subject.set(casting.shot_subject(shot, cue, names))
            self.shot_text_box.delete("1.0", "end")
            self.shot_text_box.insert("1.0", shot.get("shot_override", ""))
            self.v_board_caption.set(
                "%d. %s speaks\n\n%s\n\n%s"
                % (index + 1, cue.speaker, cue.text,
                   casting.shot_text(shot) or "(no shot description yet)"))

    def save_shot_text(self):
        """Store the user's own description for the selected shot."""
        sel = self.board_tree.selection()
        if not sel:
            messagebox.showinfo(APP, "Pick a shot in the list first.")
            return False
        index = int(sel[0])
        shots = self.project.setdefault("shots", {})
        shot = shots.setdefault(str(index), {})
        typed = self.shot_text_box.get("1.0", "end-1c").strip()
        if typed:
            shot["shot_override"] = typed
        else:
            shot.pop("shot_override", None)
        if index < len(self.cues):
            shot.setdefault("line", self.cues[index].text)
        self.dirty = True
        self._refresh_board()
        self.board_tree.selection_set(sel[0])
        self.board_tree.see(sel[0])
        self.set_status("Shot %d: %s" % (index + 1, "your description saved." if typed
                                         else "back to the AI's description."))
        return True

    def save_shot_text_and_redraw(self):
        if self.save_shot_text() and not self.busy():
            self.redraw_shot()

    def set_shot_subject(self, redraw=False):
        """Pin whose face this shot draws. Overrides the AI for this shot only."""
        sel = self.board_tree.selection()
        if not sel:
            messagebox.showinfo(APP, "Pick a shot in the list first.")
            return False
        who = self.v_board_subject.get().strip().upper()
        if not who:
            return False
        index = int(sel[0])
        shots = self.project.setdefault("shots", {})
        shot = shots.setdefault(str(index), {})
        shot["subject_override"] = who
        if index < len(self.cues):
            shot.setdefault("line", self.cues[index].text)
        self.dirty = True
        self._refresh_board()
        self.board_tree.selection_set(sel[0])
        self.board_tree.see(sel[0])
        self.set_status("Shot %d will be drawn as %s." % (index + 1, who))
        return True

    def set_shot_subject_and_redraw(self):
        if self.set_shot_subject() and not self.busy():
            self.redraw_shot()

    def make_storyboard(self):
        cues = self.scan_script()
        if not cues:
            messagebox.showinfo(APP, "Write or generate a script first.")
            self.nb.select(self.tab_script)
            return
        p = self._collect_ui_into_project()
        if not proj.workflow_cfg(p, "shot").get("path"):
            messagebox.showinfo(APP, "Set a picture workflow for 'shot' on the Setup tab first.")
            self.nb.select(self.tab_wf)
            return
        out = p["options"]["output_dir"] or self._default_outdir()
        self.v_outdir.set(out)
        self._refresh_board()
        self.board_progress.configure(maximum=len(cues), value=0)
        self.nb.select(self.tab_board)
        self._log_clear()
        self._start(StoryboardJob(p, cues, out, self.events.put), "Drawing the storyboard...")

    def redraw_shot(self):
        sel = self.board_tree.selection()
        if not sel:
            messagebox.showinfo(APP, "Pick a shot in the list first.")
            return
        p = self._collect_ui_into_project()
        out = p["options"]["output_dir"] or self._default_outdir()
        index = int(sel[0])
        self.board_progress.configure(maximum=1, value=0)
        self._start(StoryboardJob(p, self.cues, out, self.events.put, only=[index]),
                    "Redrawing shot %d..." % (index + 1))

    # ----------------------------------------------------------- workflow tab

    def _build_workflow_tab(self):
        t = self.tab_wf
        srv = ttk.LabelFrame(t, text="Local ComfyUI server", padding=10)
        srv.pack(fill="x")
        self.v_host = tk.StringVar()
        self.v_port = tk.StringVar()
        ttk.Label(srv, text="Host:").pack(side="left")
        ttk.Entry(srv, textvariable=self.v_host, width=16).pack(side="left", padx=(4, 12))
        ttk.Label(srv, text="Port:").pack(side="left")
        ttk.Entry(srv, textvariable=self.v_port, width=8).pack(side="left", padx=4)
        ttk.Button(srv, text="Test connection", command=self.test_connection).pack(side="left", padx=12)
        ttk.Button(srv, text="Open ComfyUI", command=self.open_comfy).pack(side="left")

        pick = ttk.Frame(t)
        pick.pack(fill="x", pady=(10, 0))
        ttk.Label(pick, text="Job:").pack(side="left")
        self.v_slot = tk.StringVar()
        self.slot_labels = {proj.WORKFLOW_SLOTS[s]["label"]: s for s in proj.WORKFLOW_SLOTS}
        cb = ttk.Combobox(pick, textvariable=self.v_slot, state="readonly", width=52,
                          values=[proj.WORKFLOW_SLOTS[s]["label"] for s in proj.WORKFLOW_SLOTS])
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._show_slot())
        self.v_slot_state = tk.StringVar(value="")
        ttk.Label(pick, textvariable=self.v_slot_state, foreground="#666").pack(side="left", padx=8)

        wfb = ttk.LabelFrame(t, text="Workflow (API format)", padding=10)
        wfb.pack(fill="x", pady=(8, 0))
        wfb.columnconfigure(1, weight=1)
        self.v_wf_path = tk.StringVar()
        ttk.Label(wfb, text="File:").grid(row=0, column=0, sticky="w")
        ttk.Entry(wfb, textvariable=self.v_wf_path).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(wfb, text="Browse...", command=self.pick_workflow).grid(row=0, column=2)
        ttk.Label(wfb, foreground="#666",
                  text="In ComfyUI: Settings > enable Dev mode, then Workflow > Export (API). "
                       "The voice slot is required; the picture slots are optional."
                  ).grid(row=1, column=1, sticky="w", padx=6, pady=(2, 0))

        self.mapbox = ttk.LabelFrame(t, text="Which inputs should ScriptVoice drive?", padding=10)
        self.mapbox.pack(fill="x", pady=(10, 0))
        self.mapbox.columnconfigure(1, weight=1)
        self.map_vars = {}

        insp = ttk.LabelFrame(t, text="Workflow inputs", padding=10)
        insp.pack(fill="both", expand=True, pady=(10, 0))
        self.wf_tree = ttk.Treeview(insp, columns=("node", "input", "value"),
                                    show="headings", height=10)
        for col, w in (("node", 280), ("input", 170), ("value", 460)):
            self.wf_tree.heading(col, text=col.title())
            self.wf_tree.column(col, width=w)
        vsb = ttk.Scrollbar(insp, command=self.wf_tree.yview)
        self.wf_tree.configure(yscrollcommand=vsb.set)
        self.wf_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

    def _slot(self):
        return self.slot_labels.get(self.v_slot.get(), "voice")

    def _show_slot(self):
        """Rebuild the mapping rows for the slot the user picked."""
        slot = self._slot()
        cfg = proj.workflow_cfg(self.project, slot)
        self.v_wf_path.set(cfg.get("path", ""))
        for child in self.mapbox.winfo_children():
            child.destroy()
        self.map_vars = {}
        wf = self.workflows.get(slot)
        choices = [""] + [r[0] for r in proj.widget_inputs(wf)] if wf else [""]

        for r, (key, hint, required) in enumerate(proj.WORKFLOW_SLOTS[slot]["keys"]):
            ttk.Label(self.mapbox, text="%s%s \u2192" % (key, "*" if required else "")).grid(
                row=r, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=(cfg.get("mapping") or {}).get(key, ""))
            box = ttk.Combobox(self.mapbox, textvariable=var, state="readonly", values=choices)
            box.grid(row=r, column=1, sticky="ew", padx=6)
            box.bind("<<ComboboxSelected>>", lambda e: self._store_mapping())
            ttk.Label(self.mapbox, text=hint, foreground="#666").grid(row=r, column=2, sticky="w")
            self.map_vars[key] = var
        ttk.Button(self.mapbox, text="Auto-detect", command=self.autodetect_mapping).grid(
            row=len(proj.WORKFLOW_SLOTS[slot]["keys"]), column=1, sticky="w", padx=6, pady=(8, 0))

        self.wf_tree.delete(*self.wf_tree.get_children())
        if wf:
            for target, cls, name, val in proj.widget_inputs(wf):
                self.wf_tree.insert("", "end", iid=target,
                                    values=("%s  #%s" % (cls, target.split(".")[0]),
                                            name, _short(val)))
            self.param_combo["values"] = [r[0] for r in proj.widget_inputs(
                self.workflows.get("voice") or wf)]
        self._slot_state()

    def _slot_state(self):
        ready, missing = [], []
        for slot in proj.WORKFLOW_SLOTS:
            cfg = proj.workflow_cfg(self.project, slot)
            need = [k for k, _, req in proj.WORKFLOW_SLOTS[slot]["keys"] if req]
            if cfg.get("path") and all((cfg.get("mapping") or {}).get(k) for k in need):
                ready.append(slot)
            else:
                missing.append(slot)
        self.v_slot_state.set("ready: %s%s" % (", ".join(ready) or "none",
                                               ("   missing: " + ", ".join(missing))
                                               if missing else ""))

    def _store_mapping(self):
        slot = self._slot()
        cfg = self.project["workflows"].setdefault(slot, {"path": "", "mapping": {}})
        cfg["mapping"] = {k: v.get().strip() for k, v in self.map_vars.items() if v.get().strip()}
        cfg["path"] = self.v_wf_path.get().strip()
        self.dirty = True
        self._slot_state()

    # -------------------------------------------------------------- movie tab

    def _build_movie_tab(self):
        t = self.tab_movie
        top = ttk.LabelFrame(t, text="Output", padding=10)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        self.v_outdir = tk.StringVar()
        ttk.Label(top, text="Folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.v_outdir).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(top, text="Browse...", command=self.pick_outdir).grid(row=0, column=2)

        opt = ttk.Frame(top)
        opt.grid(row=1, column=1, sticky="w", padx=6, pady=(8, 0))
        self.v_gap = tk.StringVar()
        self.v_reuse = tk.BooleanVar(value=True)
        ttk.Label(opt, text="Silence between lines (s):").pack(side="left")
        ttk.Entry(opt, textvariable=self.v_gap, width=6).pack(side="left", padx=6)
        ttk.Checkbutton(opt, text="Re-use anything that hasn't changed",
                        variable=self.v_reuse).pack(side="left", padx=12)

        grow = ttk.Frame(top)
        grow.grid(row=3, column=1, sticky="w", padx=6, pady=(6, 0))
        self.v_free_gpu = tk.BooleanVar(value=False)
        ttk.Checkbutton(grow, text="Free the GPU between steps",
                        variable=self.v_free_gpu).pack(side="left")
        ttk.Label(grow, foreground="#666",
                  text="unload the writing model before drawing, and ComfyUI before "
                       "writing").pack(side="left", padx=8)

        vrow = ttk.Frame(top)
        vrow.grid(row=2, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(vrow, text="Voices from:").pack(side="left")
        self.v_backend = tk.StringVar()
        self.backend_labels = {
            "A ComfyUI text-to-speech workflow": "comfyui",
            "The voices built into Windows": "system",
        }
        cb = ttk.Combobox(vrow, textvariable=self.v_backend, state="readonly", width=34,
                          values=list(self.backend_labels))
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._backend_changed())
        self.v_backend_note = tk.StringVar(value="")
        ttk.Label(vrow, textvariable=self.v_backend_note, foreground="#666").pack(side="left")

        ctl = ttk.Frame(t)
        ctl.pack(fill="x", pady=(10, 0))
        self.btn_movie = ttk.Button(ctl, text="Make the movie  (F5)", command=self.make_movie)
        self.btn_movie.pack(side="left")
        ttk.Button(ctl, text="Dialogue only", command=self.render_dialogue).pack(side="left", padx=6)
        self.btn_cancel = ttk.Button(ctl, text="Stop", command=self.cancel_job, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        ttk.Button(ctl, text="Play result", command=self.play_result).pack(side="left", padx=6)
        ttk.Button(ctl, text="Open folder", command=self.open_output_folder).pack(side="left")

        self.progress = ttk.Progressbar(t, mode="determinate")
        self.progress.pack(fill="x", pady=(10, 6))

        panes = ttk.Panedwindow(t, orient="vertical")
        panes.pack(fill="both", expand=True)
        cf = ttk.Frame(panes)
        self.cue_tree = ttk.Treeview(cf, columns=("speaker", "text", "voice", "shot"),
                                     show="headings", height=10)
        for col, w, a in (("speaker", 130, "w"), ("text", 560, "w"),
                          ("voice", 90, "center"), ("shot", 90, "center")):
            self.cue_tree.heading(col, text=col.title())
            self.cue_tree.column(col, width=w, anchor=a)
        csb = ttk.Scrollbar(cf, command=self.cue_tree.yview)
        self.cue_tree.configure(yscrollcommand=csb.set)
        self.cue_tree.pack(side="left", fill="both", expand=True)
        csb.pack(side="right", fill="y")
        self.cue_tree.bind("<Double-1>", self.play_selected_cue)
        panes.add(cf, weight=3)

        lf = ttk.Frame(panes)
        self.log = tk.Text(lf, height=8, wrap="word", state="disabled",
                           font=("Consolas", 9), background="#111111", foreground="#dddddd")
        lsb = ttk.Scrollbar(lf, command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        panes.add(lf, weight=1)

    # --------------------------------------------------------------- project

    def _load_project_into_ui(self):
        p = self.project
        o = p["options"]
        self.premise_text.delete("1.0", "end")
        self.premise_text.insert("1.0", p.get("premise", ""))
        self.script_text.delete("1.0", "end")
        self.script_text.insert("1.0", p.get("script", ""))
        self.v_default_speaker.set(o.get("default_speaker", "NARRATOR"))
        self.v_strip.set(bool(o.get("strip_parentheticals", True)))
        self.v_split.set(bool(o.get("split_sentences", False)))
        self.v_maxchars.set(str(o.get("max_chars", 0)))
        self.v_gap.set(str(o.get("gap_seconds", 0.35)))
        self.v_reuse.set(bool(o.get("reuse_unchanged", True)))
        self.v_free_gpu.set(bool(o.get("free_gpu", False)))
        backend = o.get("voice_backend", "comfyui")
        for label, key in self.backend_labels.items():
            if key == backend:
                self.v_backend.set(label)
        self.cb_sys_voice["values"] = [""] + speech.voices()
        self._backend_changed()
        self.v_outdir.set(o.get("output_dir", "") or self._default_outdir())
        self.v_max_actors.set(str(o.get("max_actors", 5)))
        self.v_frames.set(str(o.get("turnaround_frames", 8)))
        self.v_scenes.set(str(o.get("scene_count", 4)))
        self.v_host.set(p["server"].get("host", "127.0.0.1"))
        self.v_port.set(str(p["server"].get("port", 8188)))
        self.v_llm_url.set(p["llm"].get("base_url", ""))
        self.v_llm_model.set(p["llm"].get("model", ""))

        self.workflows = {}
        for slot in proj.WORKFLOW_SLOTS:
            path = proj.workflow_cfg(p, slot).get("path")
            if path and os.path.exists(path):
                try:
                    self.workflows[slot] = proj.load_workflow(path)
                except Exception:
                    pass
        self.v_slot.set(proj.WORKFLOW_SLOTS["voice"]["label"])
        self._show_slot()
        self._refresh_cast()
        self._retitle()

    def _collect_ui_into_project(self):
        p = self.project
        p["premise"] = self.premise_text.get("1.0", "end-1c").strip()
        p["script"] = self.script_text.get("1.0", "end-1c")
        o = p["options"]
        o["default_speaker"] = (self.v_default_speaker.get().strip() or "NARRATOR").upper()
        o["strip_parentheticals"] = bool(self.v_strip.get())
        o["split_sentences"] = bool(self.v_split.get())
        o["max_chars"] = _int(self.v_maxchars.get(), 0)
        o["gap_seconds"] = _float(self.v_gap.get(), 0.35)
        o["reuse_unchanged"] = bool(self.v_reuse.get())
        o["free_gpu"] = bool(self.v_free_gpu.get())
        o["voice_backend"] = self.backend_labels.get(self.v_backend.get(), "comfyui")
        o["output_dir"] = self.v_outdir.get().strip()
        o["max_actors"] = max(1, min(8, _int(self.v_max_actors.get(), 5)))
        o["turnaround_frames"] = max(2, min(24, _int(self.v_frames.get(), 8)))
        o["scene_count"] = max(1, min(20, _int(self.v_scenes.get(), 4)))
        p["server"]["host"] = self.v_host.get().strip() or "127.0.0.1"
        p["server"]["port"] = _int(self.v_port.get(), 8188)
        p["llm"]["base_url"] = self.v_llm_url.get().strip()
        p["llm"]["model"] = self.v_llm_model.get().strip()
        # The look box belongs to whichever actor is selected. Collecting it here
        # means New look and Regenerate pick up what was just typed, with no need
        # to press Save first.
        if hasattr(self, "look_text") and self.selected_actor:
            actor = (p.get("characters") or {}).get(self.selected_actor)
            if actor is not None:
                typed = self.look_text.get("1.0", "end-1c").strip()
                if typed != (actor.get("look_note") or ""):
                    actor["look_note"] = typed
                    self.dirty = True
                ref = self.v_reference.get().strip()
                if ref != (actor.get("reference_image") or ""):
                    actor["reference_image"] = ref
                    self.dirty = True
                written = self.who_text.get("1.0", "end-1c").strip()
                if written and written != (actor.get("one_line") or ""):
                    actor["one_line"] = written
                    self.dirty = True
                if self.dirty:
                    # Redraw just this card so the edit is visible at once. A full
                    # _refresh_cast here would re-enter select_actor and overwrite
                    # the very box being typed in.
                    card = self.cards.get(self.selected_actor)
                    if card is not None:
                        card.refresh(actor)
        if self.map_vars:
            self._store_mapping()
        return p

    def _retitle(self):
        name = os.path.basename(self.project_path) if self.project_path else "Untitled"
        self.master.title("%s - %s%s" % (APP, name, " *" if self.dirty else ""))

    def _default_outdir(self):
        base = os.path.dirname(self.project_path) if self.project_path else os.getcwd()
        return os.path.join(base, "output")

    def new_project(self):
        if not self._confirm_discard():
            return
        self.project = proj.new_project()
        self.project_path = ""
        self.cues = []
        self.cue_tree.delete(*self.cue_tree.get_children())
        self._load_project_into_ui()
        self.dirty = False
        self._retitle()
        self.set_status("New project.")

    def open_project(self):
        if not self._confirm_discard():
            return
        path = filedialog.askopenfilename(
            title="Open project",
            filetypes=[("ScriptVoice project", "*" + PROJ_EXT), ("All files", "*.*")])
        if not path:
            return
        try:
            self.project = proj.load(path)
        except Exception as e:
            messagebox.showerror(APP, "Couldn't open that project:\n%s" % e)
            return
        self.project_path = path
        adopted = proj.adopt_default_workflows(self.project)
        self._load_project_into_ui()
        self.dirty = bool(adopted)
        self._retitle()
        if adopted:
            self.set_status("Opened %s - filled %d empty workflow slot%s from the "
                            "bundled workflow." % (os.path.basename(path), len(adopted),
                                                   "" if len(adopted) == 1 else "s"))
            self._log("This project had no picture workflow, so %s now use the one "
                      "shipped with the program. Load your own on the Setup tab to "
                      "change that." % ", ".join(adopted))
        else:
            self.set_status("Opened %s" % os.path.basename(path))

    def save_project(self):
        if not self.project_path:
            return self.save_project_as()
        proj.save(self._collect_ui_into_project(), self.project_path)
        self.dirty = False
        self._retitle()
        self.set_status("Saved %s" % os.path.basename(self.project_path))

    def save_project_as(self):
        path = filedialog.asksaveasfilename(
            title="Save project", defaultextension=PROJ_EXT,
            filetypes=[("ScriptVoice project", "*" + PROJ_EXT)])
        if not path:
            return
        self.project_path = path
        self.save_project()

    def import_script(self):
        path = filedialog.askopenfilename(
            title="Import script",
            filetypes=[("Text", "*.txt *.md *.fountain"), ("All files", "*.*")])
        if not path:
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        self.script_text.delete("1.0", "end")
        self.script_text.insert("1.0", text)
        self.dirty = True
        self.nb.select(self.tab_script)
        self.compile_script()

    def _confirm_discard(self):
        if not self.dirty:
            return True
        r = messagebox.askyesnocancel(APP, "Save changes to the current project first?")
        if r is None:
            return False
        if r:
            self.save_project()
        return True

    # ------------------------------------------------------------ local model

    def detect_llm(self):
        self.set_status("Looking for a local model server...")

        def work():
            found = llm_mod.discover()
            self.events.put({"kind": "llm_found", "servers": found})

        threading.Thread(target=work, daemon=True).start()

    def load_models(self):
        url = self.v_llm_url.get().strip()
        if not url:
            return
        def work():
            try:
                models = llm_mod.LocalLLM(url).models()
                self.events.put({"kind": "llm_models", "models": models})
            except llm_mod.LLMError as e:
                self.events.put({"kind": "llm_models", "models": [], "error": str(e)})
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- casting

    def create_cast(self, steps=None):
        p = self._collect_ui_into_project()
        if not p["premise"]:
            messagebox.showinfo(APP, "Write the premise first - one or two sentences.")
            self.nb.select(self.tab_premise)
            return
        if not self.ensure_llm():
            return
        p = self._collect_ui_into_project()
        if steps is None:
            steps = ("cast", "roles", "portrait", "spin", "voice")
        if "cast" in steps and p.get("characters"):
            if not messagebox.askyesno(
                    APP, "This replaces the current cast and everything rendered for them.\n\n"
                         "Continue?"):
                return
        out = p["options"]["output_dir"] or self._default_outdir()
        self.v_outdir.set(out)
        self._log_clear()
        self.nb.select(self.tab_cast)
        self._start(CastJob(p, out, self.events.put, steps=steps), "Casting...")

    WAITING = {"actor": "Recasting...", "voice": "Choosing a new voice...",
               "look": "Starting a new render...", "spin": "Starting the turnaround...",
               "role": "Reading their lines in the script..."}

    def regenerate(self, name, what, note=""):
        p = self._collect_ui_into_project()
        out = p["options"]["output_dir"] or self._default_outdir()
        card = self.cards.get(name)
        if card is not None:      # feedback on the click itself, not on the first
            card.set_progress(self.WAITING.get(what, "Working..."))   # worker event
        self._start(RegenerateJob(p, out, self.events.put, name, what=what, note=note),
                    "Regenerating %s for %s..." % (what, name))

    def ensure_llm(self):
        """True if a model server is answering - offering to start one if not.

        Writing needs the model; drawing does not. Rather than failing a click
        outright, ask once and start LM Studio ourselves.
        """
        url = self.v_llm_url.get() or "http://127.0.0.1:1234/v1"
        try:
            llm_mod.LocalLLM(url).models()
            return True
        except llm_mod.LLMError:
            pass
        found = llm_mod.discover()
        if found:                       # something else is already serving
            self.v_llm_url.set(found[0][1])
            self.set_status("Using %s at %s" % (found[0][0], found[0][1]))
            return True
        if not llm_mod.find_lmstudio_cli():
            messagebox.showerror(
                APP, "No local model server is running, and LM Studio's command-line "
                     "tool wasn't found so it can't be started for you.\n\n"
                     "Open LM Studio and use Developer > Start Server, then try again.")
            return False
        if not messagebox.askyesno(
                APP, "This step needs the AI to write, and no model server is "
                     "running.\n\nStart LM Studio now?\n\nIt will share the graphics "
                     "card with ComfyUI, so drawing will be slower while it is "
                     "loaded. Drawing on its own (New look) does not need it."):
            return False
        self.set_status("Starting LM Studio...")
        self.update_idletasks()
        try:
            base = llm_mod.start_lmstudio()
        except llm_mod.LLMError as e:
            messagebox.showerror(APP, str(e))
            self.set_status("LM Studio did not start.")
            return False
        self.v_llm_url.set(base)
        self.dirty = True
        self.load_models()
        self.set_status("LM Studio is running at %s." % base)
        return True

    def _card_action(self, name, action, value):
        if self.busy() and action not in ("approve",):
            messagebox.showinfo(APP, "Something is already running.")
            return
        actor = (self.project.get("characters") or {}).get(name)
        if not actor:
            return
        if action == "approve":
            actor["approved"] = bool(value)
            self.dirty = True
            self._update_gate()
            return
        if action == "lead":
            actor["lead"] = bool(value)
            actor["role"] = "lead" if value else "supporting"
            self.dirty = True
            self._refresh_cast()
            self.set_status("%s is %s the main character." % (name, "now" if value else "no longer"))
            return
        if action == "play":
            path = actor.get("voice_sample")
            if path and os.path.exists(path):
                _play(path)
            else:
                messagebox.showinfo(APP, "No voice sample yet for %s." % name)
            return
        if action == "find":
            self.find_in_script(name)
            return
        if action == "remove":
            self.remove_character(name)
            return
        if action == "same":
            self.merge_character(name)
            return
        if action in ("actor", "voice", "look"):
            note = ""
            if action in ("actor", "voice"):
                if not self.ensure_llm():
                    return
                note = _ask_string(self, "Regenerate %s" % action,
                                   "Optional note for the AI (leave blank for a free hand):") or ""
            self.select_actor(name)
            self.regenerate(name, action, note)

    def save_look(self, redraw=False):
        """Store the user's own appearance words on the selected actor."""
        name = self.selected_actor
        actor = (self.project.get("characters") or {}).get(name)
        if not actor:
            messagebox.showinfo(APP, "Select an actor first.")
            return False
        self._collect_ui_into_project()
        self.dirty = True
        self._refresh_cast()
        self.set_status("Saved the look for %s." % name)
        return True

    def merge_character(self, name):
        """Fold this character's lines into another - MAN is really SAM."""
        chars = self.project.get("characters") or {}
        others = [n for n in [a["name"] for a in proj.cast(self.project)] if n != name]
        if not others:
            messagebox.showinfo(APP, "There is nobody else in the cast to merge into.")
            return
        target = _ask_choice(self, "Same person",
                            "%s is really which character?" % name, others)
        if not target or target not in chars:
            return
        keep = chars[target]
        aliases = [a for a in (keep.get("aliases") or []) if a != target]
        for extra in [name] + list(chars.get(name, {}).get("aliases") or []):
            if extra not in aliases and extra != target:
                aliases.append(extra)
        keep["aliases"] = aliases
        chars.pop(name, None)
        self.project["cast_order"] = [n for n in (self.project.get("cast_order") or [])
                                      if n != name]
        if self.selected_actor == name:
            self.selected_actor = target
        self.dirty = True
        self.scan_script()
        self._refresh_cast()
        self.select_actor(target)
        self.set_status("%s is now part of %s. %s speaks %d line%s."
                        % (name, target, target, keep.get("line_count", 0),
                           "" if keep.get("line_count", 0) == 1 else "s"))

    def split_character(self):
        """Undo a merge: give one absorbed name its own card back."""
        name = self.selected_actor
        actor = (self.project.get("characters") or {}).get(name)
        if not actor:
            return
        aliases = list(actor.get("aliases") or [])
        if not aliases:
            messagebox.showinfo(APP, "%s hasn't absorbed anyone." % name)
            return
        which = _ask_choice(self, "Split off", "Give which name its own card back?",
                            aliases)
        if not which:
            return
        actor["aliases"] = [a for a in aliases if a != which]
        chars = self.project.setdefault("characters", {})
        if which not in chars:
            chars[which] = proj.new_character(which)
            self.project.setdefault("cast_order", []).append(which)
        self.dirty = True
        self.scan_script()
        self._refresh_cast()
        self.select_actor(which)
        self.set_status("%s is a separate character again." % which)

    def add_character(self):
        """Put a character in the cast by hand, with no AI involved."""
        raw = _ask_string(self, "Add a character",
                          "Name, as it appears in the script (e.g. SAM, BANKER):")
        name = script_parser._norm(raw or "")
        if not name:
            return
        chars = self.project.setdefault("characters", {})
        if name in chars:
            messagebox.showinfo(APP, "%s is already in the cast." % name)
            self.select_actor(name)
            return
        chars[name] = proj.new_character(name)
        order = self.project.setdefault("cast_order", [])
        if name not in order:
            order.append(name)
        self.dirty = True
        self._refresh_cast()
        self.select_actor(name)
        self.set_status("Added %s. Type their look and what they do, then press New look."
                        % name)

    def remove_character(self, name):
        """Take a character out of the cast. Their files on disk are left alone."""
        actor = (self.project.get("characters") or {}).get(name)
        if not actor:
            return
        made = [w for w, got in (("a portrait", actor.get("portrait")),
                                 ("a turnaround", actor.get("turnaround")),
                                 ("a voice sample", actor.get("voice_sample"))) if got]
        extra = ("\n\nThey have %s. Those files stay on disk, so adding them back "
                 "with the same name picks them up again." % " and ".join(made)) if made else ""
        if not messagebox.askyesno(APP, "Remove %s from the cast?%s" % (name, extra)):
            return
        self.project["characters"].pop(name, None)
        self.project["cast_order"] = [n for n in (self.project.get("cast_order") or [])
                                      if n != name]
        if self.selected_actor == name:
            self.selected_actor = ""
        self.dirty = True
        self._refresh_cast()
        self.set_status("Removed %s from the cast." % name)

    def find_in_script(self, name):
        """Show the Script tab with this character's lines highlighted.

        Half the cast of a real screenplay is MAN, INVESTOR, BANKER - names that
        say nothing. Seeing their actual lines is the fastest way to know who
        they are.
        """
        self._collect_ui_into_project()
        cues = self.scan_script()
        places = [c for c in (cues or []) if c.speaker == name]
        self.nb.select(self.tab_script)
        self.script_text.tag_remove("whois", "1.0", "end")
        if not places:
            self.set_status("%s does not speak anywhere in the script." % name)
            messagebox.showinfo(
                APP, "%s has no lines in the script.\n\nThey may be spelled "
                     "differently there, or only appear in the action." % name)
            return
        self.script_text.tag_configure("whois", background="#fff3a3", foreground="#000")
        for c in places:
            self.script_text.tag_add("whois", "%d.0" % c.line_no, "%d.end" % c.line_no)
        first = places[0].line_no
        self.script_text.see("%d.0" % max(1, first - 2))
        self.script_text.mark_set("insert", "%d.0" % first)
        self.script_text.focus_set()
        self.set_status("%s: %d line%s, first at line %d. Highlighted in the script."
                        % (name, len(places), "" if len(places) == 1 else "s", first))

    def pick_reference(self):
        """Choose your own picture of this character for every render."""
        name = self.selected_actor
        actor = (self.project.get("characters") or {}).get(name)
        if not actor:
            messagebox.showinfo(APP, "Select an actor first.")
            return
        path = filedialog.askopenfilename(
            title="Reference picture for %s" % name,
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp"), ("All files", "*.*")])
        if not path:
            return
        actor["reference_image"] = path
        self.v_reference.set(path)
        self.dirty = True
        self._refresh_cast()
        self.select_actor(name)
        self.set_status("%s will be drawn from %s." % (name, os.path.basename(path)))

    def clear_reference(self):
        """Go back to the portrait the program drew."""
        name = self.selected_actor
        actor = (self.project.get("characters") or {}).get(name)
        if not actor:
            return
        actor["reference_image"] = ""
        self.v_reference.set("")
        self.dirty = True
        self.select_actor(name)
        self.set_status("%s is back to the drawn portrait." % name)

    def save_who(self):
        """Store the user's own words about who this character is."""
        if not self.selected_actor:
            messagebox.showinfo(APP, "Select an actor first.")
            return False
        self._collect_ui_into_project()
        self._refresh_cast()
        self.set_status("Saved the description for %s." % self.selected_actor)
        return True

    def save_who_and_judge(self):
        if self.save_who() and not self.busy():
            if self.ensure_llm():
                self.regenerate(self.selected_actor, "role")

    def save_look_and_redraw(self):
        if self.save_look() and not self.busy():
            self.regenerate(self.selected_actor, "look")

    def select_actor(self, name):
        actor = (self.project.get("characters") or {}).get(name)
        if not actor:
            return
        self.selected_actor = name
        self.v_sel_name.set("%s - %s" % (name, actor.get("voice_type", "") or "no voice yet"))
        self.v_voice_file.set(actor.get("voice_file", ""))
        self.v_voice_value.set(actor.get("voice_value", ""))
        self.v_seed.set(str(actor.get("seed", -1)))
        override = actor.get("system_voice") or {}
        assigned = system_voice(actor)
        self.v_sys_voice.set(override.get("voice", ""))
        self.v_sys_rate.set(str(override.get("rate", assigned.get("rate", 0))))
        self.v_sys_pitch.set(str(override.get("pitch", assigned.get("pitch", 0))))
        self.v_sys_note.set(("chosen: " if override.get("voice") else "auto: ")
                            + speech.describe_voice(assigned))
        self.look_text.delete("1.0", "end")
        self.look_text.insert("1.0", actor.get("look_note", ""))
        self.who_text.delete("1.0", "end")
        self.who_text.insert("1.0", actor.get("one_line", ""))
        self.v_reference.set(actor.get("reference_image", ""))
        using = visuals.identity_image(actor)
        if actor.get("reference_image"):
            self.v_reference_note.set("Your own picture. Every shot of %s is drawn "
                                      "from it." % name)
        elif using:
            self.v_reference_note.set("Using the portrait the program drew. Choose your "
                                      "own picture to override it.")
        else:
            self.v_reference_note.set("Nothing to lock onto yet - press New look, or "
                                      "choose a picture of your own.")
        frames = list(actor.get("turnaround") or [])
        if not frames and actor.get("portrait"):
            frames = [actor["portrait"]]
        self.spin.set_frames(frames, "%s - %d frame%s" % (name, len(frames),
                                                          "" if len(frames) == 1 else "s"))
        self._refresh_params(actor)

    def _refresh_cast(self):
        actors = proj.cast(self.project)
        names = [a["name"] for a in actors]
        # How many lines each one has, so a card can say "38 lines in the script".
        counts = {}
        for c in (self.cues or []):
            counts[c.speaker] = counts.get(c.speaker, 0) + 1
        for a in actors:
            a["line_count"] = counts.get(a["name"], 0)
        if set(names) != set(self.cards):
            self.cast_scroll.clear()
            self.cards = {}
            for a in actors:
                card = ActorCard(self.cast_scroll.body, a, self._card_action, self.select_actor)
                card.pack(fill="x", pady=4, padx=2)
                self.cards[a["name"]] = card
        else:
            for a in actors:
                self.cards[a["name"]].refresh(a)
        approved = sum(1 for a in actors if a.get("approved"))
        described = sum(1 for a in actors if a.get("one_line"))
        self.v_cast_summary.set(
            "%d character%s | %d described | %d approved"
            % (len(actors), "" if len(actors) == 1 else "s", described, approved)
            if actors else "No cast yet - write a premise and press Create the cast.")
        if self.selected_actor in self.cards:
            self.select_actor(self.selected_actor)
        elif actors:
            self.select_actor(actors[0]["name"])
        self._update_gate()

    def _update_gate(self):
        actors = proj.cast(self.project)
        pending = [a["name"] for a in actors if not a.get("approved")]
        if actors and not pending:
            self.v_gate.set("Every actor is approved.")
            self.btn_movie_gate.configure(state="normal")
        else:
            self.v_gate.set("Waiting on approval: %s" % (", ".join(pending) or "-")
                            if actors else "")
            self.btn_movie_gate.configure(state="disabled")
        for a in actors:
            if a["name"] in self.cards:
                self.cards[a["name"]].v_approved.set(bool(a.get("approved")))

    def approve_all(self):
        for a in proj.cast(self.project):
            a["approved"] = True
        self.dirty = True
        self._refresh_cast()

    def save_voice_details(self):
        actor = (self.project.get("characters") or {}).get(self.selected_actor)
        if not actor:
            return
        actor["voice_file"] = self.v_voice_file.get().strip()
        actor["voice_value"] = self.v_voice_value.get().strip()
        actor["seed"] = _int(self.v_seed.get(), -1)
        chosen = self.v_sys_voice.get().strip()
        actor["system_voice"] = {"voice": chosen,
                                 "rate": _int(self.v_sys_rate.get(), 0),
                                 "pitch": _int(self.v_sys_pitch.get(), 0)} if chosen else {}
        self.dirty = True
        self._refresh_cast()
        self.set_status("Saved voice details for %s." % self.selected_actor)

    def _backend_changed(self):
        """Say plainly what the chosen voice source can and can't do."""
        backend = self.backend_labels.get(self.v_backend.get(), "comfyui")
        if backend == "system":
            names = speech.voices()
            self.v_backend_note.set(
                "   %d voice%s installed - robotic, but needs nothing"
                % (len(names), "" if len(names) == 1 else "s") if names
                else "   no Windows voices found on this machine")
        else:
            ready = bool(proj.workflow_cfg(self.project, "voice").get("path"))
            self.v_backend_note.set("   using the voice workflow" if ready
                                    else "   no voice workflow set yet (tab 4)")
        self.dirty = True

    def preview_system_voice(self):
        """Speak this actor's sample line with their Windows voice."""
        actor = (self.project.get("characters") or {}).get(self.selected_actor)
        if not actor:
            return
        if not speech.available():
            messagebox.showinfo(APP, "Windows speech isn't available on this machine.")
            return
        self.save_voice_details()
        line = actor.get("sample_line") or "This is how I sound in this film."
        out = os.path.join(self.v_outdir.get() or self._default_outdir(), "_previews")

        def work():
            try:
                v = system_voice(actor)
                path = speech.speak_to_wav(
                    line, os.path.join(out, "%s_system.wav" % actor["name"].replace(" ", "_")),
                    voice=v["voice"], rate=v["rate"], pitch=v["pitch"])
                self.events.put({"kind": "played", "path": path})
            except Exception as e:
                self.events.put({"kind": "failed", "message": str(e), "cancelled": False})

        self.set_status("Speaking %s..." % actor["name"])
        threading.Thread(target=work, daemon=True).start()

    def pick_voice_file(self):
        path = filedialog.askopenfilename(
            title="Reference voice clip",
            filetypes=[("Audio", "*.wav *.mp3 *.flac *.ogg *.m4a"), ("All files", "*.*")])
        if path:
            self.v_voice_file.set(path)

    def _refresh_params(self, actor):
        self.param_tree.delete(*self.param_tree.get_children())
        wf = self.workflows.get("voice")
        for target, value in sorted((actor.get("params") or {}).items()):
            self.param_tree.insert("", "end", iid=target,
                                   text=proj.describe(wf, target) if wf else target,
                                   values=(value,))

    def set_param(self):
        actor = (self.project.get("characters") or {}).get(self.selected_actor)
        target = self.v_param_target.get().strip()
        if not actor or not target:
            messagebox.showinfo(APP, "Select an actor and a workflow input first.")
            return
        actor.setdefault("params", {})[target] = _coerce(self.v_param_value.get())
        self._refresh_params(actor)
        self.dirty = True

    def remove_param(self):
        actor = (self.project.get("characters") or {}).get(self.selected_actor)
        sel = self.param_tree.selection()
        if not actor or not sel:
            return
        for iid in sel:
            (actor.get("params") or {}).pop(iid, None)
        self._refresh_params(actor)
        self.dirty = True

    # ----------------------------------------------------------------- script

    def write_script(self):
        p = self._collect_ui_into_project()
        actors = proj.cast(p)
        if not actors:
            messagebox.showinfo(APP, "Create the cast first - the AI writes only for them.")
            self.nb.select(self.tab_premise)
            return
        if not self.ensure_llm():
            return
        p = self._collect_ui_into_project()
        if self.script_text.get("1.0", "end-1c").strip():
            if not messagebox.askyesno(APP, "Replace the current script?"):
                return
        self.nb.select(self.tab_script)
        self._start(_ScriptJob(p, self.events.put), "Writing the script...")

    def _on_tab_changed(self, _evt=None):
        """Re-read the script whenever the user moves on from it.

        Editing the script and then wondering where the characters went is the
        obvious trap, so leaving the tab quietly parses it.
        """
        if not hasattr(self, "script_text"):
            return
        if self.nb.select() in (str(self.tab_cast), str(self.tab_board)):
            self._collect_ui_into_project()
            text = self.project.get("script", "")
            if text.strip() and text != getattr(self, "_last_compiled", None):
                self.scan_script()

    def compile_script(self):
        """Read the script, add every speaker to the cast, and say what happened."""
        before = set(self.project.get("characters") or {})
        cues = self.scan_script()
        if not cues:
            messagebox.showinfo(
                APP, "Nothing to compile yet.\n\nPaste a script, or press Load a script "
                     "file, or write one on the Premise tab.")
            return []
        added = [n for n in (self.project.get("characters") or {}) if n not in before]
        speakers = script_parser.speakers(cues)
        self.set_status("Compiled %d lines from %d characters." % (len(cues), len(speakers)))
        messagebox.showinfo(
            APP, "%d spoken lines from %d characters.\n\n%s\n\n%s"
                 % (len(cues), len(speakers), ", ".join(speakers),
                    ("Added to the cast: %s" % ", ".join(added)) if added
                    else "Everyone was already in the cast."))
        self.nb.select(self.tab_cast)
        return cues

    def scan_script(self):
        self._collect_ui_into_project()
        o = self.project["options"]
        self.cues = script_parser.parse(
            self.project["script"], default_speaker=o["default_speaker"],
            strip_parentheticals=o["strip_parentheticals"],
            split_sentences=o["split_sentences"], max_chars=o["max_chars"])
        # MAN and SAM are the same person if the user has said so, and that has
        # to be true before the cast is counted or anything is rendered.
        proj.apply_aliases(self.project, self.cues)
        names = script_parser.speakers(self.cues)
        unknown = [n for n in names if n not in self.project["characters"]]
        for n in unknown:                       # keep the script renderable
            self.project["characters"][n] = proj.new_character(n)
            self.project.setdefault("cast_order", []).append(n)
        self._refresh_cast()
        self._refresh_cue_tree()
        self._refresh_board()
        self.script_info.set(
            "%d lines, %d speakers%s." % (len(self.cues), len(names),
                                          (" (%s added to the cast)" % ", ".join(unknown))
                                          if unknown else ""))
        self.dirty = True
        # Remember what was parsed, so moving between tabs does not re-parse an
        # unchanged script. This was never set, so every visit to Cast or
        # Storyboard re-read the whole script and rebuilt every list.
        self._last_compiled = self.project["script"]
        return self.cues

    # ----------------------------------------------------------------- render

    def render_dialogue(self):
        cues = self.scan_script()
        if not cues:
            messagebox.showinfo(APP, "There's nothing to speak - the script is empty.")
            return
        p = self._collect_ui_into_project()
        out = p["options"]["output_dir"] or self._default_outdir()
        self.v_outdir.set(out)
        self.progress.configure(maximum=len(cues), value=0)
        self.nb.select(self.tab_movie)
        self._log_clear()
        self._start(RenderJob(p, cues, os.path.join(out, "audio"), self.events.put),
                    "Recording dialogue...")

    def make_movie(self):
        cues = self.scan_script()
        if not cues:
            messagebox.showinfo(APP, "Write or generate a script first.")
            self.nb.select(self.tab_script)
            return
        p = self._collect_ui_into_project()
        actors = proj.cast(p)
        pending = [a["name"] for a in actors if not a.get("approved")]
        if pending:
            if not messagebox.askyesno(
                    APP, "These actors aren't approved yet:\n\n%s\n\n"
                         "Make the movie anyway?" % ", ".join(pending)):
                self.nb.select(self.tab_cast)
                return
        out = p["options"]["output_dir"] or self._default_outdir()
        self.v_outdir.set(out)
        self.progress.configure(maximum=len(cues), value=0)
        self._refresh_cue_tree()
        self.nb.select(self.tab_movie)
        self._log_clear()
        self._start(MovieJob(p, cues, out, self.events.put), "Making the movie...")

    def _refresh_cue_tree(self):
        self.cue_tree.delete(*self.cue_tree.get_children())
        for c in self.cues:
            self.cue_tree.insert("", "end", iid=str(c.index),
                                 values=(c.speaker, _short(c.text, 160), "queued", "queued"))

    def play_result(self):
        out = self.v_outdir.get() or self._default_outdir()
        for candidate in (os.path.join(out, "movie.mp4"),
                          os.path.join(out, "audio", "full_take.wav")):
            if os.path.exists(candidate):
                return _play(candidate)
        messagebox.showinfo(APP, "Nothing rendered yet.")

    def play_selected_cue(self, _e=None):
        sel = self.cue_tree.selection()
        if not sel:
            return
        tags = self.cue_tree.item(sel[0], "tags") or ()
        if tags and os.path.exists(tags[0]):
            _play(tags[0])
        else:
            self.set_status("That line hasn't been rendered yet.")

    def open_output_folder(self):
        out = self.v_outdir.get() or self._default_outdir()
        if not os.path.isdir(out):
            messagebox.showinfo(APP, "Nothing rendered yet.")
            return
        if sys.platform.startswith("win"):
            os.startfile(out)
        else:
            webbrowser.open("file://" + out)

    def pick_outdir(self):
        d = filedialog.askdirectory(title="Output folder")
        if d:
            self.v_outdir.set(d)

    # -------------------------------------------------------------- workflows

    def pick_workflow(self):
        path = filedialog.askopenfilename(
            title="ComfyUI workflow (API format)",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if path:
            self._load_workflow(self._slot(), path)

    def _load_workflow(self, slot, path, quiet=False):
        try:
            wf = proj.load_workflow(path)
        except Exception as e:
            if not quiet:
                messagebox.showerror(APP, str(e))
            return
        self.workflows[slot] = wf
        cfg = self.project["workflows"].setdefault(slot, {"path": "", "mapping": {}})
        cfg["path"] = path
        self.v_wf_path.set(path)
        if not (cfg.get("mapping") or {}):
            cfg["mapping"] = proj.guess_mapping(wf, slot)
        self._show_slot()
        self.dirty = True
        if not quiet:
            self.set_status("Loaded %d-node workflow for %s."
                            % (len(wf), proj.WORKFLOW_SLOTS[slot]["label"]))

    def autodetect_mapping(self):
        slot = self._slot()
        wf = self.workflows.get(slot)
        if not wf:
            messagebox.showinfo(APP, "Load a workflow for this job first.")
            return
        guess = proj.guess_mapping(wf, slot)
        for key, var in self.map_vars.items():
            var.set(guess.get(key, ""))
        self._store_mapping()
        self.set_status("Auto-detected: %s" % ", ".join(
            "%s=%s" % (k, v) for k, v in guess.items() if v) or "nothing found")

    def test_connection(self):
        self._collect_ui_into_project()
        c = ComfyClient(self.project["server"]["host"], self.project["server"]["port"])

        def work():
            try:
                self.events.put({"kind": "conn", "ok": True, "message": c.ping()})
                return
            except ComfyError as e:
                first = str(e)
            found = comfy_mod.find_server(self.project["server"]["host"])
            if found:
                self.events.put({"kind": "conn", "ok": True, "port": found,
                                 "message": ComfyClient(self.project["server"]["host"],
                                                        found).ping()})
            else:
                self.events.put({"kind": "conn", "ok": False, "message": first})

        threading.Thread(target=work, daemon=True).start()

    def open_comfy(self):
        self._collect_ui_into_project()
        webbrowser.open("http://%s:%d" % (self.project["server"]["host"],
                                          self.project["server"]["port"]))

    # ------------------------------------------------------------ job control

    def busy(self):
        return bool(self.job and self.job.is_alive())

    def _start(self, job, label):
        if self.busy():
            messagebox.showinfo(APP, "Something is already running.")
            return
        self.job = job
        self.set_status(label)
        self.btn_cancel.configure(state="normal")
        for b in (self.btn_cast, self.btn_movie, self.btn_movie_gate, self.btn_board):
            b.configure(state="disabled")
        job.start()

    def cancel_job(self):
        if self.busy():
            self.job.cancel()
            try:
                ComfyClient(self.project["server"]["host"],
                            self.project["server"]["port"]).interrupt()
            except Exception:
                pass
            self.set_status("Stopping...")

    def _job_finished(self):
        for card in self.cards.values():
            card.clear_progress()       # a failed job must not leave a bar marching
        self.btn_cancel.configure(state="disabled")
        for b in (self.btn_cast, self.btn_movie, self.btn_board):
            b.configure(state="normal")
        self._update_gate()

    # ---------------------------------------------------------- event pumping

    def _drain_events(self):
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        self.after(120, self._drain_events)

    def _handle_event(self, e):
        kind = e.get("kind")
        if kind == "log":
            self._log(e["message"])
        elif kind == "step":
            self.set_status(e["label"] + (" (%d/%d)" % (e["done"], e["total"])
                                          if e.get("total") else ""))
        elif kind == "conn":
            self._set_conn(comfy=e["message"] if e["ok"] else "unreachable")
            if not e["ok"]:
                messagebox.showerror(APP, "Couldn't reach ComfyUI.\n\n%s\n\n"
                                          "Start ComfyUI first, then try again." % e["message"])
        elif kind == "llm_found":
            self._servers = e["servers"]
            urls = [s[1] for s in self._servers]
            self.cb_llm["values"] = urls
            if urls:
                if self.v_llm_url.get() not in urls:
                    self.v_llm_url.set(urls[0])
                self.load_models()
                self.set_status("Found: %s" % ", ".join("%s (%s)" % (s[0], s[1])
                                                        for s in self._servers))
            else:
                self._set_conn(model="none found")
                messagebox.showinfo(
                    APP, "No local model server answered on 127.0.0.1.\n\n"
                         "Start LM Studio (Developer > Start Server) or run `ollama serve`, "
                         "then press Detect again.")
        elif kind == "llm_models":
            models = e.get("models") or []
            self.cb_model["values"] = models
            if models and self.v_llm_model.get() not in models:
                self.v_llm_model.set(models[0])
            self._set_conn(model=self.v_llm_model.get() or (e.get("error") or "no model"))
            self.dirty = True
        elif kind == "played":
            self.set_status("Played %s" % os.path.basename(e["path"]))
            _play(e["path"])
        elif kind == "cast_updated":
            self._refresh_cast()
            self.dirty = True
        elif kind == "actor_progress":
            card = self.cards.get(e.get("name"))
            if card is not None:
                card.set_progress(e.get("label", ""), e.get("done", 0), e.get("total", 0))
        elif kind == "actor_updated":
            self._refresh_cast()
            if e.get("name") == self.selected_actor:
                self.select_actor(e["name"])
        elif kind == "asset":
            name, asset = e.get("name"), e.get("asset")
            if name in self.cards:
                self.cards[name].refresh(self.project["characters"][name])
            if asset == "spin" and name == self.selected_actor:
                self.select_actor(name)
            elif asset == "spin_frame":
                self.set_status("%s turnaround frame %d/%d"
                                % (name, e.get("index", 0) + 1, e.get("total", 0)))
        elif kind == "cue_start":
            self._cue_state(e["index"], "voice", "speaking...")
            self.set_status("Line %d/%d - %s" % (e["index"] + 1, e["total"], e["speaker"]))
        elif kind == "cue_done":
            self._cue_state(e["index"], "voice", "re-used" if e.get("cached") else "done",
                            tag=e.get("file"))
            self.progress.configure(value=e["index"] + 1)
        elif kind == "shot_done":
            self._cue_state(e["index"], "shot", "re-used" if e.get("cached") else "done")
            iid = str(e["index"])
            if self.board_tree.exists(iid):
                self.board_tree.set(iid, "state", "re-used" if e.get("cached") else "drawn")
                self.board_tree.see(iid)
                if self.board_tree.selection() and self.board_tree.selection()[0] == iid:
                    self._on_board_select()
            self.board_progress.configure(value=self.board_progress["value"] + 1)
        elif kind == "finished":
            self._job_finished()
            self._on_job_result(e)
        elif kind == "failed":
            self._job_finished()
            self._log("ERROR: " + e["message"])
            self.set_status("Stopped." if e.get("cancelled") else "Failed.")
            if not e.get("cancelled"):
                messagebox.showerror(APP, e["message"])

    def _on_job_result(self, e):
        job = e.get("job")
        if job == "cast":
            self._refresh_cast()
            if e.get("visuals") is False:
                self.set_status("Cast written, but nothing drawn - ComfyUI is unreachable.")
                messagebox.showinfo(
                    APP, "The cast is written.\n\nNo portraits, turnarounds "
                         "or voice samples were made, because ComfyUI couldn't be "
                         "reached. Start it, press Test connection on the Setup tab, "
                         "then use New look on each actor.")
            else:
                self.set_status("Cast ready: %s" % ", ".join(e.get("cast") or []))
            self.nb.select(self.tab_cast)
        elif job == "regen":
            self._refresh_cast()
            if e.get("visuals") is False:
                self._no_server_dialog(e.get("name", "The character"), e.get("what", ""))
            else:
                self.set_status("%s regenerated." % e.get("name", ""))
        elif job == "script":
            self.script_text.delete("1.0", "end")
            self.script_text.insert("1.0", e.get("script", ""))
            self.dirty = True
            self.scan_script()
            self.set_status("Script written.")
        elif job == "voice":
            self.set_status("Dialogue recorded.")
            if e.get("stitched") and messagebox.askyesno(APP, "Dialogue done. Play the full take?"):
                _play(e["stitched"])
        elif job == "storyboard":
            self._refresh_board()
            self.set_status("Storyboard ready: %d shots drawn." % e.get("drawn", 0))
            children = self.board_tree.get_children()
            if children and not self.board_tree.selection():
                self.board_tree.selection_set(children[0])
        elif job == "movie":
            if e.get("movie"):
                self.set_status("Movie ready: %s" % e["movie"])
                if messagebox.askyesno(APP, "The movie is cut.\n\n%s\n\nPlay it now?"
                                            % e["movie"]):
                    _play(e["movie"])
            else:
                self.set_status("Assets ready (no ffmpeg, so no movie file).")
                messagebox.showinfo(
                    APP, "Everything rendered: %d lines, %d shots, the full audio take and "
                         "movie.edl.json.\n\nffmpeg wasn't found, so the .mp4 wasn't muxed. "
                         "Install ffmpeg and press Make the movie again - the rendered pieces "
                         "are re-used, so it only does the cut."
                         % (e.get("lines", 0), e.get("shots", 0)))

    def _no_server_dialog(self, name, what):
        """Explain a half-done regenerate in terms of the button that was pressed."""
        addr = "%s:%s" % (self.project["server"]["host"], self.project["server"]["port"])
        got = {"actor": "%s has been rewritten - who they are, how they look on paper "
                        "and how they sound." % name,
               "voice": "%s has a new voice written for them." % name}.get(
                   what, "Nothing happened.")
        self.set_status("Nothing drawn - ComfyUI is not answering on %s." % addr)
        messagebox.showwarning(
            APP, "%s\n\nNo picture was drawn, because ComfyUI is not answering on %s.\n\n"
                 "Start ComfyUI, then press Test connection on the Setup tab. If it is "
                 "already running it may be on a different port - Test connection will "
                 "find it. Then press New look on this actor to draw them." % (got, addr))

    def _cue_state(self, index, column, value, tag=None):
        iid = str(index)
        if self.cue_tree.exists(iid):
            self.cue_tree.set(iid, column, value)
            self.cue_tree.see(iid)
            if tag:
                self.cue_tree.item(iid, tags=(tag,))

    # ------------------------------------------------------------- utilities

    def _set_conn(self, comfy=None, model=None):
        cur = self.conn.get()
        c = comfy if comfy is not None else cur.split("|")[0].replace("ComfyUI:", "").strip()
        m = model if model is not None else cur.split("|")[-1].replace("model:", "").strip()
        self.conn.set("ComfyUI: %s   |   model: %s" % (c or "not checked", m or "not checked"))

    def set_status(self, msg):
        self.status.set(msg)

    def _log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", str(msg).rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _log_clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def show_help(self):
        messagebox.showinfo(APP, HELP_TEXT)

    def _on_close(self):
        if self.busy():
            if not messagebox.askyesno(APP, "A job is running. Quit anyway?"):
                return
            self.job.cancel()
        if self._confirm_discard():
            self.master.destroy()


class _ScriptJob(Worker):
    """Premise + approved cast -> screenplay."""

    kind = "script"

    def __init__(self, project, on_event):
        Worker.__init__(self, on_event)
        self.project = project

    def execute(self):
        # (flattened) from .pipeline import make_llm
        llm = make_llm(self.project)
        self.step("Writing the screenplay")
        text = casting.write_script(
            llm, self.project.get("premise", ""), proj.cast(self.project),
            scenes=int((self.project.get("options") or {}).get("scene_count", 4)),
            cancel=self.cancelled)
        self.result = {"script": text}


HELP_TEXT = """\
ScriptVoice makes a film offline: a local LLM casts it, ComfyUI draws and speaks it.

1. Premise - describe the film in a sentence or two and point the app at your local
   model server (LM Studio, Ollama, llama.cpp - press Detect). "Create the cast" then
   invents the characters, says in a sentence what each one does in the script,
   renders a portrait and a full 360 turnaround, and records a voice sample.

2. Cast - one card per actor, with what they do in the script and how many lines
   they have. Find in script highlights their lines on the Script tab, which is the
   quickest way to tell who MAN or INVESTOR actually is. Regenerate the actor (a different
   person, same role), regenerate the voice (same person, new voice), or ask for a new
   look. Drag the turnaround to spin it. Tick Approved when you're happy; the movie
   button unlocks once the whole cast is approved. Every character is locked to a fixed
   appearance and a fixed seed, so they stay the same person in every shot.

3. Script - write it yourself, or have the AI write it for the cast you approved.

4. Workflows - one ComfyUI API workflow per job: voice (required), portrait, turnaround
   and shot (optional). Export them from ComfyUI with Dev mode on: Workflow > Export (API).

5. Movie - renders every line through the voice workflow, a shot for each line through
   the picture workflow, and cuts them together with ffmpeg into movie.mp4. Anything
   already rendered is re-used, so changing one line only re-renders that line.
"""


# ---------------------------------------------------------------- small helpers

def _short(v, n=90):
    s = str(v).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def _int(v, default):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def _float(v, default):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def _coerce(s):
    s = str(s).strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _play(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        else:
            webbrowser.open("file://" + path)
    except Exception as e:
        messagebox.showerror(APP, "Couldn't play %s:\n%s" % (path, e))


def _ask_choice(parent, title, prompt, choices):
    """A small modal that returns one of `choices`, or "" if cancelled."""
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent.winfo_toplevel())
    win.resizable(False, False)
    frame = ttk.Frame(win, padding=14)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=prompt).pack(anchor="w")
    var = tk.StringVar(value=choices[0] if choices else "")
    box = ttk.Combobox(frame, textvariable=var, values=list(choices),
                       state="readonly", width=32)
    box.pack(anchor="w", pady=(8, 12))
    box.focus_set()
    out = {"value": ""}

    def take():
        out["value"] = var.get()
        win.destroy()

    row = ttk.Frame(frame)
    row.pack(anchor="e")
    ttk.Button(row, text="OK", command=take).pack(side="left")
    ttk.Button(row, text="Cancel", command=win.destroy).pack(side="left", padx=6)
    win.bind("<Return>", lambda e: take())
    win.bind("<Escape>", lambda e: win.destroy())
    win.grab_set()
    parent.wait_window(win)
    return out["value"]


def _ask_string(parent, title, prompt):
    from tkinter import simpledialog
    return simpledialog.askstring(title, prompt, parent=parent)


def fit_rows_to_font(root):
    """Make table rows tall enough for the font actually in use.

    Tk's default Treeview row height is a fixed 20 pixels, so on a high-DPI
    display the text of one row overlaps the next.
    """
    from tkinter import font as tkfont
    try:
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
    except tk.TclError:
        return
    ttk.Style().configure("Treeview", rowheight=max(20, int(line * 1.45)))


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform.startswith("win") else "clam")
    except tk.TclError:
        pass
    fit_rows_to_font(root)
    App(root)
    root.mainloop()


# --------------------------------------------------------------------------
# In the package these were separate modules; flattened into one file they all
# live in this namespace, so module-qualified references point back at it.
# --------------------------------------------------------------------------
import sys as _sys
_self = _sys.modules[__name__]
audio = runtime = speech = script_parser = llm = llm_mod = comfy = casting = project = proj = comfy_mod = jobs = worker = visuals = movie = movie_mod = render = pipeline = widgets = gui = _self

if __name__ == "__main__":
    main()
