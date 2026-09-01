"""One queued ComfyUI prompt, for any slot (voice, portrait, turnaround, shot)."""

import os
import random

from . import project as proj
from .comfy import ComfyError

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
