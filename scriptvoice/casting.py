"""The casting director: a local LLM turns a premise into a locked-down cast,
judges whether each actor fits the plot, and plans the shots for the movie.

Every actor record carries a fixed appearance description and a fixed seed, so
the same character looks and sounds the same in every image and every line.
"""

import random
import re

from . import script_parser
from .llm import LLMError

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
 "cast": ["NAMES of everyone visible in this shot, closest to camera first"],
 "subject": "NAME of the one character the camera is on",
 "setting": "where this happens - the heading's location",
 "wardrobe": {"NAME": "garments only"},
 "mood": "lighting and mood"}

"subject" is who we SEE, which is often not who is speaking. A reaction shot of
the listener during someone else's line is normal and good - just say whose face
is in frame. Use a name from the cast, exactly as spelled there.

"cast" is everyone visible, and it is usually just one person. Two or more means
a wider shot - say so in "shot", because a close-up cannot hold two faces. Put
whoever is nearest the camera first.
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


def shot_people(shot, cue, cast_names=()):
    """Everyone visible in this shot, nearest the camera first.

    The user's own list wins, then the planner's, then whoever the shot text
    names, and failing all of that the one person shot_subject settles on.
    """
    known = [str(n).strip().upper() for n in cast_names if str(n).strip()]
    for key in ("cast_override", "cast"):
        picked = [str(n).strip().upper() for n in ((shot or {}).get(key) or [])]
        picked = [n for n in picked if n in known]
        if picked:
            out = []
            for n in picked:                    # keep order, drop repeats
                if n not in out:
                    out.append(n)
            return out
    named, text = [], shot_text(shot)
    for n in sorted(known, key=len, reverse=True):
        m = re.search(r"\b%s\b" % re.escape(n), text, re.I) if text else None
        if m and not any(n in other for other in named):
            named.append((m.start(), n))
    if named:
        return [n for _, n in sorted(named)]
    one = shot_subject(shot, cue, cast_names)
    return [one] if one else []


def shot_subject(shot, cue, cast_names=()):
    """Whose face this shot is of. The speaker is only the last resort.

    Order: what the user chose, then what the model said, then the first cast
    member the shot description actually names, then the speaker.
    """
    names = [str(n).strip().upper() for n in cast_names if str(n).strip()]
    want = str((shot or {}).get("subject_override", "")).strip().upper()
    if want and want in names:
        return want                     # the user pinned this one
    for key in ("cast_override", "cast"):
        picked = [str(n).strip().upper() for n in ((shot or {}).get(key) or [])]
        picked = [n for n in picked if n in names]
        if picked:
            return picked[0]            # nearest the camera holds the face
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
                "cast": [str(x).strip().upper()
                         for x in (item.get("cast") or []) if str(x).strip()],
                "setting": str(item.get("setting", "")).strip(),
                # kept as-is: a dict keyed by name, or the sentence a smaller
                # model returns instead. wardrobe_for() sorts out which.
                "wardrobe": item.get("wardrobe", ""),
                "mood": str(item.get("mood", "")).strip(),
            }
    return shots


# ------------------------------------------------------------ prompt builders

def with_prefix(prompt, prefix=""):
    """Put the project's prefix at the front of a prompt, once."""
    prefix = str(prefix or "").strip().strip(",")
    if not prefix:
        return prompt
    if prompt.lower().startswith(prefix.lower()):
        return prompt
    return "%s, %s" % (prefix, prompt) if prompt else prefix


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


def shot_prompt(actor_map, cue, shot, style="cinematic film still, 35mm, natural lighting",
                prefix=""):
    """The prompt for one movie shot, with the speaker's fixed look folded in."""
    subject = shot_subject(shot, cue, actor_map.keys())
    actor = actor_map.get(subject) or {}
    people = shot_people(shot, cue, actor_map.keys())
    # The scene supplies the clothes when it has an opinion, so the character's
    # one fixed outfit does not follow them onto the lake.
    scene_dress = wardrobe_for(shot, subject, actor_map.keys())
    who = (look_prompt(actor, style="", wardrobe=not scene_dress)
           if actor else subject.title())
    # Anyone else in frame is described too, or the renderer invents them. Their
    # faces are not locked - only one identity can be - but a wide shot is where
    # extra people appear, and there a description is enough.
    others = []
    for name in people[1:3]:
        rec = actor_map.get(name)
        if not rec:
            continue
        bits = look_prompt(rec, style="", wardrobe=False)[:120]
        dress = wardrobe_for(shot, name, actor_map.keys()) or rec.get("wardrobe", "")
        others.append("with %s: %s%s" % (name.title(), bits,
                                         (", wearing " + dress) if dress else ""))
    # The scene heading comes from the script, so it outranks whatever the model
    # decided the setting was.
    where = scene_phrase(shot.get("scene", "")) or shot.get("setting", "")
    dress = scene_dress or actor.get("wardrobe", "")
    parts = [shot_text(shot) or "medium shot of %s speaking" % subject.title(),
             ("in " + where) if where else "",
             who, ("wearing " + dress) if dress else ""] + others + [
             shot.get("mood", ""), style]
    return with_prefix(", ".join(p.strip(" ,") for p in parts if p and p.strip()), prefix)


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
