"""Base class for the background jobs the GUI runs (render, casting, movie)."""

import threading
import traceback


class Worker(threading.Thread):
    """A cancellable job that reports progress through a single callback.

    Every callback fires on the worker thread; the GUI queues them and handles
    them on the main loop.
    """

    daemon = True
    kind = "job"

    def __init__(self, on_event):
        threading.Thread.__init__(self)
        self.on_event = on_event
        self._cancel = threading.Event()
        self.error = None
        self.result = {}

    # ----------------------------------------------------------- cancellation

    def cancel(self):
        self._cancel.set()

    def cancelled(self):
        return self._cancel.is_set()

    # --------------------------------------------------------------- reporting

    def emit(self, kind, **kw):
        kw["kind"] = kind
        kw.setdefault("job", self.kind)
        try:
            self.on_event(kw)
        except Exception:
            pass

    def log(self, msg):
        self.emit("log", message=str(msg))

    def step(self, label, done=0, total=0):
        self.emit("step", label=label, done=done, total=total)

    # -------------------------------------------------------------- execution

    def execute(self):                                    # pragma: no cover
        raise NotImplementedError

    def run(self):
        try:
            self.execute()
            self.emit("finished", **self.result)
        except Exception as e:                            # surfaced in the GUI
            if self.cancelled() or "cancel" in str(e).lower():
                self.emit("failed", message="Cancelled.", cancelled=True)
                return
            # ComfyError / LLMError messages are already written for humans
            self.error = str(e) if isinstance(e, RuntimeError) else "%s: %s" % (
                type(e).__name__, e)
            self.log(traceback.format_exc(limit=4).strip().splitlines()[-1])
            self.emit("failed", message=self.error, cancelled=False)
