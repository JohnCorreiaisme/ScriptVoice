"""Local LLM client. Speaks the OpenAI /v1 chat protocol, which LM Studio,
Ollama, llama.cpp, Jan, vLLM and text-generation-webui all serve locally.

Nothing here talks to the internet - only 127.0.0.1.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

# (label, port) pairs probed by discover(); all OpenAI-compatible on /v1.
CANDIDATES = [
    ("LM Studio", 1234),
    ("Ollama", 11434),
    ("llama.cpp / vLLM", 8080),
    ("Jan", 1337),
    ("text-generation-webui", 5000),
    ("KoboldCpp", 5001),
]


class LLMError(RuntimeError):
    pass


# LM Studio ships a command-line tool that can start its server for us, so a
# stopped server is a question to the user rather than a dead end.
LMS_PATHS = (
    os.path.join("~", ".cache", "lm-studio", "bin", "lms.exe"),
    os.path.join("~", ".lmstudio", "bin", "lms.exe"),
    os.path.join("~", ".cache", "lm-studio", "bin", "lms"),
    os.path.join("~", ".lmstudio", "bin", "lms"),
)


def find_lmstudio_cli():
    """The path to LM Studio's `lms` tool, or "" if it isn't installed."""
    for p in LMS_PATHS:
        p = os.path.expanduser(p)
        if os.path.exists(p):
            return p
    found = shutil.which("lms")
    return found or ""


def unload_lmstudio(cli="", timeout=60):
    """Unload every loaded model so the card is free to draw. False if it can't."""
    cli = cli or find_lmstudio_cli()
    if not cli:
        return False
    try:
        r = subprocess.run([cli, "unload", "--all"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           timeout=timeout)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def start_lmstudio(wait=40, host="127.0.0.1", port=1234, cli=""):
    """Start LM Studio's server and wait for it to answer. Returns a base_url.

    Raises LLMError with something the user can act on, never a raw traceback.
    """
    cli = cli or find_lmstudio_cli()
    if not cli:
        raise LLMError(
            "LM Studio's command-line tool wasn't found, so it can't be started "
            "for you.\n\nOpen LM Studio and use Developer > Start Server.")
    base = "http://%s:%d/v1" % (host, int(port))
    try:
        proc = subprocess.Popen([cli, "server", "start"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
    except OSError as e:
        raise LLMError("Couldn't run %s: %s" % (cli, e))
    deadline = time.time() + max(5, wait)
    while time.time() < deadline:
        if port_open(host, port, timeout=0.5):
            try:
                if LocalLLM(base).models():
                    return base
            except LLMError:
                pass                      # server is up, models still enumerating
        time.sleep(0.5)
    out = ""
    try:
        proc.kill()
        out = (proc.communicate(timeout=3)[0] or b"").decode("utf-8", "replace")[-300:]
    except Exception:
        pass
    raise LLMError("LM Studio didn't come up within %d seconds.\n\n%s" % (wait, out.strip()))


def port_open(host, port, timeout=0.35):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, int(port))) == 0
    finally:
        s.close()


def discover(host="127.0.0.1"):
    """Find local OpenAI-compatible servers. Returns [(label, base_url, [models])]."""
    found = []
    for label, port in CANDIDATES:
        if not port_open(host, port):
            continue
        base = "http://%s:%d/v1" % (host, port)
        try:
            models = LocalLLM(base).models()
        except LLMError:
            models = []
        found.append((label, base, models))
    return found


class LocalLLM(object):
    def __init__(self, base_url="http://127.0.0.1:1234/v1", model="", timeout=600,
                 temperature=0.8):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or ""
        self.timeout = timeout
        self.temperature = temperature

    # ---------------------------------------------------------------- plumbing

    def _request(self, path, payload=None, method="GET", timeout=None):
        url = self.base_url + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:800]
            raise LLMError("The local model server returned %s for %s:\n%s" % (e.code, path, body))
        except urllib.error.URLError as e:
            raise LLMError(
                "Couldn't reach a local model at %s (%s).\n\n"
                "Start LM Studio (Developer > Start Server) or `ollama serve`, "
                "then press Detect again." % (self.base_url, e.reason))
        except ValueError as e:
            raise LLMError("The model server sent something that isn't JSON: %s" % e)

    def models(self):
        """Chat-capable models, best first. Embedding models are dropped."""
        data = self._request("/models", timeout=5)
        ids = [m.get("id", "") for m in (data.get("data") or []) if m.get("id")]
        return rank_models(ids)

    def ready(self):
        """Return a short status string or raise LLMError."""
        ms = self.models()
        if not ms:
            raise LLMError("Server is up at %s but has no model loaded." % self.base_url)
        if not self.model:
            self.model = ms[0]
        return "%s (%d model%s available)" % (self.model, len(ms), "" if len(ms) == 1 else "s")

    # ------------------------------------------------------------------- chat

    def chat(self, messages, temperature=None, max_tokens=2048, stop=None, cancel=None):
        if not self.model:
            self.ready()
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if stop:
            payload["stop"] = stop
        if cancel is not None and cancel():
            raise LLMError("Cancelled")
        data = self._request("/chat/completions", payload, method="POST")
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise LLMError("Unexpected reply from the model: %s" % json.dumps(data)[:400])

    def chat_json(self, system, user, expect="object", retries=2, temperature=None,
                  max_tokens=2048, cancel=None):
        """Ask for JSON and actually get JSON back, even from small local models."""
        messages = [
            {"role": "system", "content": system.strip() +
             "\n\nReply with JSON only. No commentary, no markdown fences."},
            {"role": "user", "content": user.strip()},
        ]
        last = ""
        for attempt in range(retries + 1):
            raw = self.chat(messages, temperature=temperature, max_tokens=max_tokens,
                            cancel=cancel)
            last = raw
            parsed = extract_json(raw, expect)
            if parsed is not None:
                return parsed
            messages = messages[:2] + [
                {"role": "assistant", "content": raw[:1500]},
                {"role": "user", "content":
                 "That wasn't valid JSON. Reply again with a single valid JSON %s and nothing "
                 "else." % expect},
            ]
        raise LLMError(
            "The model didn't return usable JSON after %d tries. Last reply:\n\n%s"
            % (retries + 1, last[:600]))


