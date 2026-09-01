"""Reusable Tkinter pieces: image loading, a scrolling frame, the turnaround
viewer, and the actor card with its regenerate buttons."""

import os
import tkinter as tk
from tkinter import ttk

try:
    from PIL import Image, ImageTk
    HAVE_PIL = True
except Exception:                       # pragma: no cover - Pillow is optional
    HAVE_PIL = False

PLACEHOLDER = "#2c2f36"


def load_photo(path, max_w=220, max_h=280):
    """A Tk image scaled to fit, or None. Uses Pillow when it's installed."""
    if not path or not os.path.exists(path):
        return None
    try:
        if HAVE_PIL:
            im = Image.open(path)
            if getattr(im, "is_animated", False):
                im.seek(0)
            im = im.convert("RGB")
            im.thumbnail((max_w, max_h))
            return ImageTk.PhotoImage(im)
        photo = tk.PhotoImage(file=path)          # Tk 8.6 reads PNG and GIF
        factor = max(1, int(max(photo.width() / float(max_w),
                                photo.height() / float(max_h)) + 0.999))
        return photo.subsample(factor, factor) if factor > 1 else photo
    except Exception:
        return None


class ScrollFrame(ttk.Frame):
    """A vertically scrolling container. Put your widgets in `.body`."""

    def __init__(self, master, **kw):
        ttk.Frame.__init__(self, master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.sb.pack(side="right", fill="y")
        self.body = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", lambda e: self._wheel(True))
        self.canvas.bind("<Leave>", lambda e: self._wheel(False))

    def _on_body(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)

    def _wheel(self, on):
        if on:
            self.canvas.bind_all("<MouseWheel>", self._scroll)
        else:
            self.canvas.unbind_all("<MouseWheel>")

    def _scroll(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120), "units")

    def clear(self):
        for child in self.body.winfo_children():
            child.destroy()


