"""Minimal ComfyUI HTTP client. Stdlib only, fully offline (localhost)."""

import hashlib
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
        with open(path, "rb") as f:
            content = f.read()
        # cast/HAROLD/portrait.png and cast/JEANNE/portrait.png share a basename,
        # so uploading by basename put every character on one file and every shot
        # was then conditioned on whoever went last. Name by content instead.
        stem, ext = os.path.splitext(os.path.basename(path))
        name = "%s_%s%s" % (stem, hashlib.sha1(content).hexdigest()[:10], ext)
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
