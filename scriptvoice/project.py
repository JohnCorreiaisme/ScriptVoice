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

# The single-file build bakes the default workflow in here, so ScriptVoice.py
# can draw on its own with nothing beside it. Empty when running the package,
# which reads workflows/ off disk instead.
EMBEDDED_WORKFLOW_JSON = ""
BUILTIN = "(built in: SDXL Turbo)"


def builtin_workflow():
    """The default workflow as a dict: from disk if it is there, else baked in."""
    path = bundled_workflow()
    if path:
        try:
            return load_workflow(path)
        except Exception:
            pass
    if EMBEDDED_WORKFLOW_JSON:
        try:
            return json.loads(EMBEDDED_WORKFLOW_JSON)
        except ValueError:
            pass
    return None


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
    wf = builtin_workflow()
    if not wf:
        return []
    path = bundled_workflow() or BUILTIN
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
            # Words put at the very front of every picture prompt. PhotoMaker
            # and similar identity models require a trigger phrase there, and
            # it is also the place for a house style.
            "prompt_prefix": "",
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
    """Load a ComfyUI workflow saved in *API format* and validate it.

    The sentinel BUILTIN means the copy baked into the single file, which has
    no path on disk to read.
    """
    if path == BUILTIN:
        wf = json.loads(EMBEDDED_WORKFLOW_JSON or "{}")
        if not wf:
            raise ValueError("This build has no workflow baked into it.")
        return wf
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