class SpinViewer(ttk.Frame):
    """The 360 turnaround: drag to spin, or let it turn on its own."""

    def __init__(self, master, size=(320, 380), **kw):
        ttk.Frame.__init__(self, master, **kw)
        self.size = size
        self.frames = []          # file paths
        self.photos = []          # Tk images, kept alive
        self.index = 0
        self.playing = False
        self._after = None
        self._drag_x = None
        self.title = tk.StringVar(value="No turnaround yet")

        ttk.Label(self, textvariable=self.title, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.canvas = tk.Canvas(self, width=size[0], height=size[1],
                                background=PLACEHOLDER, highlightthickness=0)
        self.canvas.pack(pady=4)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag_x", None))

        row = ttk.Frame(self)
        row.pack(fill="x")
        self.btn_play = ttk.Button(row, text="Spin", width=7, command=self.toggle)
        self.btn_play.pack(side="left")
        ttk.Button(row, text="<", width=3, command=lambda: self.nudge(-1)).pack(side="left", padx=(6, 0))
        ttk.Button(row, text=">", width=3, command=lambda: self.nudge(1)).pack(side="left")
        self.pos = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.pos, foreground="#666").pack(side="left", padx=8)
        self._empty()

    # -------------------------------------------------------------- content

    def set_frames(self, paths, title=""):
        self.stop()
        self.frames = [p for p in (paths or []) if p and os.path.exists(p)]
        self.photos = [load_photo(p, self.size[0], self.size[1]) for p in self.frames]
        self.photos = [p for p in self.photos if p is not None]
        self.frames = self.frames[:len(self.photos)]
        self.index = 0
        self.title.set(title or ("%d frames" % len(self.frames)))
        if self.frames:
            self._draw()
            if len(self.frames) > 2:
                self.play()
        else:
            self._empty()

    def _empty(self):
        self.canvas.delete("all")
        self.canvas.create_text(self.size[0] // 2, self.size[1] // 2, fill="#8a8f99",
                                width=self.size[0] - 40, justify="center",
                                text="No turnaround yet.\n\nCreate the cast, or press\n"
                                     "New look on an actor.")
        self.pos.set("")

    def _draw(self):
        if not self.photos:
            return self._empty()
        self.index %= len(self.photos)
        photo = self.photos[self.index]
        self.canvas.delete("all")
        self.canvas.create_image(self.size[0] // 2, self.size[1] // 2, image=photo)
        self.pos.set("%d / %d" % (self.index + 1, len(self.photos)))

    # --------------------------------------------------------------- motion

    def nudge(self, step):
        if self.photos:
            self.index = (self.index + step) % len(self.photos)
            self._draw()

    def toggle(self):
        self.stop() if self.playing else self.play()

    def play(self):
        if len(self.photos) < 2:
            return
        self.playing = True
        self.btn_play.configure(text="Stop")
        self._tick()

    def stop(self):
        self.playing = False
        self.btn_play.configure(text="Spin")
        if self._after:
            try:
                self.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _tick(self):
        if not self.playing:
            return
        self.nudge(1)
        self._after = self.after(140, self._tick)

    def _press(self, e):
        self.stop()
        self._drag_x = e.x

    def _drag(self, e):
        if self._drag_x is None or not self.photos:
            return
        step = int((e.x - self._drag_x) / 18)
        if step:
            self.index = (self.index - step) % len(self.photos)
            self._drag_x = e.x
            self._draw()


ROLE_COLOR = "#1a4f9c"


class ActorCard(ttk.Frame):
    """One actor: portrait, who they are, what they do in the script, and the
    regenerate buttons for the actor and for the voice."""

    def __init__(self, master, actor, on_action, on_select, **kw):
        ttk.Frame.__init__(self, master, padding=8, relief="groove", borderwidth=1, **kw)
        self.actor = actor
        self.on_action = on_action
        self.name = actor["name"]
        self._photo = None

        self.thumb = tk.Canvas(self, width=120, height=150, background=PLACEHOLDER,
                               highlightthickness=0, cursor="hand2")
        self.thumb.grid(row=0, column=0, rowspan=3, sticky="nw")
        self.thumb.bind("<Button-1>", lambda e: on_select(self.name))

        head = ttk.Frame(self)
        head.grid(row=0, column=1, sticky="ew", padx=10)
        self.columnconfigure(1, weight=1)
        self.v_title = tk.StringVar()
        ttk.Label(head, textvariable=self.v_title, font=("Segoe UI", 11, "bold")).pack(side="left")
        self.v_where = tk.StringVar()
        self.lbl_where = ttk.Label(head, textvariable=self.v_where,
                                   font=("Segoe UI", 9), foreground=ROLE_COLOR)
        self.lbl_where.pack(side="left", padx=10)

        self.v_body = tk.StringVar()
        self.lbl_body = ttk.Label(self, textvariable=self.v_body, justify="left",
                                  wraplength=560, foreground="#333")
        self.lbl_body.grid(row=1, column=1, sticky="ew", padx=10, pady=(2, 6))
        # A fixed wrap width clipped the judgement text whenever the cast pane was
        # narrower than 560px, so wrap to whatever width the card actually has.
        self.bind("<Configure>", self._rewrap)

        btns = ttk.Frame(self)
        btns.grid(row=2, column=1, sticky="w", padx=10)
        self._build_progress()
        self.v_approved = tk.BooleanVar(value=bool(actor.get("approved")))
        ttk.Checkbutton(btns, text="Approved", variable=self.v_approved,
                        command=lambda: on_action(self.name, "approve", self.v_approved.get())
                        ).pack(side="left", padx=(0, 10))
        self.v_lead = tk.BooleanVar(value=bool(actor.get("lead")))
        ttk.Checkbutton(btns, text="Main character", variable=self.v_lead,
                        command=lambda: on_action(self.name, "lead", self.v_lead.get())
                        ).pack(side="left", padx=(0, 12))
        for label, action, hint in (
                ("Regenerate actor", "actor", "a different person in the same role"),
                ("Regenerate voice", "voice", "a different voice, same person"),
                ("New look", "look", "same person, new render"),
                ("Play voice", "play", "hear the sample line"),
                ("Find in script", "find", "jump to their lines"),
                ("Same as...", "same", "this is really another character"),
                ("Remove", "remove", "take them out of the cast")):
            ttk.Button(btns, text=label, command=lambda a=action: on_action(self.name, a, None)
                       ).pack(side="left", padx=2)
        self.refresh(actor)

    def _rewrap(self, event):
        # 120 thumbnail + 10 padx either side + the card's own border
        width = max(200, event.width - 160)
        if abs(width - int(self.lbl_body.cget("wraplength") or 0)) > 8:
            self.lbl_body.configure(wraplength=width)

    # ------------------------------------------------------------- progress

    def _build_progress(self):
        """A bar under the buttons, shown only while this actor is being made.

        Drawing a portrait and a turnaround is minutes of silent GPU work, so
        without this the only feedback is the fans.
        """
        self.prog_row = ttk.Frame(self)
        self.prog_row.grid(row=3, column=1, sticky="ew", padx=10, pady=(6, 0))
        self.prog_row.columnconfigure(1, weight=1)
        self.prog = ttk.Progressbar(self.prog_row, length=180, mode="determinate")
        self.prog.grid(row=0, column=0, sticky="w")
        self.v_prog = tk.StringVar(value="")
        ttk.Label(self.prog_row, textvariable=self.v_prog,
                  foreground="#1a4f9c").grid(row=0, column=1, sticky="w", padx=8)
        self._prog_mode = ""
        self.prog_row.grid_remove()

    def set_progress(self, label, done=0, total=0):
        """Show the bar. `total` of 0 means "no idea how long" - march instead."""
        if not label:
            return self.clear_progress()
        self.v_prog.set(label)
        want = "determinate" if total else "indeterminate"
        if want != self._prog_mode:
            if self._prog_mode == "indeterminate":
                self.prog.stop()
            self.prog.configure(mode=want)
            self._prog_mode = want
            if want == "indeterminate":
                self.prog.start(60)
        if total:
            self.prog.configure(maximum=total, value=min(done, total))
        self.prog_row.grid()

    def clear_progress(self):
        if self._prog_mode == "indeterminate":
            self.prog.stop()
        self._prog_mode = ""
        self.prog.configure(mode="determinate", value=0)
        self.v_prog.set("")
        self.prog_row.grid_remove()

    def refresh(self, actor):
        self.actor = actor
        role = "lead" if actor.get("lead") else actor.get("role", "")
        self.v_title.set("%s   (%s)" % (actor["name"], role) if role else actor["name"])
        said = int(actor.get("line_count", 0) or 0)
        self.v_where.set(("%d line%s in the script" % (said, "" if said == 1 else "s"))
                         if said else "")
        lines = []
        if actor.get("aliases"):
            lines.append("Also called: %s" % ", ".join(actor["aliases"]))
        if actor.get("one_line"):
            lines.append(actor["one_line"])
        if actor.get("look_note"):
            lines.append("Look you asked for: %s" % actor["look_note"])
        voice = ", ".join(x for x in (actor.get("voice_type", ""),
                                      actor.get("voice_direction", "")) if x)
        if voice:
            lines.append("Voice: %s" % voice)
        if actor.get("sample_line"):
            lines.append("“%s”" % actor["sample_line"])
        marks = []
        if actor.get("portrait"):
            marks.append("portrait")
        if actor.get("turnaround"):
            marks.append("%d-frame spin" % len(actor["turnaround"]))
        if actor.get("reference_image"):
            marks.append("your reference picture")
        if actor.get("voice_sample"):
            marks.append("voice sample")
        lines.append("Files: %s" % (", ".join(marks) if marks else "none yet"))
        self.v_body.set("\n".join(lines))
        self.v_approved.set(bool(actor.get("approved")))
        self.v_lead.set(bool(actor.get("lead")))

        self._photo = load_photo(actor.get("portrait", "")
                                 or (actor.get("turnaround") or [""])[0], 120, 150)
        self.thumb.delete("all")
        if self._photo:
            self.thumb.create_image(60, 75, image=self._photo)
        else:
            self.thumb.create_text(60, 75, text="no\nportrait", fill="#8a8f99",
                                   justify="center")
