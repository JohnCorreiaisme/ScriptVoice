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
