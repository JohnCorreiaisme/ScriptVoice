"""Runs a parsed script through ComfyUI's TTS workflow, one cue at a time."""

import hashlib
import json
import os
import re
import wave

from . import audio, project as proj, speech
from .comfy import ComfyClient, ComfyError
from .jobs import AUDIO, SlotRunner
from .worker import Worker


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
    return speech.assign_voice(seed, hint=character.get("voice_type", ""),
                               gender=character.get("voice_gender", ""))


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