# ------------------------------------------------------------- model selection

NOT_CHAT = ("embed", "rerank", "whisper", "clip-", "-tts", "stable-diffusion", "sdxl")
NARROW = ("coder", "code-", "-code", "codestral", "starcoder", "math", "vision", "vl-")
SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.I)


def rank_models(ids):
    """Sort a server's model list so the best casting model comes first.

    A coder or embedding model happens to be first on plenty of installs, and
    writing a cast is neither of those jobs.
    """
    def key(name):
        low = name.lower()
        if any(w in low for w in NOT_CHAT):
            return None                       # can't hold a conversation at all
        narrow = any(w in low for w in NARROW)
        m = SIZE.search(low.replace("-", " "))
        billions = float(m.group(1)) if m else 0.0
        return (narrow, -billions, low)       # general first, then biggest

    scored = [(key(n), n) for n in ids]
    return [n for k, n in sorted((s for s in scored if s[0] is not None))]


# ------------------------------------------------------------------ json repair

FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
TRAILING_COMMA = re.compile(r",\s*([}\]])")
THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def extract_json(text, expect="object"):
    """Pull the first JSON object/array out of a model reply, or None."""
    if not text:
        return None
    text = THINK.sub(" ", text)
    m = FENCE.search(text)
    if m:
        text = m.group(1)
    opener, closer = ("{", "}") if expect == "object" else ("[", "]")

    candidates = []
    start = text.find(opener)
    if start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    candidates.append(text.strip())

    for cand in candidates:
        for attempt in (cand, TRAILING_COMMA.sub(r"\1", cand)):
            try:
                val = json.loads(attempt)
            except ValueError:
                continue
            if expect == "object" and isinstance(val, dict):
                return val
            if expect == "array" and isinstance(val, list):
                return val
            if expect == "array" and isinstance(val, dict):
                for key in ("characters", "cast", "actors", "items", "results", "shots"):
                    if isinstance(val.get(key), list):
                        return val[key]
    return None
