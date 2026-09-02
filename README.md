
# ScriptVoice
Human generated text: Upload a script or put an idea in the description and the Ai will build you a script. Choose Characters to use in the story and add their details. A story board is automatically created. And then a movie is generated from that with the characters you choose. :End Human generated text.


An offline film studio for a local ComfyUI. Give it a script; it works out the cast, draws each
character, gives them a voice, storyboards every line, and cuts the result into a movie file.
Nothing leaves the machine — ComfyUI, the language model and the speech synthesis are all local.

---

## ⚠️ This is a work in progress

**It is not finished, and it is not a product.** It has been built and exercised on exactly one
machine against one screenplay. Expect rough edges, expect to have to press things twice, and
expect the parts marked "not done" below to be genuinely not done.

It is offered in case it is useful to read or to build on. If you use it, keep your own backups
of your script — the project file is the only place your edits live.

---

## What it does

Six tabs, in the order you'd actually work:

1. **Premise** — a sentence or two. The AI casts the film from it, or you skip this and load a
   script you already have.
2. **Script** — paste it, load a file, or have the AI write one. **Compile script** reads it and
   adds every speaker to the cast.
3. **Cast** — one card per character: portrait, an 8-frame turnaround you can drag to spin, a
   voice sample, and what they do in the script. Tabs beside it for the look you want, a
   **reference face** every render is conditioned on, the description (editable — the AI reads it
   back), the voice, and workflow overrides. You can mark the main character, merge two names that
   are the same person, jump to their lines in the script, and add or remove characters by hand.
4. **Storyboard** — one image per line of dialogue, drawn from the script's own scene headings,
   scene-appropriate wardrobe, and the reference face of whoever is in frame — which is often the
   listener, not the speaker. Per shot you can pick that face yourself and write your own shot
   description; both survive a replan.
5. **Movie** — records every line, cuts the stills against the audio, writes an `.mp4` and an EDL.
6. **Setup** — three pages. *Everyday*: ComfyUI's address, where voices come from, the
   words in front of every picture prompt, where files go, and freeing the GPU between
   steps. *What draws and speaks*: one visible row per job saying which workflow is
   loaded and whether it is ready. *Advanced*: every input a workflow exposes.

Two ways to run it, same program:

```bash
python ScriptVoice.py        # one self-contained file, no package needed
python run.py                # the scriptvoice/ package, for development
```

`ScriptVoice.py` really is on its own: copy that one file anywhere and it runs, with the default
picture workflow baked in at build time. Everything else in this repository is source and tests.

`ScriptVoice.py` is generated — edit `scriptvoice/*.py` and run `python build_single.py`.

---

## What has actually been tested

**Automated: 731 checks** (`python selftest.py`) — run twice, once against the package and once
against the flattened single file, so the two cannot drift apart. They use a stub ComfyUI and a
stub OpenAI-compatible model server, so they need no GPU and no network.

**Interface: 73 checks** (`python gui_selftest.py`) — drives the real Tk window: that a control
exists, is wired to the right handler, and is on screen at the default window size.

**On real hardware** — Windows 11, RTX 3060 12 GB, ComfyUI with SDXL-Turbo, LM Studio serving
Qwen2.5-Coder-7B:

| | measured |
|---|---|
| One portrait | ~4 s |
| Portrait + 8-frame turnaround | ~30 s |
| Casting pass (7B) | ~20 s |
| 202 storyboard shots | ~14 min, 118 MB |
| 202 dialogue clips (Windows SAPI) | 51 s |
| Final cut | 18 min film, 1280×720, 44.8 MB |

The output `.mp4` is decoded and correlated against its source frames as part of testing —
0.985–0.992 on a real render — because an earlier version produced eighteen minutes of static
while every metadata check passed.

### Known limitations

- **One machine, one script.** Windows 11 + Python 3.10 + LM Studio. Nothing else is tested.
- **No neural voices.** The ComfyUI install it was built against has no TTS nodes, so speech is
  Windows SAPI. It sounds like Windows SAPI. The ComfyUI voice path exists but is unexercised.
- **No ffmpeg.** Muxing goes through PyAV. `pip install av`.
- **12 GB is tight.** ComfyUI and a language model do not fit at once. *Free the GPU between
  steps* on the Setup tab evicts each before the other runs, at ~10 s per handover.
- **SDXL-Turbo takes prompt adherence only so far.** A better checkpoint would help more than any
  amount of prompt engineering.

### Not done

- **Character consistency needs a workflow that can use it.** Every shot is now conditioned on
  the character's reference face - yours if you set one, otherwise the drawn portrait - and the
  seed follows the face in frame rather than the speaker. But that only bites if your shot
  workflow has a reference-image input (IPAdapter, InstantID, PuLID). With a plain text-to-image
  graph the app says so in the log and characters still drift.
- **The turnaround is 8 separate renders, not a real 3D spin.** Angles are prompted, not modelled.
- **The film is stills cut against audio.** No motion, no camera moves.
- **No continuity between shots** beyond the scene heading — no eyelines, no consistent geography.
- Voice cloning, second-pass shot refinement, and subtitles are all unimplemented.

---

## Requirements

- **Python 3.8+** with Tkinter (standard on Windows)
- **ComfyUI** running locally, with an image workflow exported via *Workflow → Export (API)*.
  A working SDXL-Turbo graph ships in `workflows/`, and a new project adopts it automatically.
- **A local model server** speaking the OpenAI `/v1` protocol — LM Studio, Ollama, llama.cpp,
  Jan, vLLM. The app finds it, and can start LM Studio for you.
- `pip install pillow` — image previews (optional, degrades to Tk's own PNG/GIF support)
- `pip install av` — writing the `.mp4` (optional; without it you get the stills, the audio and
  an EDL)

**To keep a character's face across shots** you need a workflow with a reference-image input.
`workflows/sdxl_photomaker_reference_api.json` uses the PhotoMaker nodes ComfyUI already ships,
so no custom node packs — it wants `photomaker-v1.bin` in `models/photomaker/` and
`sd_xl_base_1.0.safetensors` in `models/checkpoints/`. PhotoMaker needs a trigger phrase at the
front of the prompt: put `a person img` in *Words in front of every picture prompt* on the Setup
tab. Measured at 16 s a shot at 768x768 on a 3060.

Nothing else. No accounts, no keys, no network calls beyond `127.0.0.1`.

---

## Layout

```
ScriptVoice.py      the whole program in one file (generated)
build_single.py     flattens scriptvoice/ into it; refuses to build on a name collision
run.py              runs the package
selftest.py         731 checks, against both the package and the single file
gui_selftest.py     73 widget checks against the real window
scriptvoice/        audio, comfy, casting, llm, movie, pipeline, project, render,
                    script_parser, speech, visuals, widgets, worker, gui
workflows/          example API-format ComfyUI workflows
```

Your projects, scripts and renders are written to an output folder you choose, and are
`.gitignore`d — they never belong in this repository.

---

## Licence

[MIT](LICENSE). Use it, change it, ship it - just keep the copyright notice.

The licence covers this software only. Anything you make with it - your script, your cast, your
renders, your film - is yours, and none of it is in this repository.
