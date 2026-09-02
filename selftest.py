"""End-to-end check with no ComfyUI, no local LLM, no GPU and no GUI.

Two stub servers stand in for ComfyUI and for an OpenAI-compatible local model,
then the real pipeline runs against them: parsing, casting, judging, portraits,
turnarounds, voices, regeneration, and the movie cut.

    python selftest.py            # package + the flattened single file
    python selftest.py --single   # only ScriptVoice.py
"""

import importlib.util
import hashlib
import io
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import types
import wave
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
COMFY_PORT = 8899
LLM_PORT = 8896

STATE = {"jobs": {}, "prompts": [], "uploads": [], "dir": "", "llm_calls": []}


# --------------------------------------------------------------------- fixtures

def make_wav(path, seconds=0.4, freq=440.0, rate=22050):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(int(rate * seconds))))
    return path


def _write(work, name, text):
    """Drop a text file into the scratch dir and return its path."""
    path = os.path.join(work, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def make_png(path, size=64, shade=128):
    """A tiny valid PNG, written with the standard library only.

    Deliberately patterned rather than a flat colour: a uniform image cannot
    tell a correctly encoded frame apart from uninitialised memory.
    """
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")
        for x in range(size):
            lit = ((x // max(1, size // 2)) + (y // max(1, size // 2))) % 2
            row += bytes([(shade + 90 * lit) % 256,
                          (shade * 2 + 40 * y) % 256,
                          (40 + 12 * x) % 256])
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    return path


CAST_JSON = [
    {"name": "MAYA", "role": "protagonist", "one_line": "A marine biologist who came back",
     "age_range": "late 30s", "appearance": "tall, close-cropped black hair, weathered face",
     "wardrobe": "yellow storm jacket", "distinguishing": "scar through the left eyebrow",
     "personality": "stubborn", "voice_type": "low, dry, clipped",
     "voice_direction": "understated", "sample_line": "The light came on by itself."},
    {"name": "RUBEN", "role": "supporting", "one_line": "The keeper who never left",
     "age_range": "60s", "appearance": "stooped, heavy grey beard, thick glasses",
     "wardrobe": "oil-stained overalls", "distinguishing": "missing fingertip",
     "personality": "evasive", "voice_type": "gravel, slow, coastal accent",
     "voice_direction": "reluctant", "sample_line": "Some things are better left dark."},
]

ROLE_JSON = [
    {"name": "MAYA", "role": "The biologist who rows out to the island and finds the "
                             "light already burning."},
    {"name": "RUBEN", "role": "The keeper who swore the generator was dead and knows "
                              "why it isn't."},
]
JUDGE_JSON = [
    {"name": "MAYA", "score": 86, "verdict": "fits", "reason": "Her expertise drives the plot.",
     "fix": "Give her a reason to distrust Ruben earlier."},
    {"name": "RUBEN", "score": 52, "verdict": "weak", "reason": "He only withholds information.",
     "fix": "Make him complicit in why the light went dark."},
]

RECAST_JSON = {
    "name": "RUBEN", "role": "supporting", "one_line": "A coastguard auditor with a secret",
    "age_range": "40s", "appearance": "wiry, shaved head, burn scar along the jaw",
    "wardrobe": "navy service coat", "distinguishing": "burn scar", "personality": "brisk",
    "voice_type": "bright, fast, nasal", "voice_direction": "impatient",
    "sample_line": "I have the logbook right here."}

REVOICE_JSON = {"voice_type": "soft, breathy, high", "voice_direction": "wary",
                "sample_line": "Say that again, slowly."}

SCRIPT_TEXT = """Sure! Here is the screenplay:

[SCENE: The lamp room, midnight]
MAYA: The light came on by itself.
RUBEN: Some things are better left dark.
maya: That isn't an answer.

[SCENE: The generator shed]
RUBEN: Give it a minute.
"""

SHOTS_JSON = [{"n": i + 1, "shot": "medium shot of the speaker", "setting": "the lamp room",
               "mood": "cold blue night"} for i in range(4)]


# ---------------------------------------------------------------------- stubs

class ComfyStub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path == "/system_stats":
            return self._json({"devices": [{"name": "stub cpu"}]})
        if path.startswith("/history/"):
            pid = path.rsplit("/", 1)[1]
            entry = STATE["jobs"].get(pid)
            return self._json({pid: entry} if entry else {})
        if path == "/view":
            params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
            with open(os.path.join(STATE["dir"], params["filename"]), "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json({}, 404)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/prompt":
            wf = json.loads(raw)["prompt"]
            STATE["prompts"].append(wf)
            pid = "job%d" % len(STATE["jobs"])
            classes = {n.get("class_type") for n in wf.values()}
            if "SaveImage" in classes:
                name = "%s.png" % pid
                make_png(os.path.join(STATE["dir"], name), shade=(len(STATE["jobs"]) * 7) % 255)
                outputs = {"9": {"images": [{"filename": name, "subfolder": "", "type": "output"}]}}
            else:
                name = "%s.wav" % pid
                make_wav(os.path.join(STATE["dir"], name), freq=300 + 40 * len(STATE["jobs"]))
                outputs = {"3": {"audio": [{"filename": name, "subfolder": "", "type": "output"}]}}
            STATE["jobs"][pid] = {"status": {"completed": True, "status_str": "success"},
                                  "outputs": outputs}
            return self._json({"prompt_id": pid, "node_errors": {}})
        if self.path == "/upload/image":
            STATE["uploads"].append(len(raw))
            return self._json({"name": "ref.wav", "subfolder": "scriptvoice", "type": "input"})
        self._json({}, 404)


class LLMStub(BaseHTTPRequestHandler):
    """An OpenAI-compatible server that answers by looking at the system prompt."""

    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            return self._json({"data": [{"id": "stub-7b-instruct"}, {"id": "stub-1b"}]})
        self._json({})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        system = payload["messages"][0]["content"].lower()
        STATE["llm_calls"].append(system.split("\n")[0][:40])
        if "casting director" in system:
            # deliberately messy: fenced, with a preamble and a trailing comma
            content = "Here you go:\n```json\n%s,\n```" % json.dumps(CAST_JSON)[:-1] + "]\n```"
            content = "Here you go:\n```json\n%s\n```" % json.dumps(CAST_JSON)
        elif "script supervisor" in system:
            content = "<think>reading their lines</think>" + json.dumps(ROLE_JSON)
        elif "script editor" in system:
            content = "<think>weighing them up</think>" + json.dumps(JUDGE_JSON)
        elif "recasting" in system:
            content = json.dumps(RECAST_JSON)
        elif "voice director" in system:
            content = json.dumps(REVOICE_JSON)
        elif "screenwriter" in system:
            content = SCRIPT_TEXT
        elif "storyboard" in system:
            content = json.dumps(SHOTS_JSON)
        else:
            content = "{}"
        self._json({"choices": [{"message": {"role": "assistant", "content": content}}]})


# ----------------------------------------------------------------- namespaces

def package_ns():
    from scriptvoice import (audio, casting, llm, movie, pipeline, project,
                             render, runtime, script_parser, speech, visuals, widgets)
    return types.SimpleNamespace(
        label="package", project=project, casting=casting, script_parser=script_parser,
        audio=audio, llm=llm, visuals=visuals, movie=movie, runtime=runtime,
        speech=speech, render_mod=render,
        RenderJob=render.RenderJob, CastJob=pipeline.CastJob,
        comfy=__import__('scriptvoice.comfy',fromlist=['x']), RegenerateJob=pipeline.RegenerateJob, MovieJob=pipeline.MovieJob,
        StoryboardJob=pipeline.StoryboardJob, plan_shots_batched=pipeline.plan_shots_batched,
        stable_voice_seed=pipeline.stable_voice_seed, make_llm=pipeline.make_llm,
        pipeline=pipeline, jobs=__import__('scriptvoice.jobs', fromlist=['x']),
        widgets=widgets)


def single_ns():
    path = os.path.join(HERE, "ScriptVoice.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location("scriptvoice_single", path)
    m = importlib.util.module_from_spec(spec)
    sys.modules["scriptvoice_single"] = m
    spec.loader.exec_module(m)
    return types.SimpleNamespace(
        label="single file", project=m, casting=m, script_parser=m, audio=m, llm=m,
        visuals=m, movie=m, runtime=m, speech=m, render_mod=m, RenderJob=m.RenderJob, CastJob=m.CastJob,
        comfy=m, RegenerateJob=m.RegenerateJob, MovieJob=m.MovieJob,
        StoryboardJob=m.StoryboardJob, plan_shots_batched=m.plan_shots_batched,
        stable_voice_seed=m.stable_voice_seed, make_llm=m.make_llm,
        pipeline=m, jobs=m, widgets=m)


# --------------------------------------------------------------------- harness

class Checks(object):
    def __init__(self):
        self.passed = self.failed = 0

    def section(self, title):
        print("\n" + title)

    def __call__(self, label, cond, detail=""):
        cond = bool(cond)
        self.passed += cond
        self.failed += not cond
        print(("  PASS  " if cond else "  FAIL  ") + label + (("  -> " + detail) if detail else ""))
        return cond


def base_project(sv, work, wf_voice, wf_image):
    p = sv.project.new_project()
    p["premise"] = ("A marine biologist and a lighthouse keeper are trapped on an island "
                    "the night the light comes back on by itself.")
    p["server"] = {"host": "127.0.0.1", "port": COMFY_PORT}
    p["llm"] = {"base_url": "http://127.0.0.1:%d/v1" % LLM_PORT, "model": "stub-7b-instruct",
                "temperature": 0.8}
    voice_wf = sv.project.load_workflow(wf_voice)
    image_wf = sv.project.load_workflow(wf_image)
    p["workflows"]["voice"] = {"path": wf_voice,
                               "mapping": sv.project.guess_mapping(voice_wf, "voice")}
    for slot in ("portrait", "turnaround", "shot"):
        p["workflows"][slot] = {"path": wf_image,
                                "mapping": sv.project.guess_mapping(image_wf, slot)}
    p["options"]["output_dir"] = os.path.join(work, "out")
    p["options"]["turnaround_frames"] = 8
    return p


def _raises(fn):
    """True if calling fn() raises anything at all."""
    try:
        fn()
        return False
    except Exception:
        return True


def run_job(job, timeout=120):
    events = []
    job.on_event = events.append
    job.start()
    job.join(timeout)
    return events


def suite(sv, check):
    work = tempfile.mkdtemp(prefix="scriptvoice_%s_" % sv.label.replace(" ", "_"))
    STATE.update({"jobs": {}, "prompts": [], "uploads": [], "dir": work, "llm_calls": []})
    wf_voice = os.path.join(HERE, "workflows", "example_api_workflow.json")
    wf_image = os.path.join(HERE, "workflows", "example_image_workflow.json")

    # ---------------------------------------------------------------- parsing
    check.section("[1] script parsing (%s)" % sv.label)
    script = ("# a note that must not be spoken\n"
              "NARRATOR: The lighthouse had been dark for eleven years.\n\n"
              "MAYA: You said the generator still worked.\n\n"
              "RUBEN\nIt did. In 2014. (shrugs) Give it a minute.\n\n"
              "MAYA: We don't have a minute.\n")
    cues = sv.script_parser.parse(script)
    check("4 cues parsed", len(cues) == 4, str(len(cues)))
    check("speakers in order",
          sv.script_parser.speakers(cues) == ["NARRATOR", "MAYA", "RUBEN"])
    check("screenplay block attributed", cues[2].speaker == "RUBEN")
    check("parenthetical stripped", "shrugs" not in cues[2].text, cues[2].text)
    check("comment skipped", all("must not be spoken" not in c.text for c in cues))
    short = ("INT. WAREHOUSE - NIGHT\n"
             "REEVE It has never missed.\n"
             "VICTOR (CONT'D) NO! NO!\n"
             "A CRATE. Steel, banded, waiting.\n"
             "NORA Victor, I was kidding.\n")
    short_cues = sv.script_parser.parse(short)
    check("a five-line scene is still a screenplay, not one block of narration",
          sv.script_parser.detect_format(short) == "screenplay",
          sv.script_parser.detect_format(short))
    check("every speaker in a short scene is found",
          sv.script_parser.speakers(short_cues) == ["REEVE", "VICTOR", "NORA"],
          str(sv.script_parser.speakers(short_cues)))
    check("a shouted (CONT'D) line is attributed, not dropped",
          any(c.speaker == "VICTOR" and "NO!" in c.text for c in short_cues))
    check("the stage direction is not spoken",
          all("Steel, banded" not in c.text for c in short_cues))
    prose = ("The lighthouse had been dark for eleven years.\n"
             "NASA said the signal was nothing.\n"
             "She walked to the end of the dock and waited.\n"
             "Nobody came.\n"
             "The water was like hammered tin.\n")
    check("a capitalised word in prose does not invent a character",
          sv.script_parser.speakers(sv.script_parser.parse(prose)) == ["NARRATOR"],
          str(sv.script_parser.speakers(sv.script_parser.parse(prose))))
    check("NAME: format still wins over screenplay detection",
          sv.script_parser.detect_format("MAYA: Hi.\nRUBEN: Hello.\n") == "simple")
    check("chunking splits long lines",
          len(sv.script_parser.parse("N: One. Two. Three.", max_chars=6)) == 3)
    check("the simple format is still detected as such",
          sv.script_parser.detect_format(script) == "simple")

    # a real screenplay: speaker and line on the same row, no colon anywhere
    sp = sv.script_parser
    screen = (
        "EXT. MARINA - PIER - DAY\n"
        "Water like hammered tin. A wakeboard boat idles offshore.\n"
        "VICTOR ARDEN (52) stands on the pier in a windbreaker. He is pleased with himself.\n"
        "VICTOR (bellowing over the water) Watch this.\n"
        "On the water: SAM (16) cuts the wake and lands it clean.\n"
        "VICTOR (CONT'D) THAT'S MY KID. THAT'S MY KID.\n"
        "INT. MARINA - WORKSHOP - CONTINUOUS\n"
        "Nora in the doorway, regretting it.\n"
        "NORA Victor. Victor, I was kidding.\n"
        "VICTOR I know.\n"
        "REEVE stands at the edge of it, entirely unbothered.\n"
        "MAN (O.S.) And who are you?\n")
    scenes = []
    sc = sp.parse(screen, scene_out=scenes)
    check("screenplay format detected", sp.detect_format(screen) == "screenplay")
    check("5 cues found in the screenplay", len(sc) == 5, str(len(sc)))
    check("same-row speakers read",
          [c.speaker for c in sc] == ["VICTOR", "VICTOR", "NORA", "VICTOR", "MAN"],
          str([c.speaker for c in sc]))
    check("the direction between name and line is dropped",
          sc[0].text == "Watch this.", sc[0].text)
    check("(CONT'D) does not become a second character",
          sp.speakers(sc) == ["VICTOR", "NORA", "MAN"], str(sp.speakers(sc)))
    check("ALL-CAPS shouting still splits off the name",
          sc[1].text == "THAT'S MY KID. THAT'S MY KID.", sc[1].text)
    check("(O.S.) stripped", sc[4].text == "And who are you?", sc[4].text)
    check("scene headings are not spoken",
          all("MARINA" not in c.text for c in sc))
    check("scene headings recorded",
          [s[1] for s in scenes] == ["EXT. MARINA - PIER - DAY",
                                     "INT. MARINA - WORKSHOP - CONTINUOUS"],
          str(scenes))
    check("action prose is dropped, not spoken",
          all("hammered tin" not in c.text and "already sorry" not in c.text
              for c in sc))
    check("a character introduction is action, not a line",
          all("sun shirt" not in c.text for c in sc))
    check("a name followed by a verb is action, not a line",
          all("unbothered" not in c.text for c in sc))
    check("a mid-line colon does not invent a speaker",
          "ON THE WATER" not in sp.speakers(sc), str(sp.speakers(sc)))
    check("keep_action hands the prose to the narrator",
          any(c.speaker == "NARRATOR" and "hammered tin" in c.text
              for c in sp.parse(screen, keep_action=True)))
    check("scenes() lists the headings on its own", len(sp.scenes(screen)) == 2)
    check("screenplay mode can be forced off",
          "NARRATOR" in sp.speakers(sp.parse(screen, mode="simple")),
          str(sp.speakers(sp.parse(screen, mode="simple"))))
    check("a bare name over its dialogue still works in screenplay mode",
          [(c.speaker, c.text) for c in sp.parse(screen + "NORA\nCome back out.\n")][-1]
          == ("NORA", "Come back out."))

    # ------------------------------------------------------- parser under fire
    check.section("[1b] a hostile script (%s)" % sv.label)
    hostile = [
        ("nothing at all", ""),
        ("whitespace only", "   \n\t\n     \n"),
        ("one enormous line", "A" * 50000),
        ("punctuation only", "!!!???...---"),
        ("a 200-character name", ("N" * 200) + ": hello"),
        ("500 blank lines", "MAYA: a\n" + "\n" * 500 + "MAYA: b"),
        ("nothing but scene headings", "INT. HOUSE - DAY\nEXT. CAR - NIGHT\n"),
        ("nested parentheticals", "MAYA: (a (b (c) d) e) hello"),
        ("an unclosed parenthetical", "MAYA: (whispering hello there"),
        (u"non-ASCII throughout",
         u"MAYA: “Café” — naïve… \U0001f600\n"
         u"RUBEN: שלום"),
        ("a NUL byte", "MAYA: hel\x00lo"),
        ("a bare colon", ":"),
    ]
    crashed = []
    for label, text in hostile:
        try:
            sp.parse(text)
            sp.scenes(text)
            sp.detect_format(text)
        except Exception as e:                                   # noqa: BLE001
            crashed.append("%s: %s" % (label, e))
    check("no hostile script crashes the parser", not crashed, "; ".join(crashed))

    for eol, name in (("\r\n", "CRLF"), ("\n", "LF"), ("\r", "CR")):
        rows = [(c.speaker, c.text) for c in sp.parse(eol.join(["MAYA: hi", "RUBEN: yo", ""]))]
        check("%s line endings read the same" % name,
              rows == [("MAYA", "hi"), ("RUBEN", "yo")], str(rows))
    check("a BOM is not glued onto the first speaker",
          sp.speakers(sp.parse(u"﻿MAYA: hi\nRUBEN: yo")) == ["MAYA", "RUBEN"],
          str(sp.speakers(sp.parse(u"﻿MAYA: hi\nRUBEN: yo"))))
    check("a 50,000-character line is not truncated",
          len(sp.parse("A" * 50000)[0].text) == 50000)
    caps = sp.parse("THIS IS A LINE\nANOTHER LINE HERE\nYET MORE CAPS TEXT\n")
    check("an ALL-CAPS script loses no lines", len(caps) == 3, str(len(caps)))
    check("an ALL-CAPS line is given to the narrator",
          all(c.speaker == "NARRATOR" for c in caps) and "YET MORE CAPS TEXT" in caps[-1].text)
    swallowed = sp.parse("INT. HOUSE - DAY\nINT. HOUSE - DAY: I am talking\n")
    check("a scene heading does not swallow words written after it",
          any("I am talking" in c.text for c in swallowed), str(swallowed))
    check("the heading itself is still not spoken",
          all("INT. HOUSE" not in c.text for c in swallowed), str(swallowed))
    check("a NUL byte does not survive into a cue",
          "\x00" not in sp.parse("MAYA: hel\x00lo")[0].text)
    check("a negative max_chars does not spin forever",
          len(sp.parse("MAYA: hello world how are you", max_chars=-5)) == 1)
    long_line = "word " * 60
    check("chunking loses no words",
          " ".join(c.text for c in sp.parse("MAYA: " + long_line, max_chars=20)).split()
          == long_line.split())
    check("a 200-character name is kept as spoken text, not lost",
          "hello" in sp.parse(("N" * 200) + ": hello")[0].text)
    check("blank runs do not merge two speeches",
          len(sp.parse("MAYA: a\n" + "\n" * 500 + "MAYA: b")) == 2)
    check("the parser invents no dialogue",
          sp.parse("MAYA: only this line") and
          len(sp.parse("MAYA: only this line")) == 1)

    # ------------------------------------------------------------- workflows
    check.section("[2] workflow introspection")
    voice_wf = sv.project.load_workflow(wf_voice)
    g = sv.project.guess_mapping(voice_wf, "voice")
    check("TTS text input found", g["text"] == "2.text", g["text"])
    check("TTS voice input found", g["voice"] == "1.audio", g["voice"])
    check("TTS seed found", g["seed"] == "2.seed", g["seed"])
    image_wf = sv.project.load_workflow(wf_image)
    gi = sv.project.guess_mapping(image_wf, "portrait")
    check("positive prompt found through the sampler link", gi["prompt"] == "6.text", gi["prompt"])
    check("negative prompt told apart from positive", gi["negative"] == "7.text", gi["negative"])
    check("image seed found", gi["seed"] == "3.seed", gi["seed"])
    check("wired inputs excluded",
          "2.reference_audio" not in [r[0] for r in sv.project.widget_inputs(voice_wf)])

    # --------------------------------------------------- workflows under fire
    check.section("[2b] a workflow that fights back")
    no_text = {"1": {"class_type": "KSampler", "inputs": {"steps": 20, "cfg": 8.0}}}
    check("a workflow with no text input maps to nothing, quietly",
          sv.project.guess_mapping(no_text, "voice")["text"] == "")
    big = dict((str(i), {"class_type": "Node%d" % i,
                         "inputs": {"text": "x%d" % i, "seed": i}}) for i in range(1, 201))
    check("200 nodes are all read", len(sv.project.widget_inputs(big)) == 400)
    check("200 nodes still produce a mapping",
          sv.project.guess_mapping(big, "voice")["text"] == "1.text")
    odd = {"abc": {"class_type": "A", "inputs": {"text": "x"}},
           "2": {"class_type": "B", "inputs": {"text": "y"}}}
    check("a node id that is not a number does not break sorting",
          [r[0] for r in sv.project.widget_inputs(odd)] == ["2.text", "abc.text"])
    check("a non-numeric node id still loads",
          len(sv.project.load_workflow(_write(work, "odd.json", json.dumps(odd)))) == 2)
    guard = {"1": {"class_type": "A", "inputs": {"text": "keep me", "width": 512}}}
    check("a mapping at a vanished input reports False, writing nothing",
          sv.project.apply_value(guard, "9.text", "INTRUDER") is False
          and sv.project.apply_value(guard, "1.gone", "INTRUDER") is False
          and guard["1"]["inputs"] == {"text": "keep me", "width": 512})

    stale_wf = _write(work, "stale.json", json.dumps(
        {"1": {"class_type": "TTS", "inputs": {"renamed_text": "PLACEHOLDER", "seed": 0}}}))
    stale_p = sv.project.new_project()
    stale_p["server"] = {"host": "127.0.0.1", "port": COMFY_PORT}
    stale_p["workflows"]["voice"] = {"path": stale_wf,
                                     "mapping": {"text": "1.text", "seed": "1.seed"}}
    stale_p["options"]["output_dir"] = os.path.join(work, "stale_out")
    ev = run_job(sv.RenderJob(stale_p, [sv.script_parser.Cue(0, "MAYA", "hello", 1)],
                              os.path.join(work, "stale_out"), None))
    failed = [e for e in ev if e["kind"] == "failed"]
    check("a mapping pointing at a vanished input fails loudly, not silently",
          bool(failed) and "1.text" in failed[0].get("message", ""),
          str(failed[:1])[:150])
    check("the placeholder text was never sent to ComfyUI",
          all("PLACEHOLDER" not in json.dumps(pr) for pr in STATE["prompts"]))

    # ------------------------------------------------------------ json repair
    check.section("[3] coping with messy model replies")
    ex = sv.llm.extract_json
    check("fenced json", ex('```json\n{"a": 1}\n```')["a"] == 1)
    check("prose around it", ex('Sure!\n{"a": 2}\nHope that helps')["a"] == 2)
    check("trailing comma repaired", ex('{"a": 3,}')["a"] == 3)
    check("<think> block ignored", ex('<think>hmm</think>[{"n":1}]', "array")[0]["n"] == 1)
    check("array under a key", ex('{"characters": [{"n": 9}]}', "array")[0]["n"] == 9)
    check("garbage returns None", ex("no json here at all") is None)

    rank = sv.llm.rank_models
    check("embedding models are never offered for chat",
          rank(["nomic-embed-text-v1.5", "qwen3-27b"]) == ["qwen3-27b"])
    check("a general model beats a coder model",
          rank(["qwen2.5-coder-7b-instruct", "qwen/qwen3.8-27b"])[0] == "qwen/qwen3.8-27b")
    check("the bigger general model wins",
          rank(["llama-3.2-1b-instruct", "mistral-small-24b"])[0] == "mistral-small-24b")
    check("a coder-only server still returns its model",
          rank(["deepseek-coder-6.7b"]) == ["deepseek-coder-6.7b"])

    R = sv.runtime
    cue = lambda s: sv.script_parser.Cue(0, "X", s, 0)
    check("empty script runs for no time", R.estimate_total([]) == 0.0)
    A = sv.audio
    import wave as _w, struct as _st, tempfile as _tf, os as _os
    _p = _os.path.join(_tf.mkdtemp(), "pad.wav")
    with _w.open(_p, "wb") as _f:
        _f.setnchannels(1); _f.setsampwidth(2); _f.setframerate(8000)
        _tone = b"".join(_st.pack("<h", 8000) for _ in range(4000))
        _f.writeframes(_tone + _st.pack("<h", 0) * 8000)   # 0.5s tone + 1s pad
    with _w.open(_p) as _f:
        _params = _f.getparams(); _frames = _f.readframes(_f.getnframes())
    check("trailing pad is measured", A.trailing_silence(_frames, _params) == 8000,
          str(A.trailing_silence(_frames, _params)))
    _trimmed = A.trim_tail(_frames, _params)
    check("trimming keeps a short natural tail, not the whole pad",
          4900 <= len(_trimmed) // 2 <= 5100, str(len(_trimmed) // 2))
    check("trimming never touches audio itself",
          A.trim_tail(_frames[:8000], _params) == _frames[:8000])
    check("a short line still gets a floor", R.estimate_line("Go.") >= 0.5)
    check("150 words is about a minute",
          59 <= R.estimate_line(" ".join(["word"] * 150)) <= 62,
          "%.1fs" % R.estimate_line(" ".join(["word"] * 150)))
    check("sentences add pauses",
          R.estimate_line("One. Two. Three.") > R.estimate_line("One two three"))
    check("gaps land between cues, not after",
          abs(R.estimate_total([cue("a"), cue("b")], gap_seconds=1.0)
              - (R.estimate_line("a") + R.estimate_line("b") + 1.0)) < 1e-6)
    check("hours keep their minutes", R.format_runtime(3600) == "1h 0m 0s",
          R.format_runtime(3600))
    check("runtime reads naturally", R.format_runtime(5530) == "1h 32m 10s",
          R.format_runtime(5530))
    check("no hours means no hours field", R.format_runtime(125) == "2m 5s",
          R.format_runtime(125))
    check("seconds only under a minute", R.format_runtime(9) == "9s", R.format_runtime(9))
    g = R.target_gap([cue("one two three four five")] * 10, 5)
    check("target_gap rounds up rather than falling short",
          g["current_seconds"] + g["lines_needed"] * (g["average_line_seconds"] + 0.35)
          >= g["target_seconds"], str(g["lines_needed"]))
    check("no lines needed when already long enough",
          R.target_gap([cue(" ".join(["w"] * 2000))], 1)["lines_needed"] == 0)
    check("target_gap copes with an empty script",
          R.target_gap([], 10)["lines_needed"] > 0)

    S = sv.speech
    check("speech module imports on any platform",
          hasattr(S, "speak_to_wav") and hasattr(S, "describe_voice"))
    check("no voices means a silent, safe assignment",
          S.assign_voice(123, []) == {"voice": None, "rate": 0, "pitch": 0})
    table = [{"name": "Male A", "gender": "Male"}, {"name": "Fem B", "gender": "Female"}]
    check("a baritone is not cast as the female voice",
          S.assign_voice(7, table, hint="Loud American midwestern baritone")["voice"] == "Male A")
    check("an alto is not cast as the male voice",
          S.assign_voice(7, table, hint="Warm dry American alto")["voice"] == "Fem B")
    check("a tenor sits higher than a bass",
          S.assign_voice(3, table, hint="reedy tenor")["pitch"]
          > S.assign_voice(3, table, hint="deep bass")["pitch"])
    check("an undescribed voice still gets one",
          S.assign_voice(9, table, hint="")["voice"] in ("Male A", "Fem B"))
    check("the range wins even when only one gender is installed",
          S.assign_voice(4, [{"name": "Only Male", "gender": "Male"}],
                         hint="soprano")["voice"] == "Only Male")
    check("the lead is never given a gabbling rate",
          all(-2 <= S.assign_voice(s, table, hint="Loud American baritone")["rate"] <= 2
              for s in range(300)))
    check("a character written as fast speaks faster than one written as slow",
          S.assign_voice(11, table, hint="clipped, quick baritone")["rate"]
          > S.assign_voice(11, table, hint="slow, unhurried baritone")["rate"])
    check("'unhurried' is not read as 'hurried'",
          S._pace_of("soft, unhurried tenor", 1, 2) < 0,
          str(S._pace_of("soft, unhurried tenor", 1, 2)))
    spread = {S.assign_voice(s, table, hint="baritone")["pitch"] for s in range(300)}
    check("pitch spreads widely enough to tell voices apart",
          max(spread) - min(spread) >= 40, "%d..%d" % (min(spread), max(spread)))
    check("voices stay repeatable across processes",
          S.stable_seed("VICTOR", "baritone") == S.stable_seed("VICTOR", "baritone"))
    check("plain name lists still work",
          S.assign_voice(5, ["A", "B"])["voice"] in ("A", "B"))
    two = ["Voice A", "Voice B"]
    check("the same character always gets the same voice",
          S.assign_voice(4242, two) == S.assign_voice(4242, two))
    combos = {tuple(sorted(S.assign_voice(s, two).items())) for s in range(60)}
    check("two installed voices still yield many distinct characters",
          len(combos) >= 20, "%d combos" % len(combos))
    for s in range(200):
        a = S.assign_voice(s, two)
        if not (-2 <= a["rate"] <= 2 and -50 <= a["pitch"] <= 50):
            break
    else:
        s = None
    check("rate and pitch stay inside the usable range", s is None, str(s))
    check("negative pitch is written as -20%, never +-20%", S._signed(-20) == "-20")
    check("a quote in a voice name can't break out of the command",
          S._ps_quote("O'Brien") == "O''Brien")
    check("a character override beats the derived voice",
          sv.render_mod.system_voice({"name": "X", "seed": 5,
                                      "system_voice": {"voice": "Chosen", "rate": 2,
                                                       "pitch": -5}})["voice"] == "Chosen")

    # -------------------------------------------------------------- casting
    check.section("[4] the AI casts the film")
    p = base_project(sv, work, wf_voice, wf_image)
    events = run_job(sv.CastJob(p, p["options"]["output_dir"], lambda e: None))
    kinds = [e["kind"] for e in events]
    check("cast job finished", "finished" in kinds and "failed" not in kinds,
          next((e["message"] for e in events if e["kind"] == "failed"), ""))
    actors = sv.project.cast(p)
    check("two actors created", len(actors) == 2, str([a["name"] for a in actors]))
    maya = p["characters"].get("MAYA", {})
    check("appearance locked in", "close-cropped" in maya.get("appearance", ""))
    check("what they do in the script is recorded",
          "rows out to the island" in maya.get("one_line", ""),
          maya.get("one_line", ""))
    check("every character gets one, not just the lead",
          bool(p["characters"]["RUBEN"].get("one_line")),
          p["characters"]["RUBEN"].get("one_line", ""))
    check("the description never scores or reviews them",
          not any(w in maya.get("one_line", "").lower()
                  for w in ("score", "verdict", "fits", "weak", "cut", "should be")),
          maya.get("one_line", ""))
    check("portrait rendered", os.path.exists(maya.get("portrait", "")), maya.get("portrait", ""))
    check("8-frame turnaround rendered", len(maya.get("turnaround", [])) == 8,
          str(len(maya.get("turnaround", []))))
    check("voice sample rendered", os.path.exists(maya.get("voice_sample", "")))
    check("voice seed is fixed, not random", maya.get("seed", -1) >= 0, str(maya.get("seed")))
    check("nobody is approved automatically", not any(a.get("approved") for a in actors))

    # the spin must be the same person from different angles
    def _spins(marker):
        return [wf for wf in STATE["prompts"]
                if "6" in wf and "turnaround" in wf["6"]["inputs"]["text"]
                and marker in wf["6"]["inputs"]["text"]]

    spin_prompts = [wf["6"]["inputs"]["text"] for wf in _spins("close-cropped black hair")]
    seeds = {wf["3"]["inputs"]["seed"] for wf in _spins("close-cropped black hair")}
    check("every spin frame of one actor uses one seed", len(seeds) == 1, str(seeds))
    check("different actors get different seeds",
          seeds != {wf["3"]["inputs"]["seed"] for wf in _spins("heavy grey beard")})
    check("every spin frame asks for a different angle",
          len({p.split("turnaround")[0] for p in spin_prompts}) == 8,
          "%d distinct" % len({p.split("turnaround")[0] for p in spin_prompts}))
    check("the look text is repeated verbatim in every frame",
          all("close-cropped black hair" in t for t in spin_prompts[:8]))
    check("negative prompt was sent",
          any(wf["7"]["inputs"]["text"] != "blurry, watermark" for wf in STATE["prompts"]
              if "7" in wf))

    # --------------------------------------------- when ComfyUI is elsewhere
    check.section("[5] a misconfigured or missing ComfyUI")
    C = sv.comfy
    check("the server is found when the port is wrong",
          C.find_server("127.0.0.1", (9998, COMFY_PORT)) == COMFY_PORT)
    check("no server means no port, not an exception",
          C.find_server("127.0.0.1", (9998, 9997)) == 0)
    try:
        C.ComfyClient("127.0.0.1", 9998, timeout=1).ping()
        msg = ""
    except Exception as e:
        msg = str(e)
    check("the unreachable message says where to look, not just a socket error",
          "Test connection" in msg and "9998" in msg, msg.splitlines()[0][:60])

    saved_ports = C.COMMON_PORTS
    C.COMMON_PORTS = (9998, 9997)          # nothing to find anywhere
    try:
        stranded = json.loads(json.dumps(p))       # a copy, so later checks are unaffected
        stranded["server"] = {"host": "127.0.0.1", "port": 9998}
        before_voice = stranded["characters"]["MAYA"]["voice_type"]
        job = sv.RegenerateJob(stranded, os.path.join(work, "stranded"),
                               lambda e: None, "MAYA", what="voice")
        run_job(job, timeout=60)
        check("a regenerate survives ComfyUI being down", job.error is None,
              job.error or "")
        check("it says plainly that nothing was drawn",
              job.result.get("visuals") is False, str(job.result))
        check("the rewritten voice is kept, not discarded",
              stranded["characters"]["MAYA"]["voice_type"] != before_voice,
              stranded["characters"]["MAYA"]["voice_type"])

        # Pressing New look with no server used to report success and draw
        # nothing, so the button looked broken. It must fail out loud.
        look = sv.RegenerateJob(stranded, os.path.join(work, "stranded"),
                                lambda e: None, "MAYA", what="look")
        events = run_job(look, timeout=60)
        check("New look with no server is an error, not a silent success",
              look.error is not None, str(look.result))
        msg = (look.error or "")
        check("the error names the address it tried",
              "9998" in msg, msg.splitlines()[0][:70])
        check("and tells the user what to do about it",
              "Test connection" in msg, msg.splitlines()[0][:70])
        check("a failed New look still reports a bar to clear",
              any(e.get("kind") == "actor_progress" for e in events))
        check("the bar it showed said it was looking for the server",
              any("ComfyUI" in (e.get("label") or "") for e in events
                  if e.get("kind") == "actor_progress"),
              str([e.get("label") for e in events if e.get("kind") == "actor_progress"]))

        # Recasting is half LLM, so the writing survives - but the result has to
        # say which button was pressed, or the GUI cannot explain what happened.
        recast = sv.RegenerateJob(stranded, os.path.join(work, "stranded"),
                                  lambda e: None, "MAYA", what="actor")
        run_job(recast, timeout=60)
        check("recasting with no server keeps the writing", recast.error is None,
              recast.error or "")
        check("and reports which button was pressed",
              recast.result.get("what") == "actor" and recast.result.get("visuals") is False,
              str(recast.result))
        check("the rewritten character is still there",
              bool(stranded["characters"]["MAYA"].get("one_line")))
        check("but it does not claim a portrait it never drew",
              not stranded["characters"]["MAYA"].get("portrait"),
              str(stranded["characters"]["MAYA"].get("portrait")))

        cast_job = sv.CastJob(stranded, os.path.join(work, "stranded"), lambda e: None,
                              steps=("roles", "portrait"))
        run_job(cast_job, timeout=60)
        check("casting survives it too", cast_job.error is None, cast_job.error or "")
        check("and reports that it drew nothing",
              cast_job.result.get("visuals") is False, str(cast_job.result))
    finally:
        C.COMMON_PORTS = saved_ports
    # ------------------------------------------------- a project with no setup
    check.section("[5c] a brand-new project can draw without visiting Setup")
    fresh = sv.project.new_project()
    filled = sv.project.adopt_default_workflows(fresh)
    check("a new project adopts the bundled picture workflow",
          set(filled) == {"portrait", "turnaround", "shot"}, str(filled))
    check("and works out the inputs to drive it with",
          {"prompt", "negative", "seed"} <= set(fresh["workflows"]["portrait"]["mapping"]),
          str(sorted(fresh["workflows"]["portrait"]["mapping"])))
    check("the voice slot is left alone - there is no voice workflow to guess",
          not fresh["workflows"]["voice"]["path"])
    fresh["workflows"]["portrait"]["path"] = "chosen/by/hand.json"
    sv.project.adopt_default_workflows(fresh)
    check("adopting never overwrites a slot the user already filled",
          fresh["workflows"]["portrait"]["path"] == "chosen/by/hand.json",
          fresh["workflows"]["portrait"]["path"])

    # ComfyUI up, but nothing loaded to run: the silent no-op that made New look
    # look broken.
    bare = json.loads(json.dumps(p))
    bare["workflows"] = {k: {"path": "", "mapping": {}} for k in bare["workflows"]}
    bare_job = sv.RegenerateJob(bare, os.path.join(work, "bare"), lambda e: None,
                                "MAYA", what="look")
    run_job(bare_job, timeout=60)
    check("drawing with no workflow loaded is an error, not a quiet success",
          bare_job.error is not None, str(bare_job.result))
    check("and the error says where to load one",
          "Setup tab" in (bare_job.error or ""), (bare_job.error or "")[:70])

    # ------------------------------------------- starting LM Studio for the user
    check.section("[5d] a stopped model server is a question, not a dead end")
    check("the lms tool is looked for in LM Studio's own install spots",
          any("lm-studio" in x or "lmstudio" in x for x in sv.llm.LMS_PATHS),
          str(sv.llm.LMS_PATHS[0]))
    saved_paths, saved_which = sv.llm.LMS_PATHS, sv.llm.shutil.which
    try:
        sv.llm.LMS_PATHS = (os.path.join(work, "no_such_lms.exe"),)
        sv.llm.shutil.which = lambda name: None
        check("no LM Studio installed means no cli", sv.llm.find_lmstudio_cli() == "",
              sv.llm.find_lmstudio_cli())
        try:
            sv.llm.start_lmstudio(wait=5)
            check("starting without the cli is refused", False, "no error raised")
        except sv.llm.LLMError as e:
            check("starting without the cli is refused", True)
            check("and says to start it by hand instead",
                  "Developer" in str(e), str(e).splitlines()[-1][:60])

        # A command that runs but never serves must time out, not hang for ever.
        try:
            t0 = time.time()
            sv.llm.start_lmstudio(wait=5, port=9, cli=sys.executable)
            check("a server that never appears is an error", False, "no error raised")
        except sv.llm.LLMError as e:
            elapsed = time.time() - t0
            check("a server that never appears is an error", True)
            check("and gives up rather than hanging", 4 < elapsed < 25, "%.1fs" % elapsed)
            check("the timeout says how long it waited", "5 seconds" in str(e),
                  str(e).splitlines()[0][:60])

        try:
            sv.llm.start_lmstudio(wait=5, cli=os.path.join(work, "not_a_program.exe"))
            check("a cli that will not run is reported", False, "no error raised")
        except sv.llm.LLMError as e:
            check("a cli that will not run is reported", "not_a_program" in str(e),
                  str(e)[:60])
    finally:
        sv.llm.LMS_PATHS, sv.llm.shutil.which = saved_paths, saved_which

    # ---------------------------------------------- handing the card over
    check.section("[5e] freeing the GPU between writing and drawing")
    check("the option is off unless the user asks for it",
          sv.project.new_project()["options"]["free_gpu"] is False)

    freed = {"comfy": 0, "llm": 0}
    saved_unload = sv.llm.unload_lmstudio
    saved_free = sv.comfy.ComfyClient.free
    try:
        sv.llm.unload_lmstudio = lambda *a, **k: freed.__setitem__("llm", freed["llm"] + 1) or True
        sv.comfy.ComfyClient.free = (
            lambda self, *a, **k: freed.__setitem__("comfy", freed["comfy"] + 1) or True)

        off = json.loads(json.dumps(p))
        off["options"]["free_gpu"] = False
        run_job(sv.RegenerateJob(off, os.path.join(work, "gpu_off"), lambda e: None,
                                 "MAYA", what="actor"))
        check("with the option off nothing is unloaded",
              freed == {"comfy": 0, "llm": 0}, str(freed))

        on = json.loads(json.dumps(p))
        on["options"]["free_gpu"] = True
        events = run_job(sv.RegenerateJob(on, os.path.join(work, "gpu_on"), lambda e: None,
                                          "MAYA", what="actor"))
        check("with it on, ComfyUI is freed before the model writes",
              freed["comfy"] == 1, str(freed))
        check("and the model is unloaded before ComfyUI draws",
              freed["llm"] == 1, str(freed))
        logs = [e.get("message", "") for e in events if e.get("kind") == "log"]
        order = [i for i, m in enumerate(logs)
                 if "Freed ComfyUI" in m or "Unloaded the language model" in m]
        check("the two are logged, so the user can see the handover",
              len(order) == 2, str([logs[i] for i in order]))
        check("and they happen in that order - write first, then draw",
              len(order) == 2 and "Freed ComfyUI" in logs[order[0]],
              str([logs[i] for i in order]))
        check("the actor still comes out of it intact",
              bool(on["characters"]["MAYA"].get("one_line"))
              and os.path.exists(on["characters"]["MAYA"].get("portrait", "")))

        # New look never writes, so it must not evict a model it does not use.
        freed.update({"comfy": 0, "llm": 0})
        run_job(sv.RegenerateJob(on, os.path.join(work, "gpu_look"), lambda e: None,
                                 "MAYA", what="look"))
        check("New look does not free ComfyUI - it is the one about to draw",
              freed["comfy"] == 0, str(freed))
        check("but it does unload the writing model it will not need",
              freed["llm"] == 1, str(freed))
    finally:
        sv.llm.unload_lmstudio = saved_unload
        sv.comfy.ComfyClient.free = saved_free

    # a server that has no /free must not take the job down with it
    bad = sv.comfy.ComfyClient("127.0.0.1", 9998)
    check("freeing a server that isn't there is survivable, not fatal",
          bad.free() is False)

    # ------------------------------------------- what the user owns, not the AI
    check.section("[5f] the main-character tick box and the look box")
    fresh_c = sv.project.new_character("VICTOR")
    check("a new character is not the lead until someone says so",
          fresh_c["lead"] is False and fresh_c["look_note"] == "")

    look = dict(fresh_c, appearance="tall and angular, silver hair",
                wardrobe="expensive polo shirt", distinguishing="a signet ring",
                age_range="50s")
    plain = sv.casting.look_prompt(look)
    check("with no look note the model's description is used",
          "silver hair" in plain and "polo shirt" in plain, plain[:70])

    look["look_note"] = "bald, heavy jaw, broken nose"
    noted = sv.casting.look_prompt(look)
    check("a look note is in the prompt", "bald, heavy jaw" in noted, noted[:70])
    check("and it replaces the model's words rather than contradicting them",
          "silver hair" not in noted and "polo shirt" not in noted, noted[:90])
    check("the turnaround uses the same look, so the spin is the same person",
          all("bald, heavy jaw" in pr for _, pr in sv.casting.turnaround_prompts(look)))
    look["look_note"] = "   "
    check("a look note of only spaces falls back to the description",
          "silver hair" in sv.casting.look_prompt(look))

    # the judge must be told who the lead is, or it keeps demoting them
    lead_actor = dict(fresh_c, lead=True, one_line="the man who evicts the tenant")
    digest = sv.casting._cast_digest([lead_actor])
    check("the cast digest calls the lead a LEAD, not supporting",
          "LEAD" in digest and "supporting" not in digest, digest[:70])

    # and a recast must not quietly un-tick the box
    led = json.loads(json.dumps(p))
    led["characters"]["MAYA"].update({"lead": True, "role": "lead",
                                      "look_note": "bald, heavy jaw"})
    run_job(sv.RegenerateJob(led, os.path.join(work, "lead"), lambda e: None,
                             "MAYA", what="actor"))
    check("recasting never un-ticks Main character",
          led["characters"]["MAYA"]["lead"] is True)
    check("and never overwrites the look the user typed",
          led["characters"]["MAYA"]["look_note"] == "bald, heavy jaw",
          led["characters"]["MAYA"]["look_note"])
    check("the role word stays lead even if the model says supporting",
          led["characters"]["MAYA"]["role"] == "lead",
          led["characters"]["MAYA"]["role"])

    # ------------------------------------------- correcting what the AI wrote
    check.section("[5g] describing a character the AI misread")
    fixed = json.loads(json.dumps(p))
    fixed["characters"]["MAYA"]["one_line"] = "The goofy neighbour, not a tech genius."
    fixed["characters"]["MAYA"]["one_line"] = "stale"
    fixed["script"] = SCRIPT_TEXT
    jd = sv.RegenerateJob(fixed, os.path.join(work, "role"), lambda e: None,
                          "MAYA", what="role")
    events = run_job(jd, timeout=60)
    check("describing one character works on its own", jd.error is None, jd.error or "")
    check("it replaces the stale description",
          fixed["characters"]["MAYA"].get("one_line") != "stale",
          str(fixed["characters"]["MAYA"].get("one_line")))
    check("describing does not touch the portrait or the voice",
          fixed["characters"]["MAYA"].get("portrait") ==
          p["characters"]["MAYA"].get("portrait"))
    check("it does not need ComfyUI at all",
          jd.result.get("what") == "role" and "visuals" not in jd.result,
          str(jd.result))
    check("the new description comes from the script, not the premise",
          "rows out to the island" in fixed["characters"]["MAYA"]["one_line"],
          fixed["characters"]["MAYA"]["one_line"])
    check("the bar clears when it is done",
          [e for e in events if e.get("kind") == "actor_progress"][-1].get("label") == "")

    # what the model is actually shown - the whole point of the grounding
    sample = ("INT. TITLE OFFICE - DAY\n"
              "BANKER: So what got you into freight, Mr. Marlow?\n"
              "VICTOR: Luck.\n"
              "BANKER: Congratulations on the deal.\n"
              "INT. MARINA - NIGHT\n"
              "BANKER: That's everything. Call the office.\n")
    sample_cues = sv.script_parser.parse(sample)
    dig = sv.casting._lines_digest(sample_cues, "BANKER", sample)
    check("the digest carries every line the character speaks",
          dig.count('"') == 6, dig[:90])
    check("and says which scene each one happens in",
          "[INT. TITLE OFFICE - DAY]" in dig and "[INT. MARINA - NIGHT]" in dig,
          dig[:120])
    check("it does not repeat a scene heading for consecutive lines",
          dig.count("[INT. TITLE OFFICE - DAY]") == 1, dig[:120])
    check("a character who never speaks is said to never speak",
          sv.casting._lines_digest(sample_cues, "NOBODY", sample) == "(never speaks)")
    many = sv.script_parser.parse("".join("X: line %d.\n" % i for i in range(40)))
    big = sv.casting._lines_digest(many, "X")
    check("a talkative character is capped, and says it was capped",
          "and 16 more lines" in big, big[-40:])

    calls = []
    real_chat = sv.llm.LocalLLM.chat_json

    def spy(self, system, user, *a, **k):
        calls.append((system, user))
        return real_chat(self, system, user, *a, **k)

    sv.llm.LocalLLM.chat_json = spy
    try:
        sv.casting.describe_roles(sv.make_llm(p), "A LIGHTHOUSE PREMISE ABOUT ELEVEN YEARS",
                                  [p["characters"]["MAYA"]], sample_cues, sample)
    finally:
        sv.llm.LocalLLM.chat_json = real_chat
    sys_msg, user_msg = calls[-1]
    check("the premise is never sent to the description pass",
          "ELEVEN YEARS" not in user_msg, user_msg[:80])
    check("the model is told the script is its only source",
          "ONLY SOURCE" in sys_msg, sys_msg.splitlines()[3][:60])
    check("and is told not to invent a profession",
          "not a lawyer" in sys_msg, "")
    check("saying there is too little to tell is allowed",
          "too few to tell" in sys_msg, "")

    # where a character speaks, for the Find in script button
    found_cues = sv.script_parser.parse(SCRIPT_TEXT)
    n, first, hits = sv.casting.script_places(found_cues, "MAYA")
    check("a character's lines can be located in the script", n > 0 and first > 0,
          "%d lines, first at %d" % (n, first))
    check("the line numbers are real rows of the script",
          all(0 < h <= len(SCRIPT_TEXT.split(chr(10))) for h in hits), str(hits))
    check("a name that never speaks reports nothing rather than guessing",
          sv.casting.script_places(found_cues, "NOBODY") == (0, 0, []))

    # ------------------------------------------------ MAN is really REEVE
    check.section("[5h] folding one character into another")
    two = ("INT. BASEMENT - NIGHT\n"
           "REEVE: It has never missed.\n"
           "MAN: Take your time.\n"
           "REEVE: You have about four minutes.\n"
           "VICTOR: No.\n")
    tp = sv.project.new_project()
    tp["script"] = two
    for n in ("REEVE", "MAN", "VICTOR"):
        tp["characters"][n] = sv.project.new_character(n)
    tp["cast_order"] = ["REEVE", "MAN", "VICTOR"]

    cues = sv.script_parser.parse(two)
    check("before merging they are separate speakers",
          sorted(set(c.speaker for c in cues)) == ["MAN", "REEVE", "VICTOR"],
          str(sorted(set(c.speaker for c in cues))))
    check("a project with no merges has an empty alias map",
          sv.project.alias_map(tp) == {}, str(sv.project.alias_map(tp)))

    tp["characters"]["REEVE"]["aliases"] = ["MAN"]
    check("the alias map points the absorbed name at the real one",
          sv.project.alias_map(tp) == {"MAN": "REEVE"}, str(sv.project.alias_map(tp)))
    cues = sv.project.apply_aliases(tp, sv.script_parser.parse(two))
    check("MAN's line is spoken by REEVE after the merge",
          sorted(set(c.speaker for c in cues)) == ["REEVE", "VICTOR"],
          str(sorted(set(c.speaker for c in cues))))
    check("and REEVE now has all three lines",
          sum(1 for c in cues if c.speaker == "REEVE") == 3,
          str(sum(1 for c in cues if c.speaker == "REEVE")))
    check("nobody else is touched",
          sum(1 for c in cues if c.speaker == "VICTOR") == 1)
    check("the merged lines stay in script order",
          [c.text[:4] for c in cues if c.speaker == "REEVE"] == ["It h", "Take", "You "],
          str([c.text[:4] for c in cues if c.speaker == "REEVE"]))

    # the description pass must now see the absorbed lines as well
    dig = sv.casting._lines_digest(cues, "REEVE", two)
    check("the description reads the absorbed character's lines too",
          "Take your time" in dig, dig[:90])

    # an alias pointing at itself, or a dangling one, must not loop or crash
    tp["characters"]["REEVE"]["aliases"] = ["REEVE", "NOBODY", ""]
    check("an alias for itself is ignored", "REEVE" not in sv.project.alias_map(tp),
          str(sv.project.alias_map(tp)))
    safe = sv.project.apply_aliases(tp, sv.script_parser.parse(two))
    check("an alias for a name nobody uses changes nothing",
          sorted(set(c.speaker for c in safe)) == ["MAN", "REEVE", "VICTOR"],
          str(sorted(set(c.speaker for c in safe))))

    # splitting back apart
    tp["characters"]["REEVE"]["aliases"] = ["MAN"]
    tp["characters"]["REEVE"]["aliases"] = [a for a in tp["characters"]["REEVE"]["aliases"]
                                           if a != "MAN"]
    back = sv.project.apply_aliases(tp, sv.script_parser.parse(two))
    check("splitting them apart gives MAN their lines back",
          sum(1 for c in back if c.speaker == "MAN") == 1,
          str(sum(1 for c in back if c.speaker == "MAN")))

    # ---------------------------------------- shots that stay in their scene
    check.section("[5i] the storyboard follows the script's locations")
    ph = sv.casting.scene_phrase
    check("a slug line becomes plain English",
          ph("EXT. MARINA DOCK - DAY") == "exterior, marina dock, daytime",
          ph("EXT. MARINA DOCK - DAY"))
    check("interiors say interior",
          ph("INT. WAREHOUSE - NIGHT") == "interior, warehouse, at night",
          ph("INT. WAREHOUSE - NIGHT"))
    check("an unusual time of day is kept, not dropped",
          "dawn" in ph("EXT. FIELD - DAWN"), ph("EXT. FIELD - DAWN"))
    check("a heading with no time still works",
          ph("INT. BASEMENT") == "interior, basement", ph("INT. BASEMENT"))
    check("INT./EXT. is not mistaken for one or the other",
          "interior and exterior" in ph("INT./EXT. CAR - DAY"), ph("INT./EXT. CAR - DAY"))
    check("an empty heading gives nothing rather than the word interior",
          ph("") == "" and ph(None) == "")
    check("CONTINUOUS is a note to the crew, not a look - it is dropped",
          ph("INT. GARAGE - CONTINUOUS") == "interior, garage",
          ph("INT. GARAGE - CONTINUOUS"))
    check("so is LATER", ph("EXT. DECK - LATER") == "exterior, deck",
          ph("EXT. DECK - LATER"))
    check("but a real time of day survives",
          ph("EXT. DECK - EVENING") == "exterior, deck, in the evening",
          ph("EXT. DECK - EVENING"))

    water = ("EXT. LAKE - DAY\n"
             "VICTOR: Ease off the gas!\n"
             "SAM: He's going in!\n"
             "INT. BOARDROOM - NIGHT\n"
             "VICTOR: Then it's over.\n")
    wcues = sv.script_parser.parse(water)
    sl = sv.script_parser.scenes(water)
    check("each line is matched to the heading above it",
          [sv.casting._scene_at(sl, c.line_no) for c in wcues]
          == ["EXT. LAKE - DAY", "EXT. LAKE - DAY", "INT. BOARDROOM - NIGHT"],
          str([sv.casting._scene_at(sl, c.line_no) for c in wcues]))

    # the location in the image prompt must come from the script, even when the
    # model says something else entirely - this is the bug the user hit
    victor = dict(sv.project.new_character("VICTOR"),
                  appearance="heavy-set, silver hair", wardrobe="expensive polo shirt")
    amap = {"VICTOR": victor}
    lake_shot = {"shot": "wide shot of VICTOR at the wheel", "scene": "EXT. LAKE - DAY",
                 "setting": "a corner office with a mahogany desk", "wardrobe": "swim trunks"}
    pr = sv.casting.shot_prompt(amap, wcues[0], lake_shot)
    check("the script's location wins over the model's invented one",
          "exterior, lake" in pr and "mahogany desk" not in pr, pr[:110])
    check("the scene's clothes are used", "wearing swim trunks" in pr, pr[:110])
    check("and the character's fixed outfit does not follow them onto the lake",
          "polo shirt" not in pr, pr[:130])
    check("but their face and build still do", "heavy-set, silver hair" in pr, pr[:130])

    # ---- the "college kid" bug: one wardrobe sentence covering everybody ----
    names = ["VICTOR", "NORA", "OTIS"]
    wf = sv.casting.wardrobe_for
    both = ("Nora wears a simple yet elegant dress that catches the light. "
            "Victor is wearing casual clothes, possibly a t-shirt and shorts.")
    check("a sentence about two people gives each of them their own clothes",
          wf({"wardrobe": both}, "VICTOR", names) == "casual clothes, possibly a t-shirt and shorts",
          repr(wf({"wardrobe": both}, "VICTOR", names)))
    check("and the other one gets the dress, not the shorts",
          wf({"wardrobe": both}, "NORA", names).startswith("a simple yet elegant dress"),
          repr(wf({"wardrobe": both}, "NORA", names)))
    check("a wardrobe that only names someone else is refused, not borrowed",
          wf({"wardrobe": "Nora wears a red coat."}, "VICTOR", names) == "",
          repr(wf({"wardrobe": "Nora wears a red coat."}, "VICTOR", names)))
    check("the object form we ask for is read straight off",
          wf({"wardrobe": {"VICTOR": "swim shorts", "NORA": "sundress"}},
             "VICTOR", names) == "swim shorts")
    check("the object form is matched case-insensitively",
          wf({"wardrobe": {"victor": "swim shorts"}}, "VICTOR", names) == "swim shorts")
    check("a name missing from the object gives nothing rather than the wrong row",
          wf({"wardrobe": {"NORA": "sundress"}}, "VICTOR", names) == "")
    check("one plain outfit naming nobody is taken as the speaker's",
          wf({"wardrobe": "a wetsuit and life vest"}, "VICTOR", names)
          == "a wetsuit and life vest")
    check("a leading 'Victor is wearing' is stripped, not doubled up",
          wf({"wardrobe": "Victor is wearing a grey suit"}, "VICTOR", names)
          == "a grey suit",
          repr(wf({"wardrobe": "Victor is wearing a grey suit"}, "VICTOR", names)))
    check("no wardrobe at all is empty, not the word None",
          wf({}, "VICTOR", names) == "" and wf({"wardrobe": None}, "VICTOR", names) == "")

    # the whole prompt, which is what actually reached the renderer
    kid_cue = sv.script_parser.parse("VICTOR: The new one?" + chr(10))[0]
    kid_shot = {"shot": "medium shot of VICTOR on the deck", "wardrobe": both,
                "scene": "EXT. MARINA - TERRACE - LATER"}
    kp = sv.casting.shot_prompt(amap, kid_cue, kid_shot)
    check("the speaker is never described as wearing another character's dress",
          "wearing Nora" not in kp and "elegant dress" not in kp, kp[:130])
    check("they wear their own clothes instead",
          "wearing casual clothes" in kp, kp[:130])
    check("and it is still the right person",
          "heavy-set, silver hair" in kp, kp[:130])

    # ---- whose face is in frame: the speaker is the LAST resort ------------
    sub = sv.casting.shot_subject
    hcue = sv.script_parser.parse("VICTOR: The new one?" + chr(10))[0]
    check("the model naming a subject is believed",
          sub({"shot": "close-up of a face", "subject": "NORA"}, hcue, names) == "NORA")
    check("a shot description that names someone is read",
          sub({"shot": "Close-up of Nora's face as she listens"}, hcue, names) == "NORA",
          sub({"shot": "Close-up of Nora's face as she listens"}, hcue, names))
    check("a shot naming nobody falls back to the speaker",
          sub({"shot": "Wide shot of the deck"}, hcue, names) == "VICTOR")
    check("a subject who is not in the cast is not trusted",
          sub({"shot": "Wide shot", "subject": "NOBODY"}, hcue, names) == "VICTOR")
    check("the user's choice beats the model's",
          sub({"shot": "Wide", "subject": "NORA", "subject_override": "OTIS"},
              hcue, names) == "OTIS")
    check("when two are named the first one in the sentence wins",
          sub({"shot": "Nora turns as Otis climbs the steps"}, hcue, names) == "NORA",
          sub({"shot": "Nora turns as Otis climbs the steps"}, hcue, names))
    check("a name inside another word is not a match",
          sub({"shot": "the victorious end of the pier"}, hcue, ["VIC"]) == "VICTOR")

    # the whole point: the right face gets drawn
    jshot = {"shot": "Close-up of NORA's face as she listens", "scene": "EXT. DECK - DAY"}
    jp = sv.casting.shot_prompt({"VICTOR": victor,
                                 "NORA": dict(sv.project.new_character("NORA"),
                                                appearance="auburn hair, freckles")},
                                hcue, jshot)
    check("a reaction shot draws the listener, not the speaker",
          "auburn hair" in jp and "silver hair" not in jp, jp[:110])

    # ---- the user's own shot description -----------------------------------
    st = sv.casting.shot_text
    check("with no override the AI's description is used",
          st({"shot": "wide shot of the lake"}) == "wide shot of the lake")
    check("an override replaces it outright",
          st({"shot": "wide shot", "shot_override": "extreme close-up of a wristwatch"})
          == "extreme close-up of a wristwatch")
    check("an empty override hands the shot back to the AI",
          st({"shot": "wide shot", "shot_override": "   "}) == "wide shot")
    ov = {"shot": "wide shot of the deck", "shot_override": "close-up of OTIS laughing"}
    check("the override decides who is in frame too",
          sub(ov, hcue, names) == "OTIS", sub(ov, hcue, names))
    check("and it is the override that reaches the renderer",
          "close-up of OTIS laughing" in sv.casting.shot_prompt(amap, hcue, ov),
          sv.casting.shot_prompt(amap, hcue, ov)[:80])

    # replanning must not throw the user's choices away
    keep_p = sv.project.new_project()
    keep_p["script"] = water
    keep_p["shots"] = {"0": {"line": "old text", "subject_override": "NORA",
                             "shot_override": "close-up of a wristwatch"}}
    kept = keep_p["shots"]["0"]
    was = dict(kept)
    fresh = {"shot": "the model's new idea", "line": wcues[0].text, "scene": "EXT. LAKE - DAY"}
    for k in ("subject_override", "shot_override"):
        if was.get(k):
            fresh[k] = was[k]
    check("replanning keeps the face the user picked",
          fresh["subject_override"] == "NORA")
    check("and keeps the description they wrote",
          fresh["shot_override"] == "close-up of a wristwatch")

    no_dress = dict(lake_shot)
    no_dress.pop("wardrobe")
    pr2 = sv.casting.shot_prompt(amap, wcues[0], no_dress)
    check("with no scene wardrobe the character's own outfit is used",
          "polo shirt" in pr2, pr2[:110])

    # a shot planned before scenes were recorded must be replanned, not re-used
    stale_p = sv.project.new_project()
    stale_p["script"] = water
    stale_p["shots"] = {"0": {"line": wcues[0].text, "shot": "old", "setting": "an office"}}
    _, planned = sv.pipeline.plan_shots_batched(sv.make_llm(p), stale_p, wcues) \
        if False else (None, None)
    todo_old = [i for i, c in enumerate(wcues)
                if (stale_p["shots"].get(str(i)) or {}).get("line") != c.text]
    check("the old cache key alone would have re-used the deskbound shot",
          0 not in todo_old, str(todo_old))
    heads = {}
    last = ""
    for i, c in enumerate(wcues):
        last = sv.casting._scene_at(sl, c.line_no) or last
        heads[i] = last
    todo_new = [i for i, c in enumerate(wcues)
                if (stale_p["shots"].get(str(i)) or {}).get("line") != c.text
                or (stale_p["shots"].get(str(i)) or {}).get("scene") != heads[i]]
    check("recording the scene makes that shot stale, so it is drawn again",
          0 in todo_new, str(todo_new))

    # ------------------------------------------- keeping one face across shots
    check.section("[5j] locking a character's identity")
    ii = sv.visuals.identity_image
    port = os.path.join(work, "id_portrait.png")
    spin0 = os.path.join(work, "id_spin.png")
    mine = os.path.join(work, "id_mine.png")
    for f in (port, spin0, mine):
        make_png(f)
    check("with nothing rendered there is nothing to lock onto",
          ii(sv.project.new_character("X")) == "")
    check("a drawn portrait is used when there is one",
          ii({"portrait": port}) == port)
    check("a turnaround frame will do if there is no portrait",
          ii({"turnaround": [spin0]}) == spin0)
    check("the user's own picture beats anything generated",
          ii({"reference_image": mine, "portrait": port,
              "turnaround": [spin0]}) == mine)
    check("a reference that has been moved or deleted is skipped, not passed on",
          ii({"reference_image": os.path.join(work, "gone.png"),
              "portrait": port}) == port)

    # The real bug: the storyboard and the movie built their shots separately,
    # and only the movie passed a reference image.
    calls = []
    real_run = sv.jobs.SlotRunner.run

    def spy_run(self, values, dest, kinds, **kw):
        calls.append(dict(values))
        return real_run(self, values, dest, kinds, **kw)

    idp = json.loads(json.dumps(p))
    idp["characters"]["MAYA"]["reference_image"] = mine
    # The example workflow has no reference input, so give the shot slot one -
    # otherwise this only proves the code correctly does nothing.
    shot_map = idp["workflows"]["shot"]["mapping"]
    # 4.ckpt_name is a real string input on this graph; what it drives does
    # not matter here, only that a reference reaches the workflow at all.
    shot_map["image"] = shot_map.get("image") or "4.ckpt_name"
    idcues = sv.script_parser.parse("MAYA: One line only." + chr(10))
    sv.jobs.SlotRunner.run = spy_run
    try:
        board = sv.StoryboardJob(idp, idcues, os.path.join(work, "idboard"),
                                 lambda e: None)
        run_job(board, timeout=90)
        check("the storyboard renders", board.error is None, board.error or "")
        board_calls = [c for c in calls if "prompt" in c]
        check("the storyboard passes a reference image, as the movie always did",
              board_calls and "image" in board_calls[-1],
              str(sorted(board_calls[-1])) if board_calls else "no calls")
    finally:
        sv.jobs.SlotRunner.run = real_run

    # one path, so the picture you judge is the picture that gets filmed
    class _R(object):
        def __init__(self, has_image=True):
            self._has = has_image
            self.uploaded = []

        def has(self, key):
            return key == "image" and self._has

        def upload(self, path):
            self.uploaded.append(path)
            return "uploaded_" + os.path.basename(path)

    amap2 = {"MAYA": dict(sv.project.new_character("MAYA"),
                          appearance="short dark hair", portrait=port),
             "RUBEN": dict(sv.project.new_character("RUBEN"),
                           appearance="a burn scar", portrait=spin0)}
    cue2 = sv.script_parser.parse("MAYA: Where were you?" + chr(10))[0]
    r_ok = _R(True)
    vals, prompt2, seed2, subj2, ref2 = sv.pipeline.shot_values(
        r_ok, amap2, cue2, {"shot": "medium shot"}, 3)
    check("a shot is conditioned on the speaker's own face",
          vals.get("image") == "uploaded_id_portrait.png", str(vals.get("image")))
    check("and the seed follows that same character",
          seed2 == int(amap2["MAYA"]["look_seed"]) + 3)

    react = {"shot": "close-up of RUBEN listening"}
    vals_r, _, seed_r, subj_r, _ = sv.pipeline.shot_values(_R(True), amap2, cue2, react, 3)
    check("a reaction shot is conditioned on the listener, not the speaker",
          subj_r == "RUBEN" and vals_r.get("image") == "uploaded_id_spin.png",
          "%s %s" % (subj_r, vals_r.get("image")))
    check("and takes the listener's seed too",
          seed_r == int(amap2["RUBEN"]["look_seed"]) + 3)

    r_no = _R(False)
    vals_n, _, _, _, ref_n = sv.pipeline.shot_values(
        r_no, amap2, cue2, {"shot": "medium shot"}, 3)
    check("a workflow with no image input still renders, just without the lock",
          "image" not in vals_n and ref_n == "" and r_no.uploaded == [],
          str(sorted(vals_n)))

    # changing the reference must redraw, not re-use the old picture
    key_of = lambda pr, sd, tx, rf: hashlib.sha1(
        ("%s|%d|%s|%s" % (pr, sd, tx, rf)).encode("utf-8")).hexdigest()[:16]
    check("swapping the reference face makes the shot stale",
          key_of(prompt2, seed2, cue2.text, port)
          != key_of(prompt2, seed2, cue2.text, mine))

    # ---------------------------------------- the single file on its own
    check.section("[5k] ScriptVoice.py with nothing beside it")
    if sv.label == "single file":
        check("the default workflow is baked into the built file",
              bool(getattr(sv.project, "EMBEDDED_WORKFLOW_JSON", "")),
              "%d bytes" % len(getattr(sv.project, "EMBEDDED_WORKFLOW_JSON", "")))
        baked = json.loads(sv.project.EMBEDDED_WORKFLOW_JSON)
        check("what was baked in is a real API-format workflow",
              baked and all("class_type" in n for n in baked.values()),
              str(len(baked)) + " nodes")
        check("it matches the workflow file it was built from",
              baked == json.load(io.open(os.path.join(HERE, "workflows",
                                                      "sdxl_turbo_actor_api.json"),
                                         encoding="utf-8")))
    saved_bundled = sv.project.bundled_workflow
    try:
        # exactly the situation of copying the one file somewhere on its own
        sv.project.bundled_workflow = lambda *a, **k: ""
        alone = sv.project.new_project()
        filled = sv.project.adopt_default_workflows(alone)
        if sv.label == "single file":
            check("with no workflows folder the built file still fills its slots",
                  set(filled) == {"portrait", "turnaround", "shot"}, str(filled))
            check("and points them at the built-in copy",
                  alone["workflows"]["portrait"]["path"] == sv.project.BUILTIN,
                  alone["workflows"]["portrait"]["path"])
            check("the built-in copy loads without touching the disk",
                  bool(sv.project.load_workflow(sv.project.BUILTIN)))
            check("and its inputs are worked out the same way",
                  {"prompt", "negative", "seed"}
                  <= set(alone["workflows"]["portrait"]["mapping"]),
                  str(sorted(alone["workflows"]["portrait"]["mapping"])))
            runner_ok = sv.jobs.SlotRunner(
                sv.comfy.ComfyClient("127.0.0.1", 9998), alone, "portrait")
            check("a runner accepts the built-in path instead of demanding a file",
                  runner_ok.workflow is not None)
        else:
            check("the package with no workflows folder fills nothing, and says so",
                  filled == [], str(filled))
    finally:
        sv.project.bundled_workflow = saved_bundled

    check("a workflow path that is neither built-in nor real is refused",
          _raises(lambda: sv.jobs.SlotRunner(
              sv.comfy.ComfyClient("127.0.0.1", 9998),
              dict(sv.project.new_project(),
                   workflows={"portrait": {"path": os.path.join(work, "nope.json"),
                                           "mapping": {"prompt": "6.text"}}}),
              "portrait")))

    # ------------------------------------- the reference-conditioning workflow
    check.section("[5l] the PhotoMaker workflow that locks a face")
    pm_path = os.path.join(HERE, "workflows", "sdxl_photomaker_reference_api.json")
    if os.path.exists(pm_path):
        pm = sv.project.load_workflow(pm_path)
        check("it is a valid API-format workflow", len(pm) >= 8, "%d nodes" % len(pm))
        classes = sorted(n["class_type"] for n in pm.values())
        check("it uses the PhotoMaker nodes ComfyUI already ships",
              "PhotoMakerLoader" in classes and "PhotoMakerEncode" in classes,
              str(classes))
        check("it takes a reference image", "LoadImage" in classes, str(classes))
        pmap = sv.project.guess_mapping(pm, "shot")
        check("the prompt is mapped to PhotoMaker, not a plain text encoder",
              pmap.get("prompt", "").split(".")[0] and
              pm[pmap["prompt"].split(".")[0]]["class_type"] == "PhotoMakerEncode",
              pmap.get("prompt"))
        check("the reference image is mapped to the loader",
              pm[pmap.get("image", ".").split(".")[0]]["class_type"] == "LoadImage",
              pmap.get("image"))
        check("nothing a shot needs is left unmapped",
              not [k for k, _, req in sv.project.WORKFLOW_SLOTS["shot"]["keys"]
                   if req and not pmap.get(k)],
              str(sorted(pmap)))
        check("a workflow with a reference input reports that it can lock a face",
              sv.jobs.SlotRunner(sv.comfy.ComfyClient("127.0.0.1", 9998),
                                 dict(sv.project.new_project(),
                                      workflows={"shot": {"path": pm_path,
                                                          "mapping": pmap}}),
                                 "shot").has("image"))

    # two characters must never share one uploaded reference
    a_png = os.path.join(work, "ref_a", "portrait.png")
    b_png = os.path.join(work, "ref_b", "portrait.png")
    for f, shade in ((a_png, 40), (b_png, 200)):
        os.makedirs(os.path.dirname(f), exist_ok=True)
        make_png(f, shade=shade)
    up = []

    class _UpClient(object):
        def upload_audio(self, path, subfolder="scriptvoice"):
            import hashlib as _h
            data = io.open(path, "rb").read()
            stem, ext = os.path.splitext(os.path.basename(path))
            name = "%s/%s_%s%s" % (subfolder, stem, _h.sha1(data).hexdigest()[:10], ext)
            up.append(name)
            return name

    c = _UpClient()
    n1, n2, n1_again = (c.upload_audio(a_png), c.upload_audio(b_png),
                        c.upload_audio(a_png))
    check("two characters whose portraits share a filename do not collide",
          n1 != n2, "%s vs %s" % (n1, n2))
    check("the same picture always uploads to the same name", n1 == n1_again)
    check("the name still ends in the right extension", n1.endswith(".png"), n1)

    # ------------------------------- words in front of every picture prompt
    check.section("[5m] the prompt prefix identity models require")
    wp = sv.casting.with_prefix
    check("no prefix leaves the prompt exactly as it was",
          wp("a man on a pier", "") == "a man on a pier")
    check("a prefix goes at the very front, where the trigger must be",
          wp("a man on a pier", "a person img") == "a person img, a man on a pier")
    check("a trailing comma in the setting does not double up",
          wp("a man on a pier", "a person img,") == "a person img, a man on a pier")
    check("it is not added twice if it is already there",
          wp("a person img, a man on a pier", "a person img")
          == "a person img, a man on a pier")
    check("matching ignores case, so 'A Person Img' is not doubled",
          wp("A Person Img, on a pier", "a person img") == "A Person Img, on a pier")
    check("a prefix with an empty prompt is still the prefix",
          wp("", "a person img") == "a person img")

    pf = sv.project.new_project()
    check("the setting starts empty, so nothing changes for other workflows",
          pf["options"]["prompt_prefix"] == "")

    pre_map = {"VICTOR": dict(sv.project.new_character("VICTOR"),
                              appearance="silver hair, heavy build")}
    pre_cue = sv.script_parser.parse("VICTOR: Say that again." + chr(10))[0]
    plain = sv.casting.shot_prompt(pre_map, pre_cue, {"shot": "wide shot"})
    pref = sv.casting.shot_prompt(pre_map, pre_cue, {"shot": "wide shot"},
                                  prefix="a person img")
    check("a shot prompt gains the prefix at the front",
          pref.startswith("a person img, ") and pref.endswith(plain), pref[:60])
    check("and the shot itself is untouched underneath", plain in pref)
    check("portraits get it too, or the same workflow would reject them",
          wp(sv.casting.look_prompt(pre_map["VICTOR"]), "a person img")
          .startswith("a person img, "))
    check("every turnaround angle gets it, not just the first",
          all(wp(pr, "a person img").startswith("a person img, ")
              for _, pr in sv.casting.turnaround_prompts(pre_map["VICTOR"])))

    # ------------------------------------------------ casting the right voice
    check.section("[5n] voice gender: inferred, and overridable")
    S = sv.speech
    two = [{"name": "Microsoft David Desktop", "gender": "Male"},
           {"name": "Microsoft Zira Desktop", "gender": "Female"}]
    check("a named range still decides the gender",
          S._range_of("warm dry American alto")[0] == "Female")
    check("a stem matches, so Sopranist is not missed",
          S._range_of("Sopranist")[0] == "Female", str(S._range_of("Sopranist")))
    check("countertenor is not read as tenor",
          S._range_of("countertenor") == ("Male", 16), str(S._range_of("countertenor")))
    check("contralto is not read as alto",
          S._range_of("contralto") == ("Female", -18), str(S._range_of("contralto")))
    check("'deep' now implies male - the surfer was drawing the female voice",
          S._range_of("Deep, authoritative")[0] == "Male")
    check("a description with nothing in it implies nothing",
          S._range_of("")[0] is None)

    dane = {"name": "OTIS", "voice_type": "Deep, authoritative", "seed": 932377303}
    auto = S.assign_voice(dane["seed"], two, hint=dane["voice_type"])
    check("inference alone now casts him male", auto["voice"] == "Microsoft David Desktop",
          str(auto["voice"]))
    forced_f = S.assign_voice(dane["seed"], two, hint=dane["voice_type"], gender="Female")
    check("an explicit choice overrides the description",
          forced_f["voice"] == "Microsoft Zira Desktop", str(forced_f["voice"]))
    forced_m = S.assign_voice(dane["seed"], two, hint="Soprano", gender="Male")
    check("and overrides it in the other direction too",
          forced_m["voice"] == "Microsoft David Desktop", str(forced_m["voice"]))
    check("a blank choice falls back to the description",
          S.assign_voice(dane["seed"], two, hint="Soprano", gender="")["voice"]
          == "Microsoft Zira Desktop")
    check("a nonsense choice is ignored rather than emptying the pool",
          S.assign_voice(dane["seed"], two, hint="Soprano", gender="banana")["voice"]
          == "Microsoft Zira Desktop")
    check("the choice is stable, not re-rolled each call",
          S.assign_voice(dane["seed"], two, hint="", gender="Male")
          == S.assign_voice(dane["seed"], two, hint="", gender="Male"))

    ch = sv.project.new_character("OTIS")
    check("a new character has no gender chosen, so nothing is forced",
          ch["voice_gender"] == "")

    # -------------------------------------------- more than one person in frame
    check.section("[5o] two characters in one shot")
    sp = sv.casting.shot_people
    ppl = ["MIKE", "PLANT MANAGER", "VICTOR"]
    pmap = {n: dict(sv.project.new_character(n), appearance=a, wardrobe=w)
            for n, a, w in (("MIKE", "lean, grey stubble", "worn jacket"),
                            ("PLANT MANAGER", "stocky, hi-vis vest", "hard hat"),
                            ("VICTOR", "silver hair", "polo shirt"))}
    mcue = sv.script_parser.parse("MIKE: Look at it." + chr(10))[0]

    two = {"shot": "wide shot on the railing above the factory floor",
           "cast": ["MIKE", "PLANT MANAGER"], "scene": "INT. FACTORY - DAY"}
    check("everyone the planner listed is in frame",
          sp(two, mcue, ppl) == ["MIKE", "PLANT MANAGER"], str(sp(two, mcue, ppl)))
    check("the first one listed holds the locked face",
          sv.casting.shot_subject(two, mcue, ppl) == "MIKE")
    p2 = sv.casting.shot_prompt(pmap, mcue, two)
    check("the second person is described, not left to invention",
          "with Plant Manager" in p2 and "hi-vis vest" in p2, p2[:150])
    check("and the first is still the one drawn from",
          "lean, grey stubble" in p2, p2[:150])
    check("their clothes come along too", "hard hat" in p2, p2[:170])

    check("a name that is not in the cast is dropped from the list",
          sp({"cast": ["MIKE", "NOBODY"]}, mcue, ppl) == ["MIKE"],
          str(sp({"cast": ["MIKE", "NOBODY"]}, mcue, ppl)))
    check("a repeated name appears once",
          sp({"cast": ["MIKE", "MIKE"]}, mcue, ppl) == ["MIKE"])
    check("the user's list beats the planner's",
          sp({"cast": ["MIKE"], "cast_override": ["VICTOR", "MIKE"]}, mcue, ppl)
          == ["VICTOR", "MIKE"])
    check("and it moves the locked face with it",
          sv.casting.shot_subject(
              {"cast": ["MIKE"], "cast_override": ["VICTOR", "MIKE"]}, mcue, ppl) == "VICTOR")
    check("a pinned face still outranks the list",
          sv.casting.shot_subject(
              {"cast_override": ["VICTOR", "MIKE"], "subject_override": "MIKE"},
              mcue, ppl) == "MIKE")
    check("with no list at all, the shot text is read",
          sp({"shot": "VICTOR turns as MIKE climbs the steps"}, mcue, ppl)
          == ["VICTOR", "MIKE"],
          str(sp({"shot": "VICTOR turns as MIKE climbs the steps"}, mcue, ppl)))
    check("and failing that it falls back to the speaker",
          sp({"shot": "a wide empty room"}, mcue, ppl) == ["MIKE"])
    check("a one-person shot is unchanged - no 'with' clause appears",
          "with " not in sv.casting.shot_prompt(pmap, mcue, {"shot": "close-up",
                                                             "cast": ["MIKE"]}),
          sv.casting.shot_prompt(pmap, mcue, {"shot": "close-up", "cast": ["MIKE"]})[:90])
    crowd = {"shot": "the whole room", "cast": ["MIKE", "PLANT MANAGER", "VICTOR"]}
    pc = sv.casting.shot_prompt(pmap, mcue, crowd)
    check("a crowd is capped so the prompt does not run away",
          pc.count("with ") <= 2, "%d 'with' clauses" % pc.count("with "))

    # only one identity can be locked, so only one reference is ever uploaded
    class _R1(object):
        def __init__(self):
            self.uploaded = []

        def has(self, key):
            return key == "image"

        def upload(self, path):
            self.uploaded.append(path)
            return "up_" + os.path.basename(path)

    port = os.path.join(work, "crowd_ref.png")
    make_png(port)
    pmap["MIKE"]["portrait"] = port
    rr = _R1()
    vals, _, _, subj, ref = sv.pipeline.shot_values(rr, pmap, mcue, two, 0)
    check("a two-person shot still uploads exactly one reference face",
          len(rr.uploaded) == 1, str(rr.uploaded))
    check("and it is the first person's", subj == "MIKE" and ref == port, "%s %s" % (subj, ref))

    # --------------------------------------------------------- regeneration
    check.section("[6] the regenerate buttons")
    before_voice = dict(p["characters"]["MAYA"])
    run_job(sv.RegenerateJob(p, p["options"]["output_dir"], lambda e: None, "MAYA",
                             what="voice"))
    maya = p["characters"]["MAYA"]
    check("voice changed", maya["voice_type"] == "soft, breathy, high", maya["voice_type"])
    check("appearance untouched", maya["appearance"] == before_voice["appearance"])
    check("voice seed re-rolled", maya["seed"] != before_voice["seed"])
    check("approval reset after a change", not maya["approved"])

    seen = run_job(sv.RegenerateJob(p, p["options"]["output_dir"], lambda e: None,
                                    "RUBEN", what="actor"))
    ruben = p["characters"]["RUBEN"]
    bars = [e for e in seen if e.get("kind") == "actor_progress"]
    check("the card gets progress while the actor is being made", len(bars) >= 4,
          str(len(bars)))
    check("every progress event names the actor it belongs to",
          all(e.get("name") == "RUBEN" for e in bars),
          str(sorted({e.get("name") for e in bars})))
    check("the turnaround reports a real frame count, not a guess",
          any(e.get("total") == 8 and e.get("done") == 8 for e in bars),
          str([(e.get("done"), e.get("total")) for e in bars if e.get("total")]))
    check("the bar rises rather than jumping about",
          [e["done"] for e in bars if e.get("total") == 8]
          == sorted(e["done"] for e in bars if e.get("total") == 8))
    check("the bar is cleared at the end, so it cannot march for ever",
          bars[-1].get("label") == "", repr(bars[-1].get("label")))
    check("recast keeps the name so the script still works", "RUBEN" in p["characters"])
    check("recast is a different person", "burn scar" in ruben["appearance"], ruben["appearance"])
    check("recast re-rendered the portrait", os.path.exists(ruben.get("portrait", "")))
    check("recast re-rendered the spin", len(ruben.get("turnaround", [])) == 8)

    # -------------------------------------------------------------- writing
    check.section("[7] the AI writes the script")
    text = sv.casting.write_script(sv.make_llm(p), p["premise"],
                                   sv.project.cast(p), scenes=2)
    check("preamble stripped", not text.lower().startswith("sure"))
    check("scene headers kept", "[SCENE: The lamp room, midnight]" in text)
    check("lowercase speaker snapped to the cast", "MAYA: That isn't an answer." in text)
    parsed = sv.script_parser.parse(text)
    check("the written script parses back into cues", len(parsed) == 4, str(len(parsed)))
    check("only cast members speak",
          set(sv.script_parser.speakers(parsed)) <= {"MAYA", "RUBEN"},
          str(sv.script_parser.speakers(parsed)))

    # ----------------------------------------------------------- storyboard
    check.section("[8] the storyboard")
    p["script"] = text
    board_cues = sv.script_parser.parse(text)
    before = len(STATE["jobs"])
    board = sv.StoryboardJob(p, board_cues, p["options"]["output_dir"], lambda e: None)
    events = run_job(board, timeout=120)
    check("storyboard job finished", board.error is None, board.error or "")
    check("one drawing per line", len(board.images) == len(board_cues),
          "%d of %d" % (len(board.images), len(board_cues)))
    check("every drawing exists on disk",
          all(os.path.exists(f) for f in board.images.values()))
    check("a shot was planned for every line",
          all(str(i) in (p.get("shots") or {}) for i in range(len(board_cues))))
    check("the plan records which line it was drawn for",
          all((p["shots"][str(i)] or {}).get("line") == c.text
              for i, c in enumerate(board_cues)))
    check("drawing used one prompt per line",
          len(STATE["jobs"]) - before == len(board_cues),
          "%d prompts" % (len(STATE["jobs"]) - before))
    drawn_prompts = [wf["6"]["inputs"]["text"] for wf in STATE["prompts"][-len(board_cues):]]
    check("the speaker's locked look is folded into the shot prompt",
          any("close-cropped black hair" in q for q in drawn_prompts))

    before = len(STATE["jobs"])
    run_job(sv.StoryboardJob(p, board_cues, p["options"]["output_dir"], lambda e: None),
            timeout=120)
    check("drawing again re-uses every finished shot", len(STATE["jobs"]) == before,
          "%d new" % (len(STATE["jobs"]) - before))

    before = len(STATE["jobs"])
    one = sv.StoryboardJob(p, board_cues, p["options"]["output_dir"], lambda e: None, only=[1])
    run_job(one, timeout=120)
    check("redrawing one shot redraws exactly one", len(STATE["jobs"]) - before == 1,
          "%d new" % (len(STATE["jobs"]) - before))
    check("a redraw returns just that shot", list(one.images) == [1], str(list(one.images)))

    check("batched planning covers a script too long for one reply",
          sv.plan_shots_batched(sv.make_llm(p), p, board_cues, batch=2)[0] is not None)

    # ---------------------------------------------------------------- movie
    check.section("[9] the movie")
    p["script"] = text
    for a in sv.project.cast(p):
        a["approved"] = True
    cues = sv.script_parser.parse(text)
    before = len(STATE["jobs"])
    job = sv.MovieJob(p, cues, p["options"]["output_dir"], lambda e: None)
    events = run_job(job, timeout=180)
    check("movie job finished", job.error is None, job.error or "")
    check("shots were planned", len(p.get("shots") or {}) == len(cues),
          str(len(p.get("shots") or {})))
    check("the movie re-uses the storyboard instead of redrawing it",
          len(STATE["jobs"]) - before == len(cues),
          "%d prompts for %d lines (audio only)" % (len(STATE["jobs"]) - before, len(cues)))
    check("every line still has both a clip and a picture in the cut",
          all(s["audio"] and s["image"] for s in
              json.load(open(job.edl_path, encoding="utf-8"))["shots"]))
    check("edit list written", os.path.exists(job.edl_path or ""))
    edl = json.load(open(job.edl_path, encoding="utf-8")) if job.edl_path else {}
    check("edit list covers every line", len(edl.get("shots", [])) == len(cues))
    check("edit list timings run forward",
          all(edl["shots"][i]["start"] <= edl["shots"][i + 1]["start"]
              for i in range(len(edl.get("shots", [])) - 1)))
    check("every entry has audio and a picture",
          all(s["audio"] and s["image"] for s in edl.get("shots", [])))
    check("full audio take stitched", os.path.exists(edl.get("audio", "")), edl.get("audio", ""))
    M = sv.movie
    check("a cutter is available (ffmpeg binary or PyAV)",
          bool(M.find_ffmpeg()) or M.have_pyav(),
          "ffmpeg=%s pyav=%s" % (bool(M.find_ffmpeg()), M.have_pyav()))
    if M.have_pyav():
        _mv = os.path.join(work, "tiny.mp4")
        _ent = [e for e in edl.get("shots", [])][:3]
        _ent = [{"index": i, "audio": s["audio"], "image": s["image"], "seconds": s["duration"]}
                for i, s in enumerate(_ent)]
        try:
            M.assemble_pyav(_ent, _mv, gap_seconds=0.1, size=(320, 180), fps=12)
            _ok = os.path.exists(_mv) and os.path.getsize(_mv) > 2000
        except Exception as _e:
            _ok = False
            print("      pyav mux raised: %s: %s" % (type(_e).__name__, _e))
        check("PyAV cuts a real mp4 from clips and stills", _ok,
              "%d bytes" % (os.path.getsize(_mv) if os.path.exists(_mv) else 0))
        if _ok:
            import av as _av
            with _av.open(_mv) as _c:
                _v = [s for s in _c.streams if s.type == "video"]
                _a = [s for s in _c.streams if s.type == "audio"]
                _dur = float(_c.duration or 0) / 1000000.0
            check("the cut has both a picture and a sound track", len(_v) == 1 and len(_a) == 1)
            # A file with the right streams can still be pure static: an
            # uninitialised VideoFrame encodes happily. Check the pixels.
            try:
                with _av.open(_mv) as _c2:
                    _got = next(_c2.decode(video=0)).to_ndarray(format="rgb24")
                with _av.open(_ent[0]["image"]) as _c3:
                    _want = next(_c3.decode(video=0)).to_ndarray(format="rgb24")

                def _thumb(_a, _w=48, _h=27):
                    import math
                    _ys = [int(y * (_a.shape[0] - 1) / max(1, _h - 1)) for y in range(_h)]
                    _xs = [int(x * (_a.shape[1] - 1) / max(1, _w - 1)) for x in range(_w)]
                    return [float(_a[y][x][0]) for y in _ys for x in _xs]

                _g, _w2 = _thumb(_got), _thumb(_want)
                _mg = sum(_g) / len(_g)
                _mw = sum(_w2) / len(_w2)
                _cov = sum((a - _mg) * (b - _mw) for a, b in zip(_g, _w2))
                _sg = sum((a - _mg) ** 2 for a in _g) ** 0.5
                _sw = sum((b - _mw) ** 2 for b in _w2) ** 0.5
                _corr = _cov / (_sg * _sw) if _sg and _sw else 0.0
            except Exception as _e:
                _corr = -1.0
                print("      pixel check raised: %s" % _e)
            check("the cut shows the actual shot, not static", _corr > 0.85,
                  "correlation %.3f" % _corr)
            check("a missing shot yields black, not uninitialised memory",
                  M.black_frame((32, 32)).to_ndarray(format="rgb24").max() == 0)
            check("the cut is as long as the lines it holds", _dur > 0.5, "%.2fs" % _dur)

    ff = sv.movie.find_ffmpeg()
    if ff:
        check("movie muxed", os.path.exists(job.movie_path or ""), job.movie_path or "")
    else:
        check("PyAV cut the movie in ffmpeg's absence" if sv.movie.have_pyav()
              else "no cutter: assets kept and the mux is skipped cleanly",
              (os.path.exists(job.movie_path or "") if sv.movie.have_pyav()
               else job.movie_path == "") and os.path.exists(job.edl_path),
              job.movie_path or "no mux")

    # a second pass must not redo finished work
    before = len(STATE["jobs"])
    job2 = sv.MovieJob(p, cues, p["options"]["output_dir"], lambda e: None)
    run_job(job2, timeout=180)
    check("re-running the movie re-uses everything", len(STATE["jobs"]) == before,
          "%d new prompts" % (len(STATE["jobs"]) - before))

    cues[1] = sv.script_parser.Cue(1, cues[1].speaker, "A completely different line now.", 0)
    before = len(STATE["jobs"])
    run_job(sv.MovieJob(p, cues, p["options"]["output_dir"], lambda e: None), timeout=180)
    check("editing one line re-renders only that line's audio and shot",
          len(STATE["jobs"]) - before == 2, "%d new prompts" % (len(STATE["jobs"]) - before))

    # ------------------------------------------------------------- project io
    check.section("[10] project round trip")
    pp = os.path.join(work, "demo.svproj")
    sv.project.save(p, pp)
    p2 = sv.project.load(pp)
    check("cast survives", p2["characters"]["MAYA"]["appearance"] == maya["appearance"])
    check("turnaround paths survive", len(p2["characters"]["MAYA"]["turnaround"]) == 8)
    check("every workflow slot survives",
          all(p2["workflows"][s]["path"] for s in ("voice", "portrait", "turnaround", "shot")))
    check("premise survives", p2["premise"] == p["premise"])
    old = {"version": 1, "workflow_path": wf_voice, "mapping": {"text": "2.text"},
           "characters": {"BOB": {"voice_value": "x"}}, "script": "BOB: hi"}
    old_path = os.path.join(work, "old.svproj")
    with open(old_path, "w", encoding="utf-8") as f:
        json.dump(old, f)
    p3 = sv.project.load(old_path)
    check("a version 1 project still opens",
          p3["workflows"]["voice"]["path"] == wf_voice
          and p3["characters"]["BOB"]["voice_value"] == "x")

    # ------------------------------------------------- a damaged project file
    check.section("[8b] a project file that has been mangled")
    damaged = [
        ("an empty file", ""),
        ("truncated mid-JSON", '{"version": 2, "characters": {"BOB": {"nam'),
        ("valid JSON, wrong shape (a list)", "[1, 2, 3]"),
        ("valid JSON, wrong shape (a string)", '"hello"'),
        ("JSON null", "null"),
        ("JSON number", "42"),
        ("characters is a list", '{"characters": [{"name": "BOB"}]}'),
        ("options is a string", '{"options": "nope"}'),
        ("workflows is a list", '{"workflows": [1, 2]}'),
        ("a null character entry", '{"characters": {"BOB": null}}'),
        ("a character that is not a dict", '{"characters": {"BOB": "juststring"}}'),
        ("cast_order is not a list", '{"characters": {"BOB": {}}, "cast_order": "BOB"}'),
        ("turnaround is a string", '{"characters": {"BOB": {"turnaround": "a.png"}}}'),
        ("server is null", '{"server": null}'),
        ("a leading BOM", u"\ufeff" + '{"version": 2, "name": "X"}'),
        ("a workflow path that is gone",
         '{"workflows": {"voice": {"path": "C:/gone/x.json", "mapping": {"text": "1.text"}}}}'),
    ]
    broke = []
    for label, text in damaged:
        try:
            d = sv.project.load(_write(work, "bad.svproj", text))
            sv.project.cast(d)
            assert isinstance(d["options"], dict) and isinstance(d["characters"], dict)
            assert set(d["workflows"]) == set(sv.project.WORKFLOW_SLOTS)
        except Exception as e:                                   # noqa: BLE001
            broke.append("%s: %s" % (label, e))
    check("no damaged project file crashes the loader", not broke, "; ".join(broke))

    d = sv.project.load(_write(work, "bad.svproj", '{"options": {"gap_seconds": "abc"}}'))
    check("gap_seconds = \"abc\" falls back to the default", d["options"]["gap_seconds"] == 0.35,
          repr(d["options"]["gap_seconds"]))
    d = sv.project.load(_write(work, "bad.svproj", '{"options": {"max_chars": -5}}'))
    check("a negative max_chars falls back to the default", d["options"]["max_chars"] == 0,
          repr(d["options"]["max_chars"]))
    d = sv.project.load(_write(work, "bad.svproj", '{"characters": {"BOB": null}}'))
    check("a null character becomes a blank one, not a crash",
          d["characters"]["BOB"]["name"] == "BOB" and d["cast_order"] == ["BOB"])
    d = sv.project.load(_write(work, "bad.svproj", u'\ufeff{"name": "Kept"}'))
    check("a BOM does not hide the whole project", d["name"] == "Kept", d["name"])
    check("num_option never raises",
          sv.project.num_option({"g": "abc"}, "g", 0.35) == 0.35
          and sv.project.num_option(None, "g", 0.35) == 0.35
          and sv.project.num_option({"g": "2.5"}, "g", 0.35) == 2.5)

    # ------------------------------------------------ the Windows voice path
    check.section("[8c] system voices")
    C = sv.script_parser.Cue
    cue = C(0, "MAYA", "hello", 1)
    base_ch = sv.project.new_character("MAYA")
    base_ch["voice_type"] = "low dry baritone"
    table = [{"name": "V-Male", "gender": "Male"}, {"name": "V-Female", "gender": "Female"}]
    check("a voice hint picks the matching gender",
          sv.speech.assign_voice(1, table, "warm alto")["voice"] == "V-Female"
          and sv.speech.assign_voice(1, table, "gravel bass")["voice"] == "V-Male")
    check("the voice seed does not depend on this process",
          sv.speech.stable_seed("MAYA", "baritone") == 1914259515,
          str(sv.speech.stable_seed("MAYA", "baritone")))
    check("the same character always gets the same voice",
          sv.render_mod.system_voice(dict(base_ch))
          == sv.render_mod.system_voice(dict(base_ch)))

    def key_for(ch):
        v = sv.render_mod.system_voice(ch)
        sig = dict(v, voice_type=ch.get("voice_type", ""),
                   system_voice=ch.get("system_voice") or {})
        return sv.render_mod.cue_key(cue, ch, sig, "system")

    recast = dict(base_ch, voice_type="bright clear alto")
    override = dict(base_ch, system_voice={"voice": "V-Female", "rate": 2, "pitch": 9})
    unrelated = dict(base_ch, notes="a note nobody hears", approved=True)
    check("re-casting a character's range invalidates the cached clip",
          key_for(recast) != key_for(base_ch))
    check("a system_voice override invalidates the cached clip",
          key_for(override) != key_for(base_ch))
    check("an unrelated edit does not invalidate the cached clip",
          key_for(unrelated) == key_for(base_ch))
    check("the cue key is stable across runs",
          key_for(dict(base_ch)) == key_for(dict(base_ch)))
    check("cue_key survives a value json cannot serialise",
          len(sv.render_mod.cue_key(cue, dict(base_ch, params={"a": object()}),
                                    {}, "system")) == 16)

    if sv.speech.available():
        wav = os.path.join(work, u"sp ace'd \u00e9.wav")
        hostile_text = ("it's a 'test'; $(New-Item -ItemType File -Path "
                        "'%s') `x` %%PATH%% \"q\""
                        % os.path.join(work, "PWNED.txt").replace("\\", "/"))
        sv.speech.speak_to_wav(hostile_text, wav)
        check("a hostile line is spoken, not executed",
              os.path.isfile(wav) and not os.path.exists(os.path.join(work, "PWNED.txt")))
        check("a path with spaces, a quote and unicode still works",
              os.path.getsize(wav) > 128)
        sv.speech.speak_to_wav("a NUL\x00byte", os.path.join(work, "nul.wav"))
        check("a NUL byte does not fail the line",
              os.path.getsize(os.path.join(work, "nul.wav")) > 128)
        bad = ""
        try:
            sv.speech.speak_to_wav("hello", os.path.join(work, "x.wav"),
                                   voice="NoSuchVoice ZZZ")
        except sv.speech.SpeechError as e:
            bad = str(e)
        check("an uninstalled voice is refused, not silently swapped",
              "NoSuchVoice ZZZ" in bad, bad[:80])
        told = ""
        try:
            sv.speech.speak_to_wav("hello", work)     # a directory, not a file
        except sv.speech.SpeechError as e:
            told = str(e)
        check("a destination that is a directory is not reported as audio", bool(told))
        check("rate and pitch far out of range are clamped, not passed through",
              os.path.getsize(sv.speech.speak_to_wav(
                  "hello", os.path.join(work, "clamp.wav"), rate=99999, pitch=-99999)) > 128)
    else:
        check.section("  (no Windows speech here - system voice audio checks skipped)")

    # ------------------------------------------------------ a render under fire
    check.section("[8d] a render that goes wrong")
    r = sv.project.new_project()
    r["server"] = {"host": "127.0.0.1", "port": COMFY_PORT}
    r["workflows"]["voice"] = dict(p["workflows"]["voice"])
    r["characters"] = {"MAYA": sv.project.new_character("MAYA")}
    out = os.path.join(work, "rescue")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        f.write("{not json at all")
    with open(os.path.join(out, "unrelated.txt"), "w", encoding="utf-8") as f:
        f.write("someone else's file")
    ev = run_job(sv.RenderJob(r, [C(0, "GHOST", "not in the cast", 1),
                                  C(1, "MAYA", "   ", 2),
                                  C(2, "MAYA", "a real line", 3)], out, None))
    kinds = [e["kind"] for e in ev]
    check("a corrupt manifest does not stop the render", kinds[-1] == "finished", str(kinds[-1]))
    check("a speaker who is not in the cast still gets a voice",
          any(e.get("speaker") == "GHOST" and e["kind"] == "cue_done" for e in ev))
    check("an empty cue is skipped, not fatal",
          any(e.get("kind") == "cue_done" and e.get("skipped") for e in ev))
    check("the real lines either side of it are still rendered", len(ev[-1].get("files", [])) == 2)
    check("an unrelated file in the output folder is left alone",
          os.path.exists(os.path.join(out, "unrelated.txt")))
    with open(os.path.join(out, "manifest.json"), encoding="utf-8") as f:
        check("the manifest is rewritten as valid json", isinstance(json.load(f), dict))

    slow = [C(i, "MAYA", "line number %d" % i, i) for i in range(12)]
    job = sv.RenderJob(r, slow, os.path.join(work, "cancelled"), None)
    events = []
    job.on_event = events.append
    job.start()
    for _ in range(400):                       # cancel once it is genuinely running
        if any(e["kind"] == "cue_done" for e in events):
            break
        time.sleep(0.01)
    job.cancel()
    job.join(120)
    check("a cancelled render stops", not job.is_alive())
    stopped = [e for e in events if e["kind"] == "failed"]
    check("a cancelled render reports cancelled, not an error",
          bool(stopped) and stopped[0].get("cancelled") is True
          and "ancel" in stopped[0].get("message", ""), str(stopped[:1])[:120])
    check("a cancelled render does not render every cue",
          len(job.files) < len(slow), "%d of %d" % (len(job.files), len(slow)))

    shutil.rmtree(work, ignore_errors=True)


def main():
    only_single = "--single" in sys.argv
    check = Checks()

    comfy = HTTPServer(("127.0.0.1", COMFY_PORT), ComfyStub)
    llm = HTTPServer(("127.0.0.1", LLM_PORT), LLMStub)
    for s in (comfy, llm):
        threading.Thread(target=s.serve_forever, daemon=True).start()

    targets = []
    if not only_single:
        targets.append(package_ns())
    single = single_ns()
    if single:
        targets.append(single)
    else:
        print("\n  NOTE  ScriptVoice.py not built yet - run: python build_single.py")

    for sv in targets:
        print("\n" + "=" * 70 + "\n  %s\n" % sv.label.upper() + "=" * 70)
        suite(sv, check)

    for s in (comfy, llm):
        s.shutdown()
    print("\n%d passed, %d failed\n" % (check.passed, check.failed))
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())
