"""The AI jobs: build the cast from a premise, regenerate one actor or one
voice, and render the finished movie once the cast is approved."""

import hashlib
import json
import random
import os
import re

from . import casting, movie, project as proj, script_parser, speech, visuals
from .audio import duration
from . import comfy as comfy_mod
from .comfy import ComfyClient, ComfyError
from .jobs import AUDIO, IMAGE, SlotRunner
from . import llm as llm_mod
from .llm import LLMError, LocalLLM
from .render import RenderJob
from .worker import Worker

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
            on_frame=on_frame, reference=actor.get("portrait", ""))
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
            prompt = casting.shot_prompt(actor_map, cue, shot)
            # The seed follows the face in frame, so a character keeps their
            # look whether they are speaking or being looked at.
            subject = casting.shot_subject(shot, cue, actor_map.keys())
            actor = actor_map.get(subject) or {}
            seed = int(actor.get("look_seed", 0)) + i
            key = hashlib.sha1(("%s|%d|%s" % (prompt, seed, cue.text))
                               .encode("utf-8")).hexdigest()[:16]
            prev = manifest.get(str(i))
            self.step("Drawing shot %d of %d" % (n + 1, len(targets)), n, len(targets))
            if reuse and prev and prev.get("key") == key and os.path.exists(prev.get("file", "")):
                self.images[i] = prev["file"]
                self.emit("shot_done", index=i, total=len(self.cues), file=prev["file"],
                          cached=True, prompt=prompt)
                continue
            if self.only is not None:
                seed += random.randint(1, 10 ** 6)      # a redraw should differ
            path = runner.run({"prompt": prompt, "negative": casting.NEGATIVE, "seed": seed},
                              os.path.join(shot_dir, "%04d" % (i + 1)), IMAGE,
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
                    prompt = casting.shot_prompt(actor_map, cue, shot)
                    actor = actor_map.get(cue.speaker) or {}
                    seed = int(actor.get("look_seed", 0)) + i
                    key = hashlib.sha1(("%s|%d|%s" % (prompt, seed, cue.text))
                                       .encode("utf-8")).hexdigest()[:16]
                    prev = manifest.get(str(i))
                    self.step("Shot %d of %d" % (i + 1, len(self.cues)), i, len(self.cues))
                    if reuse and prev and prev.get("key") == key and os.path.exists(
                            prev.get("file", "")):
                        images[i] = prev["file"]
                        self.emit("shot_done", index=i, total=len(self.cues),
                                  file=prev["file"], cached=True)
                        continue
                    values = {"prompt": prompt, "negative": casting.NEGATIVE, "seed": seed}
                    if runner.has("image") and actor.get("portrait"):
                        values["image"] = runner.upload(actor["portrait"])
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
