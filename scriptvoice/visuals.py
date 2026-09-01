"""Actor visuals: the portrait, and the full spin-around turnaround."""

import os
import re

from . import casting
from .jobs import IMAGE

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


def render_portrait(runner, actor, out_dir, cancel=None, prefix=""):
    """One locked portrait: same appearance words + same look seed every time."""
    d = actor_dir(out_dir, actor["name"])
    return runner.run(
        {"prompt": casting.with_prefix(casting.look_prompt(actor), prefix),
         "negative": casting.NEGATIVE,
         "seed": actor.get("look_seed", -1)},
        os.path.join(d, "portrait"), IMAGE, cancel=cancel)


def render_turnaround(runner, actor, out_dir, frames=8, cancel=None, on_frame=None,
                      reference="", prefix=""):
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

    prompts = [(deg, casting.with_prefix(pr, prefix)) for deg, pr in prompts]
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
