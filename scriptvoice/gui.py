"""ScriptVoice - an offline GUI that casts a film with a local LLM, renders the
actors and their voices through ComfyUI, and cuts the finished movie."""

import json
import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

from . import casting, llm as llm_mod, movie as movie_mod, project as proj
from . import script_parser, speech, visuals
from . import comfy as comfy_mod
from .comfy import ComfyClient, ComfyError
from .jobs import AUDIO, SlotRunner
from .pipeline import CastJob, MovieJob, RegenerateJob, StoryboardJob
from .render import RenderJob, system_voice
from .widgets import ActorCard, ScrollFrame, SpinViewer, load_photo
from .worker import Worker

APP = "ScriptVoice"
PROJ_EXT = ".svproj"

SAMPLE_PREMISE = (
    "A decommissioned lighthouse keeper and a marine biologist are trapped on the same "
    "island the night the light comes back on by itself.")

SAMPLE_SCRIPT = """\
# Lines starting with # are notes and are never spoken.
# Write either "NAME: dialogue" or screenplay style (NAME on its own line).

NARRATOR: The lighthouse had been dark for eleven years.

MAYA: You said the generator still worked.

RUBEN
It did. In 2014. (shrugs) Give it a minute.

MAYA: We don't have a minute.
"""


class App(ttk.Frame):
    def __init__(self, master, reopen=True):
        ttk.Frame.__init__(self, master, padding=0)
        self.pack(fill="both", expand=True)
        self.master.title(APP)
        self.master.geometry("1240x820")
        self.master.minsize(1040, 700)

        self.project = proj.new_project()
        self._adopted = proj.adopt_default_workflows(self.project)
        self.project["premise"] = SAMPLE_PREMISE
        self.project["script"] = SAMPLE_SCRIPT
        self.project_path = ""
        self.workflows = {}          # slot -> loaded workflow dict
        self.cues = []
        self.job = None
        self.events = queue.Queue()
        self.cards = {}
        self.selected_actor = ""
        self.dirty = False
        self._servers = []

        fit_rows_to_font(master)
        self._build_menu()
        self._build_body()
        self._load_project_into_ui()
        self.after(120, self._drain_events)
        self.master.protocol("WM_DELETE_WINDOW", self._on_close)
        if reopen:
            # after the window exists, so a failure shows in the status bar
            self.after(80, self._reopen_last_project)

    # ------------------------------------------------------------------ chrome

    def _build_menu(self):
        m = tk.Menu(self.master)
        f = tk.Menu(m, tearoff=0)
        f.add_command(label="New project", accelerator="Ctrl+N", command=self.new_project)
        f.add_command(label="Open project...", accelerator="Ctrl+O", command=self.open_project)
        f.add_command(label="Save", accelerator="Ctrl+S", command=self.save_project)
        f.add_command(label="Save as...", command=self.save_project_as)
        f.add_separator()
        f.add_command(label="Import script from .txt...", command=self.import_script)
        f.add_command(label="Open output folder", command=self.open_output_folder)
        f.add_separator()
        f.add_command(label="Quit", command=self._on_close)
        m.add_cascade(label="File", menu=f)

        h = tk.Menu(m, tearoff=0)
        h.add_command(label="How this works", command=self.show_help)
        m.add_cascade(label="Help", menu=h)
        self.master.config(menu=m)

        self.master.bind("<Control-n>", lambda e: self.new_project())
        self.master.bind("<Control-o>", lambda e: self.open_project())
        self.master.bind("<Control-s>", lambda e: self.save_project())
        self.master.bind("<F5>", lambda e: self.make_movie())

    def _build_body(self):
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.tab_premise = ttk.Frame(self.nb, padding=10)
        self.tab_cast = ttk.Frame(self.nb, padding=10)
        self.tab_script = ttk.Frame(self.nb, padding=10)
        self.tab_board = ttk.Frame(self.nb, padding=10)
        self.tab_wf = ttk.Frame(self.nb, padding=10)
        self.tab_movie = ttk.Frame(self.nb, padding=10)
        for frame, label in ((self.tab_premise, "  1. Premise  "),
                             (self.tab_script, "  2. Script  "),
                             (self.tab_cast, "  3. Cast  "),
                             (self.tab_board, "  4. Storyboard  "),
                             (self.tab_movie, "  5. Movie  "),
                             (self.tab_wf, "  6. Setup  ")):
            self.nb.add(frame, text=label)
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._build_premise_tab()
        self._build_cast_tab()
        self._build_script_tab()
        self._build_board_tab()
        self._build_workflow_tab()
        self._build_movie_tab()

        bar = ttk.Frame(self, padding=(10, 6))
        bar.pack(fill="x")
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(bar, textvariable=self.status, anchor="w").pack(side="left", fill="x", expand=True)
        self.conn = tk.StringVar(value="ComfyUI: not checked   |   model: not checked")
        ttk.Label(bar, textvariable=self.conn, anchor="e").pack(side="right")

    def _reopen_last_project(self):
        """Load the project that was open last time, if it is still there.

        Never fatal: a project that has moved, or one this version cannot read,
        leaves you on the sample rather than on an error at startup.
        """
        path = proj.last_project()
        if not path:
            return
        try:
            self.project = proj.load(path)
        except Exception as e:
            self.set_status("Couldn't reopen %s: %s" % (os.path.basename(path), e))
            return
        self.project_path = path
        proj.remember_project(path)
        adopted = proj.adopt_default_workflows(self.project)
        self._load_project_into_ui()
        self.dirty = bool(adopted)
        self._retitle()
        self.set_status("Opened %s (the project you had last)."
                        % os.path.basename(path))

    # ------------------------------------------------------------ premise tab

    def _build_premise_tab(self):
        t = self.tab_premise
        ttk.Label(t, text="What is the film about?",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(t, foreground="#666", text=
                  "One or two sentences. The AI reads this, decides who the story needs, "
                  "and locks each character down so they stay the same person throughout."
                  ).pack(anchor="w", pady=(0, 8))

        wrap = ttk.Frame(t)
        wrap.pack(fill="both", expand=True)
        self.premise_text = tk.Text(wrap, wrap="word", height=8, undo=True,
                                    font=("Segoe UI", 11), padx=10, pady=8)
        sb = ttk.Scrollbar(wrap, command=self.premise_text.yview)
        self.premise_text.configure(yscrollcommand=sb.set)
        self.premise_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.premise_text.bind("<<Modified>>", self._on_premise_modified)

        box = ttk.LabelFrame(t, text="Local model (LM Studio, Ollama, llama.cpp...)", padding=10)
        box.pack(fill="x", pady=(12, 0))
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="Server:").grid(row=0, column=0, sticky="w")
        self.v_llm_url = tk.StringVar()
        self.cb_llm = ttk.Combobox(box, textvariable=self.v_llm_url, width=46)
        self.cb_llm.grid(row=0, column=1, sticky="ew", padx=6)
        self.cb_llm.bind("<<ComboboxSelected>>", lambda e: self.load_models())
        ttk.Button(box, text="Detect", command=self.detect_llm).grid(row=0, column=2)
        ttk.Label(box, text="Model:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.v_llm_model = tk.StringVar()
        self.cb_model = ttk.Combobox(box, textvariable=self.v_llm_model, width=46)
        self.cb_model.grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(box, text="Refresh", command=self.load_models).grid(row=1, column=2, pady=(6, 0))

        opts = ttk.Frame(box)
        opts.grid(row=2, column=1, sticky="w", padx=6, pady=(8, 0))
        self.v_max_actors = tk.StringVar()
        self.v_frames = tk.StringVar()
        self.v_scenes = tk.StringVar()
        ttk.Label(opts, text="Max characters:").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=8, width=4, textvariable=self.v_max_actors).pack(side="left", padx=(4, 14))
        ttk.Label(opts, text="Turnaround frames:").pack(side="left")
        ttk.Spinbox(opts, from_=2, to=24, width=4, textvariable=self.v_frames).pack(side="left", padx=(4, 14))
        ttk.Label(opts, text="Scenes to write:").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=20, width=4, textvariable=self.v_scenes).pack(side="left", padx=4)

        row = ttk.Frame(t)
        row.pack(fill="x", pady=(12, 0))
        self.btn_cast = ttk.Button(row, text="Create the cast  \u2192",
                                   command=self.create_cast)
        self.btn_cast.pack(side="left")
        ttk.Button(row, text="Cast without pictures", command=lambda: self.create_cast(
            steps=("cast", "roles", "voice"))).pack(side="left", padx=6)
        ttk.Button(row, text="Describe everyone's role", command=lambda: self.create_cast(
            steps=("roles",))).pack(side="left", padx=6)
        ttk.Label(row, foreground="#666",
                  text="   invents the characters, says what each one does in the script, "
                       "then renders their look, their spin and their voice").pack(side="left")

    def _on_premise_modified(self, _e=None):
        if self.premise_text.edit_modified():
            self.dirty = True
            self.premise_text.edit_modified(False)

    # --------------------------------------------------------------- cast tab

    def _build_cast_tab(self):
        t = self.tab_cast
        head = ttk.Frame(t)
        head.pack(fill="x")
        self.v_cast_summary = tk.StringVar(value="No cast yet.")
        ttk.Label(head, textvariable=self.v_cast_summary,
                  font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(head, text="Approve all", command=self.approve_all).pack(side="right")
        ttk.Button(head, text="Add a character...",
                   command=self.add_character).pack(side="right", padx=6)

        panes = ttk.Panedwindow(t, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=(8, 0))

        left = ttk.Frame(panes)
        self.cast_scroll = ScrollFrame(left)
        self.cast_scroll.pack(fill="both", expand=True)
        panes.add(left, weight=3)

        # This column holds the turnaround, the voice panel, the look box and the
        # workflow overrides - together taller than most windows, so it scrolls.
        right_outer = ttk.Frame(panes, padding=(10, 0, 0, 0))
        self.spin = SpinViewer(right_outer, size=(300, 330))
        self.spin.pack(anchor="n")
        # Everything about the selected actor, one click apart. Stacking these
        # pushed the lower ones off the bottom of the window as they grew.
        self.cast_side = ttk.Notebook(right_outer)
        self.cast_side.pack(fill="both", expand=True, pady=(8, 0))
        right = right_outer

        lbox = ttk.Frame(self.cast_side, padding=8)
        self.cast_side.add(lbox, text="  Look  ")
        self.look_text = tk.Text(lbox, height=3, wrap="word", font=("Segoe UI", 9))
        self.look_text.pack(fill="x")
        ttk.Label(lbox, foreground="#666",
                  text="e.g. bald, heavy jaw, broken nose, dockworker build. These words "
                       "lead the image prompt and the AI never rewrites them."
                  ).pack(anchor="w", pady=(4, 0))
        lrow = ttk.Frame(lbox)
        lrow.pack(fill="x", pady=(6, 0))
        ttk.Button(lrow, text="Save the look", command=self.save_look).pack(side="left")
        ttk.Button(lrow, text="Save and redraw",
                   command=self.save_look_and_redraw).pack(side="left", padx=6)

        rbox = ttk.Frame(self.cast_side, padding=8)
        self.cast_side.add(rbox, text="  Reference face  ")
        rrow = ttk.Frame(rbox)
        rrow.pack(fill="x")
        self.v_reference = tk.StringVar()
        ttk.Entry(rrow, textvariable=self.v_reference).pack(side="left", fill="x", expand=True)
        ttk.Button(rrow, text="...", width=3,
                   command=self.pick_reference).pack(side="left", padx=4)
        ttk.Button(rrow, text="Clear", width=6,
                   command=self.clear_reference).pack(side="left")
        self.v_reference_note = tk.StringVar(value="")
        ttk.Label(rbox, textvariable=self.v_reference_note, foreground="#666",
                  wraplength=360, justify="left").pack(anchor="w", pady=(4, 0))

        wbox = ttk.Frame(self.cast_side, padding=8)
        self.cast_side.add(wbox, text="  Who they are  ")
        self.who_text = tk.Text(wbox, height=3, wrap="word", font=("Segoe UI", 9))
        self.who_text.pack(fill="x")
        ttk.Label(wbox, foreground="#666",
                  text="One or two sentences. The judge, the shot planner and every "
                       "recast read this, so correcting it here corrects the film."
                  ).pack(anchor="w", pady=(4, 0))
        wrow = ttk.Frame(wbox)
        wrow.pack(fill="x", pady=(6, 0))
        ttk.Button(wrow, text="Save", command=self.save_who).pack(side="left")
        ttk.Button(wrow, text="Save and describe their role",
                   command=self.save_who_and_judge).pack(side="left", padx=6)
        ttk.Button(wrow, text="Split off a name...",
                   command=self.split_character).pack(side="left", padx=6)

        vbox = ttk.Frame(self.cast_side, padding=8)
        self.cast_side.add(vbox, text="  Voice  ")
        vbox.columnconfigure(1, weight=1)
        self.v_sel_name = tk.StringVar(value="(none selected)")
        ttk.Label(vbox, textvariable=self.v_sel_name,
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(vbox, text="Reference clip:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.v_voice_file = tk.StringVar()
        ttk.Entry(vbox, textvariable=self.v_voice_file, width=26).grid(
            row=1, column=1, sticky="ew", padx=4, pady=(6, 0))
        ttk.Button(vbox, text="...", width=3, command=self.pick_voice_file).grid(
            row=1, column=2, pady=(6, 0))
        ttk.Label(vbox, text="Preset / speaker:").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.v_voice_value = tk.StringVar()
        ttk.Entry(vbox, textvariable=self.v_voice_value).grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=4, pady=(4, 0))
        ttk.Label(vbox, text="Voice seed:").grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.v_seed = tk.StringVar()
        ttk.Entry(vbox, textvariable=self.v_seed, width=12).grid(
            row=3, column=1, sticky="w", padx=4, pady=(4, 0))
        ttk.Label(vbox, text="Voice gender:").grid(row=3, column=2, sticky="e", pady=(4, 0))
        self.v_voice_gender = tk.StringVar()
        ttk.Combobox(vbox, textvariable=self.v_voice_gender, state="readonly", width=10,
                     values=["Auto", "Male", "Female"]).grid(
                         row=3, column=3, sticky="w", padx=4, pady=(4, 0))

        ttk.Label(vbox, text="Windows voice:").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.v_sys_voice = tk.StringVar()
        self.cb_sys_voice = ttk.Combobox(vbox, textvariable=self.v_sys_voice,
                                         state="readonly", width=24)
        self.cb_sys_voice.grid(row=4, column=1, columnspan=2, sticky="ew", padx=4, pady=(8, 0))
        sysrow = ttk.Frame(vbox)
        sysrow.grid(row=5, column=1, columnspan=2, sticky="w", padx=4, pady=(4, 0))
        self.v_sys_rate = tk.StringVar()
        self.v_sys_pitch = tk.StringVar()
        ttk.Label(sysrow, text="rate").pack(side="left")
        ttk.Spinbox(sysrow, from_=-10, to=10, width=4,
                    textvariable=self.v_sys_rate).pack(side="left", padx=(2, 8))
        ttk.Label(sysrow, text="pitch %").pack(side="left")
        ttk.Spinbox(sysrow, from_=-50, to=50, increment=5, width=4,
                    textvariable=self.v_sys_pitch).pack(side="left", padx=2)
        self.v_sys_note = tk.StringVar(value="")
        ttk.Label(vbox, textvariable=self.v_sys_note, foreground="#666").grid(
            row=6, column=1, columnspan=2, sticky="w", padx=4)

        brow = ttk.Frame(vbox)
        brow.grid(row=7, column=1, columnspan=2, sticky="w", padx=4, pady=(8, 0))
        ttk.Button(brow, text="Save", command=self.save_voice_details).pack(side="left")
        ttk.Button(brow, text="Hear it", command=self.preview_system_voice).pack(side="left", padx=6)
        self.v_hear_note = tk.StringVar(value="")
        ttk.Label(brow, textvariable=self.v_hear_note,
                  foreground="#666").pack(side="left", padx=6)

        pbox = ttk.Frame(self.cast_side, padding=8)
        self.cast_side.add(pbox, text="  Advanced  ")
        self.param_tree = ttk.Treeview(pbox, columns=("value",), show="tree headings", height=4)
        self.param_tree.heading("#0", text="Input")
        self.param_tree.heading("value", text="Value")
        self.param_tree.column("#0", width=220)
        self.param_tree.column("value", width=80)
        self.param_tree.pack(fill="both", expand=True)
        prow = ttk.Frame(pbox)
        prow.pack(fill="x", pady=(4, 0))
        self.v_param_target = tk.StringVar()
        self.param_combo = ttk.Combobox(prow, textvariable=self.v_param_target,
                                        state="readonly", width=24)
        self.param_combo.pack(side="left")
        self.v_param_value = tk.StringVar()
        ttk.Entry(prow, textvariable=self.v_param_value, width=8).pack(side="left", padx=4)
        ttk.Button(prow, text="Set", width=5, command=self.set_param).pack(side="left")
        ttk.Button(prow, text="Del", width=5, command=self.remove_param).pack(side="left", padx=2)
        panes.add(right_outer, weight=2)

        foot = ttk.Frame(t)
        foot.pack(fill="x", pady=(10, 0))
        self.v_gate = tk.StringVar(value="")
        ttk.Label(foot, textvariable=self.v_gate, foreground="#666").pack(side="left")
        self.btn_movie_gate = ttk.Button(foot, text="Cast approved \u2192 draw the storyboard",
                                         command=self.make_storyboard, state="disabled")
        self.btn_movie_gate.pack(side="right")

    # ------------------------------------------------------------- script tab

    def _build_script_tab(self):
        t = self.tab_script
        head = ttk.Frame(t)
        head.pack(fill="x")
        ttk.Label(head, text='Script - "NAME: line" or screenplay format.',
                  font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(head, text="Write it with the AI", command=self.write_script).pack(side="right")

        bar = ttk.Frame(t)
        bar.pack(fill="x", pady=(8, 4))
        self.btn_compile = ttk.Button(bar, text="Compile script  \u2192  create characters",
                                      command=self.compile_script)
        self.btn_compile.pack(side="left")
        ttk.Button(bar, text="Load a script file...",
                   command=self.import_script).pack(side="left", padx=6)
        self.script_info = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.script_info,
                  foreground="#444").pack(side="left", padx=12)
        ttk.Label(t, foreground="#666",
                  text="Lines starting with # or [ are notes and are skipped. Press Compile "
                       "to read the script and add everyone who speaks to the cast."
                  ).pack(anchor="w", pady=(0, 8))

        wrap = ttk.Frame(t)
        wrap.pack(fill="both", expand=True)
        self.script_text = tk.Text(wrap, wrap="word", undo=True, font=("Consolas", 11),
                                   padx=10, pady=8)
        sb = ttk.Scrollbar(wrap, command=self.script_text.yview)
        self.script_text.configure(yscrollcommand=sb.set)
        self.script_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.script_text.bind("<<Modified>>", self._on_script_modified)

        opts = ttk.LabelFrame(t, text="Parsing", padding=8)
        opts.pack(fill="x", pady=(10, 0))
        self.v_default_speaker = tk.StringVar()
        self.v_strip = tk.BooleanVar()
        self.v_split = tk.BooleanVar()
        self.v_maxchars = tk.StringVar()
        ttk.Label(opts, text="Un-named prose is spoken by:").grid(row=0, column=0, sticky="w")
        ttk.Entry(opts, textvariable=self.v_default_speaker, width=18).grid(
            row=0, column=1, sticky="w", padx=(6, 18))
        ttk.Checkbutton(opts, text="Skip (parentheticals)", variable=self.v_strip).grid(
            row=0, column=2, sticky="w", padx=(0, 18))
        ttk.Checkbutton(opts, text="Split long lines into sentences",
                        variable=self.v_split).grid(row=0, column=3, sticky="w", padx=(0, 18))
        ttk.Label(opts, text="Max chars per chunk (0 = off):").grid(row=0, column=4, sticky="w")
        ttk.Entry(opts, textvariable=self.v_maxchars, width=6).grid(row=0, column=5, padx=6)



    def _on_script_modified(self, _e=None):
        if self.script_text.edit_modified():
            self.dirty = True
            self.script_text.edit_modified(False)

    # -------------------------------------------------------- storyboard tab

    def _build_board_tab(self):
        t = self.tab_board
        head = ttk.Frame(t)
        head.pack(fill="x")
        self.btn_board = ttk.Button(head, text="Draw the storyboard",
                                    command=self.make_storyboard)
        self.btn_board.pack(side="left")
        ttk.Button(head, text="Redraw this shot",
                   command=self.redraw_shot).pack(side="left", padx=6)
        self.v_board_info = tk.StringVar(value="No storyboard yet.")
        ttk.Label(head, textvariable=self.v_board_info,
                  foreground="#444").pack(side="left", padx=12)
        self.btn_board_movie = ttk.Button(head, text="Make the movie  \u2192",
                                          command=self.make_movie, state="disabled")
        self.btn_board_movie.pack(side="right")

        ttk.Label(t, foreground="#666",
                  text="One picture per line. Click a row to see it, and use Who is in "
                       "this shot if the camera should be on someone else."
                  ).pack(anchor="w", pady=(4, 8))

        self.board_progress = ttk.Progressbar(t, mode="determinate")
        self.board_progress.pack(fill="x", pady=(0, 8))

        panes = ttk.Panedwindow(t, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes)
        self.board_tree = ttk.Treeview(
            left, columns=("speaker", "who", "line", "shot", "state"),
            show="headings", height=18)
        for col, w, a in (("speaker", 100, "w"), ("who", 100, "w"), ("line", 260, "w"),
                          ("shot", 240, "w"), ("state", 70, "center")):
            self.board_tree.heading(col, text=col.title())
            self.board_tree.column(col, width=w, anchor=a)
        bsb = ttk.Scrollbar(left, command=self.board_tree.yview)
        self.board_tree.configure(yscrollcommand=bsb.set)
        self.board_tree.pack(side="left", fill="both", expand=True)
        bsb.pack(side="right", fill="y")
        self.board_tree.bind("<<TreeviewSelect>>", self._on_board_select)
        panes.add(left, weight=3)

        right = ttk.Frame(panes, padding=(10, 0, 0, 0))
        # A smaller preview and tabbed panels below it. Stacked, these ran past
        # the bottom of the window and the description box could not be reached.
        self.board_canvas = tk.Canvas(right, width=330, height=300, background="#2c2f36",
                                      highlightthickness=0)
        self.board_canvas.pack()
        self.v_board_caption = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.v_board_caption, wraplength=340,
                  justify="left", foreground="#333").pack(anchor="w", pady=(6, 0))

        self.board_side = ttk.Notebook(right)
        self.board_side.pack(fill="both", expand=True, pady=(8, 0))

        ovr = ttk.Frame(self.board_side, padding=8)
        self.board_side.add(ovr, text="  Describe the shot  ")
        self.shot_text_box = tk.Text(ovr, height=5, wrap="word", font=("Segoe UI", 9))
        self.shot_text_box.pack(fill="x")
        ttk.Label(ovr, foreground="#666", wraplength=330, justify="left",
                  text="Replaces the AI's description for this shot, and is kept when the "
                       "storyboard is planned again. Empty it to hand the shot back. Say "
                       "how many people and how wide - names alone do not tell the "
                       "renderer to draw two bodies."
                  ).pack(anchor="w", pady=(4, 0))
        orow = ttk.Frame(ovr)
        orow.pack(fill="x", pady=(6, 0))
        ttk.Button(orow, text="Save this shot",
                   command=self.save_shot_text).pack(side="left")
        ttk.Button(orow, text="Save and redraw",
                   command=self.save_shot_text_and_redraw).pack(side="left", padx=6)

        subj = ttk.Frame(self.board_side, padding=8)
        self.board_side.add(subj, text="  Who is in it  ")
        listrow = ttk.Frame(subj)
        listrow.pack(fill="x")
        self.lb_board_cast = tk.Listbox(listrow, selectmode="extended", height=5,
                                        exportselection=False)
        self.lb_board_cast.pack(side="left", fill="x", expand=True)
        lsb = ttk.Scrollbar(listrow, command=self.lb_board_cast.yview)
        self.lb_board_cast.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")
        self.v_board_face = tk.StringVar(value="")
        ttk.Label(subj, textvariable=self.v_board_face,
                  foreground="#1a4f9c").pack(anchor="w", pady=(4, 0))
        ttk.Label(subj, foreground="#666", wraplength=330, justify="left",
                  text="Ctrl-click for more than one. The first pick holds the locked "
                       "face; the rest are described in the prompt."
                  ).pack(anchor="w", pady=(2, 0))
        srow = ttk.Frame(subj)
        srow.pack(fill="x", pady=(6, 0))
        ttk.Button(srow, text="Use these people",
                   command=self.set_shot_subject).pack(side="left")
        ttk.Button(srow, text="Use them and redraw",
                   command=self.set_shot_subject_and_redraw).pack(side="left", padx=6)
        ttk.Button(srow, text="Clear",
                   command=self.clear_shot_subject).pack(side="left")

        panes.add(right, weight=2)
        self._board_photo = None

    def _refresh_board(self):
        """Fill the shot list from the script and whatever has been drawn.

        Rebuilding drops the selection and the scroll position, so both are put
        back: redrawing one shot should leave you looking at that shot.
        """
        if not hasattr(self, "board_tree"):
            return
        keep = list(self.board_tree.selection())
        try:
            top = self.board_tree.yview()[0]
        except Exception:
            top = 0.0
        self.board_tree.delete(*self.board_tree.get_children())
        shots = self.project.get("shots") or {}
        drawn = self._drawn_shots()
        for c in self.cues:
            shot = casting.shot_text(shots.get(str(c.index)) or {})
            rec = (shots.get(str(c.index)) or {})
            crowd = casting.shot_people(rec, c, (self.project.get("characters") or {}).keys())
            who = crowd[0] if crowd else ""
            if len(crowd) > 1:
                who = "%s +%d" % (who, len(crowd) - 1)
            self.board_tree.insert("", "end", iid=str(c.index),
                                   values=(c.speaker, who, _short(c.text, 90),
                                           _short(shot, 80),
                                           "drawn" if c.index in drawn else ""))
        self.v_board_info.set("%d lines | %d drawn" % (len(self.cues), len(drawn)))
        self.btn_board_movie.configure(state="normal" if drawn else "disabled")
        still_there = [i for i in keep if self.board_tree.exists(i)]
        if still_there:
            self.board_tree.selection_set(still_there)
            self.board_tree.focus(still_there[0])
            self.board_tree.yview_moveto(top)
            self.board_tree.see(still_there[0])

    def _drawn_shots(self):
        """{cue index: image path} from the shots manifest on disk."""
        out = self.v_outdir.get() or self._default_outdir()
        found = {}
        try:
            with open(os.path.join(out, "shots", "manifest.json"), "r",
                      encoding="utf-8") as f:
                for k, v in (json.load(f) or {}).items():
                    if v.get("file") and os.path.exists(v["file"]):
                        found[int(k)] = v["file"]
        except Exception:
            pass
        return found

    def _on_board_select(self, _e=None):
        sel = self.board_tree.selection()
        if not sel:
            return
        index = int(sel[0])
        self._board_photo = load_photo(self._drawn_shots().get(index, ""), 330, 300)
        self.board_canvas.delete("all")
        if self._board_photo:
            self.board_canvas.create_image(165, 150, image=self._board_photo)
        else:
            self.board_canvas.create_text(165, 150, fill="#8a8f99", text="not drawn yet")
        if index < len(self.cues):
            cue = self.cues[index]
            shot = (self.project.get("shots") or {}).get(str(index)) or {}
            names = [a["name"] for a in proj.cast(self.project)]
            self.lb_board_cast.delete(0, "end")
            for n in names:
                self.lb_board_cast.insert("end", n)
            people = casting.shot_people(shot, cue, names)
            for n in people:
                if n in names:
                    self.lb_board_cast.selection_set(names.index(n))
            if people:
                self.lb_board_cast.see(names.index(people[0]))
            face = casting.shot_subject(shot, cue, names)
            self.v_board_face.set(
                ("Locked face: %s" % face) + ("      also in frame: %s"
                                              % ", ".join(people[1:]) if len(people) > 1 else ""))
            self.shot_text_box.delete("1.0", "end")
            self.shot_text_box.insert("1.0", shot.get("shot_override", ""))
            self.v_board_caption.set(
                "%d. %s speaks\n\n%s\n\n%s"
                % (index + 1, cue.speaker, cue.text,
                   casting.shot_text(shot) or "(no shot description yet)"))

    def save_shot_text(self):
        """Store the user's own description for the selected shot."""
        sel = self.board_tree.selection()
        if not sel:
            messagebox.showinfo(APP, "Pick a shot in the list first.")
            return False
        index = int(sel[0])
        shots = self.project.setdefault("shots", {})
        shot = shots.setdefault(str(index), {})
        typed = self.shot_text_box.get("1.0", "end-1c").strip()
        if typed:
            shot["shot_override"] = typed
        else:
            shot.pop("shot_override", None)
        if index < len(self.cues):
            shot.setdefault("line", self.cues[index].text)
        self.dirty = True
        self._refresh_board()
        self.board_tree.selection_set(sel[0])
        self.board_tree.see(sel[0])
        self.set_status("Shot %d: %s" % (index + 1, "your description saved." if typed
                                         else "back to the AI's description."))
        return True

    def save_shot_text_and_redraw(self):
        if self.save_shot_text() and not self.busy():
            self.redraw_shot()

    def set_shot_subject(self, redraw=False):
        """Pin whose face this shot draws. Overrides the AI for this shot only."""
        sel = self.board_tree.selection()
        if not sel:
            messagebox.showinfo(APP, "Pick a shot in the list first.")
            return False
        names = [a["name"] for a in proj.cast(self.project)]
        picked = [names[i] for i in self.lb_board_cast.curselection() if i < len(names)]
        if not picked:
            messagebox.showinfo(APP, "Pick at least one person for this shot.")
            return False
        index = int(sel[0])
        shots = self.project.setdefault("shots", {})
        shot = shots.setdefault(str(index), {})
        shot["cast_override"] = picked
        shot["subject_override"] = picked[0]
        who = picked[0]
        if index < len(self.cues):
            shot.setdefault("line", self.cues[index].text)
        self.dirty = True
        self._refresh_board()
        self.board_tree.selection_set(sel[0])
        self.board_tree.see(sel[0])
        self.set_status("Shot %d: %s%s." % (
            index + 1, who,
            (" with " + ", ".join(picked[1:])) if len(picked) > 1 else " alone"))
        return True

    def clear_shot_subject(self):
        """Hand the choice of who is in this shot back to the AI."""
        sel = self.board_tree.selection()
        if not sel:
            messagebox.showinfo(APP, "Pick a shot in the list first.")
            return False
        index = int(sel[0])
        shot = (self.project.get("shots") or {}).get(str(index))
        if not shot:
            return False
        had = bool(shot.get("cast_override") or shot.get("subject_override"))
        shot.pop("cast_override", None)
        shot.pop("subject_override", None)
        self.dirty = True
        self._refresh_board()
        self.board_tree.selection_set(sel[0])
        self.board_tree.see(sel[0])
        self.set_status("Shot %d: %s" % (
            index + 1, "back to whoever the AI puts in it." if had
            else "nothing was pinned on it."))
        return True

    def set_shot_subject_and_redraw(self):
        if self.set_shot_subject() and not self.busy():
            self.redraw_shot()

    def make_storyboard(self):
        cues = self.scan_script()
        if not cues:
            messagebox.showinfo(APP, "Write or generate a script first.")
            self.nb.select(self.tab_script)
            return
        p = self._collect_ui_into_project()
        if not proj.workflow_cfg(p, "shot").get("path"):
            messagebox.showinfo(APP, "Set a picture workflow for 'shot' on the Setup tab first.")
            self.nb.select(self.tab_wf)
            return
        out = p["options"]["output_dir"] or self._default_outdir()
        self.v_outdir.set(out)
        self._refresh_board()
        self.board_progress.configure(maximum=len(cues), value=0)
        self.nb.select(self.tab_board)
        self._log_clear()
        self._start(StoryboardJob(p, cues, out, self.events.put), "Drawing the storyboard...")

    def redraw_shot(self):
        sel = self.board_tree.selection()
        if not sel:
            messagebox.showinfo(APP, "Pick a shot in the list first.")
            return
        p = self._collect_ui_into_project()
        out = p["options"]["output_dir"] or self._default_outdir()
        index = int(sel[0])
        self.board_progress.configure(maximum=1, value=0)
        self._start(StoryboardJob(p, self.cues, out, self.events.put, only=[index]),
                    "Redrawing shot %d..." % (index + 1))

    # ----------------------------------------------------------- workflow tab

    def _build_workflow_tab(self):
        """Everything you might want to change, grouped the way you'd look for it."""
        t = self.tab_wf
        book = ttk.Notebook(t)
        book.pack(fill="both", expand=True)
        self.setup_book = book

        basic = ttk.Frame(book, padding=10)
        book.add(basic, text="  Everyday  ")
        wfpage = ttk.Frame(book, padding=10)
        book.add(wfpage, text="  What draws and speaks  ")
        adv = ttk.Frame(book, padding=10)
        book.add(adv, text="  Advanced  ")

        # ---------------------------------------------------- Everyday page
        srv = ttk.LabelFrame(basic, text="1. ComfyUI", padding=10)
        srv.pack(fill="x")
        self.v_host = tk.StringVar()
        self.v_port = tk.StringVar()
        ttk.Label(srv, text="Address:").pack(side="left")
        ttk.Entry(srv, textvariable=self.v_host, width=14).pack(side="left", padx=(4, 2))
        ttk.Label(srv, text=":").pack(side="left")
        ttk.Entry(srv, textvariable=self.v_port, width=7).pack(side="left", padx=2)
        ttk.Button(srv, text="Test connection",
                   command=self.test_connection).pack(side="left", padx=10)
        ttk.Button(srv, text="Open ComfyUI", command=self.open_comfy).pack(side="left")

        vb = ttk.LabelFrame(basic, text="2. Where the voices come from", padding=10)
        vb.pack(fill="x", pady=(10, 0))
        self.v_backend = tk.StringVar()
        self.backend_labels = {
            "A ComfyUI text-to-speech workflow": "comfyui",
            "The voices built into Windows": "system",
        }
        cb = ttk.Combobox(vb, textvariable=self.v_backend, state="readonly", width=36,
                          values=list(self.backend_labels))
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._backend_changed())
        self.v_backend_note = tk.StringVar(value="")
        ttk.Label(vb, textvariable=self.v_backend_note,
                  foreground="#666").pack(side="left", padx=10)

        pb = ttk.LabelFrame(basic, text="3. Words in front of every picture prompt", padding=10)
        pb.pack(fill="x", pady=(10, 0))
        self.v_prefix = tk.StringVar()
        ttk.Entry(pb, textvariable=self.v_prefix, width=36).pack(side="left")
        ttk.Label(pb, foreground="#666", wraplength=520, justify="left",
                  text="Identity workflows like PhotoMaker need a trigger phrase here - "
                       "\"a person img\". Leave it empty for a plain picture workflow."
                  ).pack(side="left", padx=10)

        ob = ttk.LabelFrame(basic, text="4. Where the files go", padding=10)
        ob.pack(fill="x", pady=(10, 0))
        ob.columnconfigure(1, weight=1)
        self.v_outdir = tk.StringVar()
        ttk.Label(ob, text="Folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(ob, textvariable=self.v_outdir).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(ob, text="Browse...", command=self.pick_outdir).grid(row=0, column=2)
        orow = ttk.Frame(ob)
        orow.grid(row=1, column=1, sticky="w", padx=6, pady=(8, 0))
        self.v_gap = tk.StringVar()
        self.v_reuse = tk.BooleanVar(value=True)
        ttk.Label(orow, text="Silence between lines (s):").pack(side="left")
        ttk.Entry(orow, textvariable=self.v_gap, width=6).pack(side="left", padx=6)
        ttk.Checkbutton(orow, text="Re-use anything that hasn't changed",
                        variable=self.v_reuse).pack(side="left", padx=12)

        sb = ttk.LabelFrame(basic, text="5. When the program starts", padding=10)
        sb.pack(fill="x", pady=(10, 0))
        self.v_reopen = tk.BooleanVar(value=proj.load_settings().get("reopen_last", True))
        ttk.Checkbutton(sb, text="Open the project I had last",
                        variable=self.v_reopen,
                        command=lambda: proj.save_settings(
                            {"reopen_last": bool(self.v_reopen.get())})).pack(side="left")
        self.v_reopen_note = tk.StringVar(value="")
        ttk.Label(sb, textvariable=self.v_reopen_note,
                  foreground="#666").pack(side="left", padx=10)

        gb = ttk.LabelFrame(basic, text="6. Graphics card", padding=10)
        gb.pack(fill="x", pady=(10, 0))
        self.v_free_gpu = tk.BooleanVar(value=False)
        ttk.Checkbutton(gb, text="Free the GPU between steps",
                        variable=self.v_free_gpu).pack(side="left")
        ttk.Label(gb, foreground="#666", wraplength=520, justify="left",
                  text="Unloads the writing model before drawing, and ComfyUI before writing. "
                       "Turn this on if the two do not fit on your card at once."
                  ).pack(side="left", padx=10)

        # ------------------------------------------ What draws and speaks page
        ttk.Label(wfpage, font=("Segoe UI", 10, "bold"),
                  text="One ComfyUI workflow per job. Double-click a row to choose its file."
                  ).pack(anchor="w")
        ttk.Label(wfpage, foreground="#666", wraplength=760, justify="left",
                  text="In ComfyUI: Settings > enable Dev mode, then Workflow > Export (API). "
                       "A picture workflow is built in, so the drawing jobs work out of the box; "
                       "speaking needs one you provide."
                  ).pack(anchor="w", pady=(2, 8))

        self.slot_tree = ttk.Treeview(wfpage, columns=("job", "file", "state"),
                                      show="headings", height=5)
        for col, w, a in (("job", 250, "w"), ("file", 330, "w"), ("state", 150, "w")):
            self.slot_tree.heading(col, text={"job": "Job", "file": "Workflow file",
                                              "state": "Status"}[col])
            self.slot_tree.column(col, width=w, anchor=a)
        self.slot_tree.pack(fill="x")
        self.slot_tree.bind("<<TreeviewSelect>>", lambda e: self._show_slot())
        self.slot_tree.bind("<Double-1>", lambda e: self.pick_workflow())

        row = ttk.Frame(wfpage)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Choose a workflow file...",
                   command=self.pick_workflow).pack(side="left")
        ttk.Button(row, text="Work out the inputs again",
                   command=self.autodetect_mapping).pack(side="left", padx=6)
        self.v_wf_path = tk.StringVar()
        ttk.Label(row, textvariable=self.v_wf_path, foreground="#666").pack(side="left", padx=10)

        self.mapbox = ttk.LabelFrame(
            wfpage, text="What ScriptVoice sends into the selected workflow", padding=10)
        self.mapbox.pack(fill="x", pady=(10, 0))
        self.mapbox.columnconfigure(1, weight=1)
        self.map_vars = {}
        self.v_slot_state = tk.StringVar(value="")
        ttk.Label(wfpage, textvariable=self.v_slot_state,
                  foreground="#666").pack(anchor="w", pady=(8, 0))

        # --------------------------------------------------------- Advanced
        ttk.Label(adv, foreground="#666", wraplength=760, justify="left",
                  text="Every input the selected workflow exposes. You do not need this "
                       "unless the automatic mapping picked the wrong one."
                  ).pack(anchor="w", pady=(0, 8))
        insp = ttk.Frame(adv)
        insp.pack(fill="both", expand=True)
        self.wf_tree = ttk.Treeview(insp, columns=("node", "input", "value"),
                                    show="headings", height=14)
        for col, w in (("node", 280), ("input", 170), ("value", 460)):
            self.wf_tree.heading(col, text=col.title())
            self.wf_tree.column(col, width=w)
        vsb = ttk.Scrollbar(insp, command=self.wf_tree.yview)
        self.wf_tree.configure(yscrollcommand=vsb.set)
        self.wf_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.slot_labels = {proj.WORKFLOW_SLOTS[s]["label"]: s for s in proj.WORKFLOW_SLOTS}
        self.v_slot = tk.StringVar(value=proj.WORKFLOW_SLOTS["voice"]["label"])

    def _movie_ready_note(self):
        """Say plainly what the next Make the movie will use.

        The settings live on Setup and the button lives on Movie, so without
        this you can press it with a voice backend or a picture workflow you did
        not expect - which is exactly how a take gets made in the wrong voices.
        """
        if not hasattr(self, "v_movie_ready"):
            return
        p = self.project
        o = p.get("options") or {}
        if o.get("voice_backend", "comfyui") == "system":
            voice = "the voices built into Windows"
        else:
            voice = (os.path.basename(proj.workflow_cfg(p, "voice").get("path") or "")
                     or "no workflow chosen")
        shot = proj.workflow_cfg(p, "shot")
        shot_name = os.path.basename(shot.get("path") or "") or "no workflow chosen"
        locks = ("locks each character's face"
                 if (shot.get("mapping") or {}).get("image")
                 else "no reference input, so faces will drift")
        self.v_movie_ready.set(
            "Voices:   %s\nPictures: %s  -  %s\nPrefix:   %s"
            % (voice, shot_name, locks, o.get("prompt_prefix", "") or "(none)"))

    def _slot(self):
        sel = self.slot_tree.selection() if hasattr(self, "slot_tree") else ()
        if sel and sel[0] in proj.WORKFLOW_SLOTS:
            return sel[0]
        return self.slot_labels.get(self.v_slot.get(), "voice")

    def _refresh_slot_tree(self):
        """One visible row per job, so nothing is hidden behind a dropdown.

        Re-entrant on its own: filling the table sets the selection, which fires
        <<TreeviewSelect>>, which comes back round to here. The flag stops that
        becoming a spin.
        """
        if not hasattr(self, "slot_tree"):
            return
        keep = self.slot_tree.selection()
        self.slot_tree.delete(*self.slot_tree.get_children())
        for slot in proj.WORKFLOW_SLOTS:
            cfg = proj.workflow_cfg(self.project, slot)
            path = cfg.get("path") or ""
            need = [k for k, _, req in proj.WORKFLOW_SLOTS[slot]["keys"] if req]
            missing = [k for k in need if not (cfg.get("mapping") or {}).get(k)]
            if not path:
                state = "not set"
            elif missing:
                state = "needs %s" % ", ".join(missing)
            else:
                state = "ready"
            shown = path if path == getattr(proj, "BUILTIN", "") else os.path.basename(path)
            self.slot_tree.insert("", "end", iid=slot,
                                  values=(proj.WORKFLOW_SLOTS[slot]["label"],
                                          shown or "(none)", state))
        if keep and self.slot_tree.exists(keep[0]):
            self.slot_tree.selection_set(keep[0])
        elif self.slot_tree.get_children():
            self.slot_tree.selection_set(self.slot_tree.get_children()[0])

    def _show_slot(self):
        """Rebuild the mapping rows for the slot the user picked."""
        slot = self._slot()
        cfg = proj.workflow_cfg(self.project, slot)
        self.v_wf_path.set(cfg.get("path", "") or "no workflow chosen for this job")
        for child in self.mapbox.winfo_children():
            child.destroy()
        self.map_vars = {}
        wf = self.workflows.get(slot)
        choices = [""] + [r[0] for r in proj.widget_inputs(wf)] if wf else [""]

        for r, (key, hint, required) in enumerate(proj.WORKFLOW_SLOTS[slot]["keys"]):
            ttk.Label(self.mapbox, text="%s%s \u2192" % (key, "*" if required else "")).grid(
                row=r, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=(cfg.get("mapping") or {}).get(key, ""))
            box = ttk.Combobox(self.mapbox, textvariable=var, state="readonly", values=choices)
            box.grid(row=r, column=1, sticky="ew", padx=6)
            box.bind("<<ComboboxSelected>>", lambda e: self._store_mapping())
            ttk.Label(self.mapbox, text=hint, foreground="#666").grid(row=r, column=2, sticky="w")
            self.map_vars[key] = var
        ttk.Button(self.mapbox, text="Auto-detect", command=self.autodetect_mapping).grid(
            row=len(proj.WORKFLOW_SLOTS[slot]["keys"]), column=1, sticky="w", padx=6, pady=(8, 0))

        self.wf_tree.delete(*self.wf_tree.get_children())
        if wf:
            for target, cls, name, val in proj.widget_inputs(wf):
                self.wf_tree.insert("", "end", iid=target,
                                    values=("%s  #%s" % (cls, target.split(".")[0]),
                                            name, _short(val)))
            self.param_combo["values"] = [r[0] for r in proj.widget_inputs(
                self.workflows.get("voice") or wf)]
        self._slot_state()

    def _slot_state(self):
        ready, missing = [], []
        for slot in proj.WORKFLOW_SLOTS:
            cfg = proj.workflow_cfg(self.project, slot)
            need = [k for k, _, req in proj.WORKFLOW_SLOTS[slot]["keys"] if req]
            if cfg.get("path") and all((cfg.get("mapping") or {}).get(k) for k in need):
                ready.append(slot)
            else:
                missing.append(slot)
        self.v_slot_state.set("Ready: %s%s" % (", ".join(ready) or "none",
                                               ("      Still to set up: " + ", ".join(missing))
                                               if missing else ""))
        self._movie_ready_note()

    def _store_mapping(self):
        slot = self._slot()
        cfg = self.project["workflows"].setdefault(slot, {"path": "", "mapping": {}})
        cfg["mapping"] = {k: v.get().strip() for k, v in self.map_vars.items() if v.get().strip()}
        # v_wf_path is a label now and carries a human message when nothing is
        # loaded ("no workflow chosen for this job"). Writing that back here put
        # that sentence into the project as if it were a filename.
        self.dirty = True
        self._refresh_slot_tree()
        self._slot_state()

    # -------------------------------------------------------------- movie tab

    def _build_movie_tab(self):
        t = self.tab_movie
        head = ttk.Frame(t)
        head.pack(fill="x")
        self.v_movie_ready = tk.StringVar(value="")
        ttk.Label(head, textvariable=self.v_movie_ready, justify="left",
                  foreground="#444").pack(side="left")
        ttk.Button(head, text="Change any of this on the Setup tab",
                   command=lambda: self.nb.select(self.tab_wf)).pack(side="right")

        ctl = ttk.Frame(t)
        ctl.pack(fill="x", pady=(10, 0))
        self.btn_movie = ttk.Button(ctl, text="Make the movie  (F5)", command=self.make_movie)
        self.btn_movie.pack(side="left")
        ttk.Button(ctl, text="Dialogue only", command=self.render_dialogue).pack(side="left", padx=6)
        self.btn_cancel = ttk.Button(ctl, text="Stop", command=self.cancel_job, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        ttk.Button(ctl, text="Play result", command=self.play_result).pack(side="left", padx=6)
        ttk.Button(ctl, text="Open folder", command=self.open_output_folder).pack(side="left")

        self.progress = ttk.Progressbar(t, mode="determinate")
        self.progress.pack(fill="x", pady=(10, 6))

        panes = ttk.Panedwindow(t, orient="vertical")
        panes.pack(fill="both", expand=True)
        cf = ttk.Frame(panes)
        self.cue_tree = ttk.Treeview(cf, columns=("speaker", "text", "voice", "shot"),
                                     show="headings", height=10)
        for col, w, a in (("speaker", 130, "w"), ("text", 560, "w"),
                          ("voice", 90, "center"), ("shot", 90, "center")):
            self.cue_tree.heading(col, text=col.title())
            self.cue_tree.column(col, width=w, anchor=a)
        csb = ttk.Scrollbar(cf, command=self.cue_tree.yview)
        self.cue_tree.configure(yscrollcommand=csb.set)
        self.cue_tree.pack(side="left", fill="both", expand=True)
        csb.pack(side="right", fill="y")
        self.cue_tree.bind("<Double-1>", self.play_selected_cue)
        panes.add(cf, weight=3)

        lf = ttk.Frame(panes)
        self.log = tk.Text(lf, height=8, wrap="word", state="disabled",
                           font=("Consolas", 9), background="#111111", foreground="#dddddd")
        lsb = ttk.Scrollbar(lf, command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        panes.add(lf, weight=1)

    # --------------------------------------------------------------- project

    def _load_project_into_ui(self):
        p = self.project
        o = p["options"]
        self.premise_text.delete("1.0", "end")
        self.premise_text.insert("1.0", p.get("premise", ""))
        self.script_text.delete("1.0", "end")
        self.script_text.insert("1.0", p.get("script", ""))
        self.v_default_speaker.set(o.get("default_speaker", "NARRATOR"))
        self.v_strip.set(bool(o.get("strip_parentheticals", True)))
        self.v_split.set(bool(o.get("split_sentences", False)))
        self.v_maxchars.set(str(o.get("max_chars", 0)))
        self.v_gap.set(str(o.get("gap_seconds", 0.35)))
        self.v_reuse.set(bool(o.get("reuse_unchanged", True)))
        self.v_free_gpu.set(bool(o.get("free_gpu", False)))
        self.v_prefix.set(o.get("prompt_prefix", ""))
        self._movie_ready_note()
        backend = o.get("voice_backend", "comfyui")
        for label, key in self.backend_labels.items():
            if key == backend:
                self.v_backend.set(label)
        self.cb_sys_voice["values"] = [""] + speech.voices()
        self._backend_changed()
        self.v_outdir.set(o.get("output_dir", "") or self._default_outdir())
        self.v_max_actors.set(str(o.get("max_actors", 5)))
        self.v_frames.set(str(o.get("turnaround_frames", 8)))
        self.v_scenes.set(str(o.get("scene_count", 4)))
        self.v_host.set(p["server"].get("host", "127.0.0.1"))
        self.v_port.set(str(p["server"].get("port", 8188)))
        self.v_llm_url.set(p["llm"].get("base_url", ""))
        self.v_llm_model.set(p["llm"].get("model", ""))

        self.workflows = {}
        for slot in proj.WORKFLOW_SLOTS:
            path = proj.workflow_cfg(p, slot).get("path")
            if path and os.path.exists(path):
                try:
                    self.workflows[slot] = proj.load_workflow(path)
                except Exception:
                    pass
        self.v_slot.set(proj.WORKFLOW_SLOTS["voice"]["label"])
        self._refresh_slot_tree()
        self._show_slot()
        self._refresh_cast()
        self._retitle()

    def _collect_ui_into_project(self):
        p = self.project
        p["premise"] = self.premise_text.get("1.0", "end-1c").strip()
        p["script"] = self.script_text.get("1.0", "end-1c")
        o = p["options"]
        o["default_speaker"] = (self.v_default_speaker.get().strip() or "NARRATOR").upper()
        o["strip_parentheticals"] = bool(self.v_strip.get())
        o["split_sentences"] = bool(self.v_split.get())
        o["max_chars"] = _int(self.v_maxchars.get(), 0)
        o["gap_seconds"] = _float(self.v_gap.get(), 0.35)
        o["reuse_unchanged"] = bool(self.v_reuse.get())
        o["free_gpu"] = bool(self.v_free_gpu.get())
        o["prompt_prefix"] = self.v_prefix.get().strip()
        self._movie_ready_note()
        o["voice_backend"] = self.backend_labels.get(self.v_backend.get(), "comfyui")
        o["output_dir"] = self.v_outdir.get().strip()
        o["max_actors"] = max(1, min(8, _int(self.v_max_actors.get(), 5)))
        o["turnaround_frames"] = max(2, min(24, _int(self.v_frames.get(), 8)))
        o["scene_count"] = max(1, min(20, _int(self.v_scenes.get(), 4)))
        p["server"]["host"] = self.v_host.get().strip() or "127.0.0.1"
        p["server"]["port"] = _int(self.v_port.get(), 8188)
        p["llm"]["base_url"] = self.v_llm_url.get().strip()
        p["llm"]["model"] = self.v_llm_model.get().strip()
        # The look box belongs to whichever actor is selected. Collecting it here
        # means New look and Regenerate pick up what was just typed, with no need
        # to press Save first.
        if hasattr(self, "look_text") and self.selected_actor:
            actor = (p.get("characters") or {}).get(self.selected_actor)
            if actor is not None:
                typed = self.look_text.get("1.0", "end-1c").strip()
                if typed != (actor.get("look_note") or ""):
                    actor["look_note"] = typed
                    self.dirty = True
                want = self.v_voice_gender.get()
                want = "" if want in ("", "Auto") else want
                if want != (actor.get("voice_gender") or ""):
                    actor["voice_gender"] = want
                    self.dirty = True
                ref = self.v_reference.get().strip()
                if ref != (actor.get("reference_image") or ""):
                    actor["reference_image"] = ref
                    self.dirty = True
                written = self.who_text.get("1.0", "end-1c").strip()
                if written and written != (actor.get("one_line") or ""):
                    actor["one_line"] = written
                    self.dirty = True
                if self.dirty:
                    # Redraw just this card so the edit is visible at once. A full
                    # _refresh_cast here would re-enter select_actor and overwrite
                    # the very box being typed in.
                    card = self.cards.get(self.selected_actor)
                    if card is not None:
                        card.refresh(actor)
        if self.map_vars:
            self._store_mapping()
        return p

    def _retitle(self):
        name = os.path.basename(self.project_path) if self.project_path else "Untitled"
        self.master.title("%s - %s%s" % (APP, name, " *" if self.dirty else ""))

    def _default_outdir(self):
        base = os.path.dirname(self.project_path) if self.project_path else os.getcwd()
        return os.path.join(base, "output")

    def new_project(self):
        if not self._confirm_discard():
            return
        self.project = proj.new_project()
        self.project_path = ""
        self.cues = []
        self.cue_tree.delete(*self.cue_tree.get_children())
        self._load_project_into_ui()
        self.dirty = False
        self._retitle()
        self.set_status("New project.")

    def open_project(self):
        if not self._confirm_discard():
            return
        path = filedialog.askopenfilename(
            title="Open project",
            filetypes=[("ScriptVoice project", "*" + PROJ_EXT), ("All files", "*.*")])
        if not path:
            return
        try:
            self.project = proj.load(path)
        except Exception as e:
            messagebox.showerror(APP, "Couldn't open that project:\n%s" % e)
            return
        self.project_path = path
        adopted = proj.adopt_default_workflows(self.project)
        self._load_project_into_ui()
        self.dirty = bool(adopted)
        self._retitle()
        if adopted:
            self.set_status("Opened %s - filled %d empty workflow slot%s from the "
                            "bundled workflow." % (os.path.basename(path), len(adopted),
                                                   "" if len(adopted) == 1 else "s"))
            self._log("This project had no picture workflow, so %s now use the one "
                      "shipped with the program. Load your own on the Setup tab to "
                      "change that." % ", ".join(adopted))
        else:
            self.set_status("Opened %s" % os.path.basename(path))

    def save_project(self):
        if not self.project_path:
            return self.save_project_as()
        proj.save(self._collect_ui_into_project(), self.project_path)
        proj.remember_project(self.project_path)
        self.dirty = False
        self._retitle()
        self.set_status("Saved %s" % os.path.basename(self.project_path))

    def save_project_as(self):
        path = filedialog.asksaveasfilename(
            title="Save project", defaultextension=PROJ_EXT,
            filetypes=[("ScriptVoice project", "*" + PROJ_EXT)])
        if not path:
            return
        self.project_path = path
        self.save_project()

    def import_script(self):
        path = filedialog.askopenfilename(
            title="Import script",
            filetypes=[("Text", "*.txt *.md *.fountain"), ("All files", "*.*")])
        if not path:
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        self.script_text.delete("1.0", "end")
        self.script_text.insert("1.0", text)
        self.dirty = True
        self.nb.select(self.tab_script)
        self.compile_script()

    def _confirm_discard(self):
        if not self.dirty:
            return True
        r = messagebox.askyesnocancel(APP, "Save changes to the current project first?")
        if r is None:
            return False
        if r:
            self.save_project()
        return True

    # ------------------------------------------------------------ local model

    def detect_llm(self):
        self.set_status("Looking for a local model server...")

        def work():
            found = llm_mod.discover()
            self.events.put({"kind": "llm_found", "servers": found})

        threading.Thread(target=work, daemon=True).start()

    def load_models(self):
        url = self.v_llm_url.get().strip()
        if not url:
            return
        def work():
            try:
                models = llm_mod.LocalLLM(url).models()
                self.events.put({"kind": "llm_models", "models": models})
            except llm_mod.LLMError as e:
                self.events.put({"kind": "llm_models", "models": [], "error": str(e)})
        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- casting

    def create_cast(self, steps=None):
        p = self._collect_ui_into_project()
        if not p["premise"]:
            messagebox.showinfo(APP, "Write the premise first - one or two sentences.")
            self.nb.select(self.tab_premise)
            return
        if not self.ensure_llm():
            return
        p = self._collect_ui_into_project()
        if steps is None:
            steps = ("cast", "roles", "portrait", "spin", "voice")
        if "cast" in steps and p.get("characters"):
            if not messagebox.askyesno(
                    APP, "This replaces the current cast and everything rendered for them.\n\n"
                         "Continue?"):
                return
        out = p["options"]["output_dir"] or self._default_outdir()
        self.v_outdir.set(out)
        self._log_clear()
        self.nb.select(self.tab_cast)
        self._start(CastJob(p, out, self.events.put, steps=steps), "Casting...")

    WAITING = {"actor": "Recasting...", "voice": "Choosing a new voice...",
               "look": "Starting a new render...", "spin": "Starting the turnaround...",
               "role": "Reading their lines in the script..."}

    def regenerate(self, name, what, note=""):
        p = self._collect_ui_into_project()
        out = p["options"]["output_dir"] or self._default_outdir()
        card = self.cards.get(name)
        if card is not None:      # feedback on the click itself, not on the first
            card.set_progress(self.WAITING.get(what, "Working..."))   # worker event
        self._start(RegenerateJob(p, out, self.events.put, name, what=what, note=note),
                    "Regenerating %s for %s..." % (what, name))

    def ensure_llm(self):
        """True if a model server is answering - offering to start one if not.

        Writing needs the model; drawing does not. Rather than failing a click
        outright, ask once and start LM Studio ourselves.
        """
        url = self.v_llm_url.get() or "http://127.0.0.1:1234/v1"
        try:
            llm_mod.LocalLLM(url).models()
            return True
        except llm_mod.LLMError:
            pass
        found = llm_mod.discover()
        if found:                       # something else is already serving
            self.v_llm_url.set(found[0][1])
            self.set_status("Using %s at %s" % (found[0][0], found[0][1]))
            return True
        if not llm_mod.find_lmstudio_cli():
            messagebox.showerror(
                APP, "No local model server is running, and LM Studio's command-line "
                     "tool wasn't found so it can't be started for you.\n\n"
                     "Open LM Studio and use Developer > Start Server, then try again.")
            return False
        if not messagebox.askyesno(
                APP, "This step needs the AI to write, and no model server is "
                     "running.\n\nStart LM Studio now?\n\nIt will share the graphics "
                     "card with ComfyUI, so drawing will be slower while it is "
                     "loaded. Drawing on its own (New look) does not need it."):
            return False
        self.set_status("Starting LM Studio...")
        self.update_idletasks()
        try:
            base = llm_mod.start_lmstudio()
        except llm_mod.LLMError as e:
            messagebox.showerror(APP, str(e))
            self.set_status("LM Studio did not start.")
            return False
        self.v_llm_url.set(base)
        self.dirty = True
        self.load_models()
        self.set_status("LM Studio is running at %s." % base)
        return True

    def _card_action(self, name, action, value):
        if self.busy() and action not in ("approve",):
            messagebox.showinfo(APP, "Something is already running.")
            return
        actor = (self.project.get("characters") or {}).get(name)
        if not actor:
            return
        if action == "approve":
            actor["approved"] = bool(value)
            self.dirty = True
            self._update_gate()
            return
        if action == "lead":
            actor["lead"] = bool(value)
            actor["role"] = "lead" if value else "supporting"
            self.dirty = True
            self._refresh_cast()
            self.set_status("%s is %s the main character." % (name, "now" if value else "no longer"))
            return
        if action == "play":
            path = actor.get("voice_sample")
            if path and os.path.exists(path):
                _play(path)
            else:
                messagebox.showinfo(APP, "No voice sample yet for %s." % name)
            return
        if action == "find":
            self.find_in_script(name)
            return
        if action == "remove":
            self.remove_character(name)
            return
        if action == "same":
            self.merge_character(name)
            return
        if action in ("actor", "voice", "look"):
            note = ""
            if action in ("actor", "voice"):
                if not self.ensure_llm():
                    return
                note = _ask_string(self, "Regenerate %s" % action,
                                   "Optional note for the AI (leave blank for a free hand):") or ""
            self.select_actor(name)
            self.regenerate(name, action, note)

    def save_look(self, redraw=False):
        """Store the user's own appearance words on the selected actor."""
        name = self.selected_actor
        actor = (self.project.get("characters") or {}).get(name)
        if not actor:
            messagebox.showinfo(APP, "Select an actor first.")
            return False
        self._collect_ui_into_project()
        self.dirty = True
        self._refresh_cast()
        self.set_status("Saved the look for %s." % name)
        return True

    def merge_character(self, name):
        """Fold this character's lines into another - MAN is really SAM."""
        chars = self.project.get("characters") or {}
        others = [n for n in [a["name"] for a in proj.cast(self.project)] if n != name]
        if not others:
            messagebox.showinfo(APP, "There is nobody else in the cast to merge into.")
            return
        target = _ask_choice(self, "Same person",
                            "%s is really which character?" % name, others)
        if not target or target not in chars:
            return
        keep = chars[target]
        aliases = [a for a in (keep.get("aliases") or []) if a != target]
        for extra in [name] + list(chars.get(name, {}).get("aliases") or []):
            if extra not in aliases and extra != target:
                aliases.append(extra)
        keep["aliases"] = aliases
        chars.pop(name, None)
        self.project["cast_order"] = [n for n in (self.project.get("cast_order") or [])
                                      if n != name]
        if self.selected_actor == name:
            self.selected_actor = target
        self.dirty = True
        self.scan_script()
        self._refresh_cast()
        self.select_actor(target)
        self.set_status("%s is now part of %s. %s speaks %d line%s."
                        % (name, target, target, keep.get("line_count", 0),
                           "" if keep.get("line_count", 0) == 1 else "s"))

    def split_character(self):
        """Undo a merge: give one absorbed name its own card back."""
        name = self.selected_actor
        actor = (self.project.get("characters") or {}).get(name)
        if not actor:
            return
        aliases = list(actor.get("aliases") or [])
        if not aliases:
            messagebox.showinfo(APP, "%s hasn't absorbed anyone." % name)
            return
        which = _ask_choice(self, "Split off", "Give which name its own card back?",
                            aliases)
        if not which:
            return
        actor["aliases"] = [a for a in aliases if a != which]
        chars = self.project.setdefault("characters", {})
        if which not in chars:
            chars[which] = proj.new_character(which)
            self.project.setdefault("cast_order", []).append(which)
        self.dirty = True
        self.scan_script()
        self._refresh_cast()
        self.select_actor(which)
        self.set_status("%s is a separate character again." % which)

    def add_character(self):
        """Put a character in the cast by hand, with no AI involved."""
        raw = _ask_string(self, "Add a character",
                          "Name, as it appears in the script (e.g. SAM, BANKER):")
        name = script_parser._norm(raw or "")
        if not name:
            return
        chars = self.project.setdefault("characters", {})
        if name in chars:
            messagebox.showinfo(APP, "%s is already in the cast." % name)
            self.select_actor(name)
            return
        chars[name] = proj.new_character(name)
        order = self.project.setdefault("cast_order", [])
        if name not in order:
            order.append(name)
        self.dirty = True
        self._refresh_cast()
        self.select_actor(name)
        self.set_status("Added %s. Type their look and what they do, then press New look."
                        % name)

    def remove_character(self, name):
        """Take a character out of the cast. Their files on disk are left alone."""
        actor = (self.project.get("characters") or {}).get(name)
        if not actor:
            return
        made = [w for w, got in (("a portrait", actor.get("portrait")),
                                 ("a turnaround", actor.get("turnaround")),
                                 ("a voice sample", actor.get("voice_sample"))) if got]
        extra = ("\n\nThey have %s. Those files stay on disk, so adding them back "
                 "with the same name picks them up again." % " and ".join(made)) if made else ""
        if not messagebox.askyesno(APP, "Remove %s from the cast?%s" % (name, extra)):
            return
        self.project["characters"].pop(name, None)
        self.project["cast_order"] = [n for n in (self.project.get("cast_order") or [])
                                      if n != name]
        if self.selected_actor == name:
            self.selected_actor = ""
        self.dirty = True
        self._refresh_cast()
        self.set_status("Removed %s from the cast." % name)

    def find_in_script(self, name):
        """Show the Script tab with this character's lines highlighted.

        Half the cast of a real screenplay is MAN, INVESTOR, BANKER - names that
        say nothing. Seeing their actual lines is the fastest way to know who
        they are.
        """
        self._collect_ui_into_project()
        cues = self.scan_script()
        places = [c for c in (cues or []) if c.speaker == name]
        self.nb.select(self.tab_script)
        self.script_text.tag_remove("whois", "1.0", "end")
        if not places:
            self.set_status("%s does not speak anywhere in the script." % name)
            messagebox.showinfo(
                APP, "%s has no lines in the script.\n\nThey may be spelled "
                     "differently there, or only appear in the action." % name)
            return
        self.script_text.tag_configure("whois", background="#fff3a3", foreground="#000")
        for c in places:
            self.script_text.tag_add("whois", "%d.0" % c.line_no, "%d.end" % c.line_no)
        first = places[0].line_no
        self.script_text.see("%d.0" % max(1, first - 2))
        self.script_text.mark_set("insert", "%d.0" % first)
        self.script_text.focus_set()
        self.set_status("%s: %d line%s, first at line %d. Highlighted in the script."
                        % (name, len(places), "" if len(places) == 1 else "s", first))

    def pick_reference(self):
        """Choose your own picture of this character for every render."""
        name = self.selected_actor
        actor = (self.project.get("characters") or {}).get(name)
        if not actor:
            messagebox.showinfo(APP, "Select an actor first.")
            return
        path = filedialog.askopenfilename(
            title="Reference picture for %s" % name,
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp"), ("All files", "*.*")])
        if not path:
            return
        actor["reference_image"] = path
        self.v_reference.set(path)
        self.dirty = True
        self._refresh_cast()
        self.select_actor(name)
        self.set_status("%s will be drawn from %s." % (name, os.path.basename(path)))

    def clear_reference(self):
        """Go back to the portrait the program drew."""
        name = self.selected_actor
        actor = (self.project.get("characters") or {}).get(name)
        if not actor:
            return
        actor["reference_image"] = ""
        self.v_reference.set("")
        self.dirty = True
        self.select_actor(name)
        self.set_status("%s is back to the drawn portrait." % name)

    def save_who(self):
        """Store the user's own words about who this character is."""
        if not self.selected_actor:
            messagebox.showinfo(APP, "Select an actor first.")
            return False
        self._collect_ui_into_project()
        self._refresh_cast()
        self.set_status("Saved the description for %s." % self.selected_actor)
        return True

    def save_who_and_judge(self):
        if self.save_who() and not self.busy():
            if self.ensure_llm():
                self.regenerate(self.selected_actor, "role")

    def save_look_and_redraw(self):
        if self.save_look() and not self.busy():
            self.regenerate(self.selected_actor, "look")

    def select_actor(self, name):
        actor = (self.project.get("characters") or {}).get(name)
        if not actor:
            return
        self.selected_actor = name
        self.v_sel_name.set("%s - %s" % (name, actor.get("voice_type", "") or "no voice yet"))
        self.v_voice_file.set(actor.get("voice_file", ""))
        self.v_voice_value.set(actor.get("voice_value", ""))
        self.v_seed.set(str(actor.get("seed", -1)))
        override = actor.get("system_voice") or {}
        assigned = system_voice(actor)
        self.v_voice_gender.set(actor.get("voice_gender", "") or "Auto")
        self.v_sys_voice.set(override.get("voice", ""))
        self.v_sys_rate.set(str(override.get("rate", assigned.get("rate", 0))))
        self.v_sys_pitch.set(str(override.get("pitch", assigned.get("pitch", 0))))
        self.v_sys_note.set(("chosen: " if override.get("voice") else "auto: ")
                            + speech.describe_voice(assigned))
        self.look_text.delete("1.0", "end")
        self.look_text.insert("1.0", actor.get("look_note", ""))
        self.who_text.delete("1.0", "end")
        self.who_text.insert("1.0", actor.get("one_line", ""))
        self.v_reference.set(actor.get("reference_image", ""))
        using = visuals.identity_image(actor)
        if actor.get("reference_image"):
            self.v_reference_note.set("Your own picture. Every shot of %s is drawn "
                                      "from it." % name)
        elif using:
            self.v_reference_note.set("Using the portrait the program drew. Choose your "
                                      "own picture to override it.")
        else:
            self.v_reference_note.set("Nothing to lock onto yet - press New look, or "
                                      "choose a picture of your own.")
        frames = list(actor.get("turnaround") or [])
        if not frames and actor.get("portrait"):
            frames = [actor["portrait"]]
        self.spin.set_frames(frames, "%s - %d frame%s" % (name, len(frames),
                                                          "" if len(frames) == 1 else "s"))
        self._refresh_params(actor)

    def _refresh_cast(self):
        actors = proj.cast(self.project)
        names = [a["name"] for a in actors]
        # How many lines each one has, so a card can say "38 lines in the script".
        counts = {}
        for c in (self.cues or []):
            counts[c.speaker] = counts.get(c.speaker, 0) + 1
        for a in actors:
            a["line_count"] = counts.get(a["name"], 0)
        if set(names) != set(self.cards):
            self.cast_scroll.clear()
            self.cards = {}
            for a in actors:
                card = ActorCard(self.cast_scroll.body, a, self._card_action, self.select_actor)
                card.pack(fill="x", pady=4, padx=2)
                self.cards[a["name"]] = card
        else:
            for a in actors:
                self.cards[a["name"]].refresh(a)
        approved = sum(1 for a in actors if a.get("approved"))
        described = sum(1 for a in actors if a.get("one_line"))
        self.v_cast_summary.set(
            "%d character%s | %d described | %d approved"
            % (len(actors), "" if len(actors) == 1 else "s", described, approved)
            if actors else "No cast yet - write a premise and press Create the cast.")
        if self.selected_actor in self.cards:
            self.select_actor(self.selected_actor)
        elif actors:
            self.select_actor(actors[0]["name"])
        self._update_gate()

    def _update_gate(self):
        actors = proj.cast(self.project)
        pending = [a["name"] for a in actors if not a.get("approved")]
        if actors and not pending:
            self.v_gate.set("Every actor is approved.")
            self.btn_movie_gate.configure(state="normal")
        else:
            self.v_gate.set("Waiting on approval: %s" % (", ".join(pending) or "-")
                            if actors else "")
            self.btn_movie_gate.configure(state="disabled")
        for a in actors:
            if a["name"] in self.cards:
                self.cards[a["name"]].v_approved.set(bool(a.get("approved")))

    def approve_all(self):
        for a in proj.cast(self.project):
            a["approved"] = True
        self.dirty = True
        self._refresh_cast()

    def save_voice_details(self):
        actor = (self.project.get("characters") or {}).get(self.selected_actor)
        if not actor:
            return
        actor["voice_file"] = self.v_voice_file.get().strip()
        actor["voice_value"] = self.v_voice_value.get().strip()
        actor["seed"] = _int(self.v_seed.get(), -1)
        chosen = self.v_sys_voice.get().strip()
        actor["system_voice"] = {"voice": chosen,
                                 "rate": _int(self.v_sys_rate.get(), 0),
                                 "pitch": _int(self.v_sys_pitch.get(), 0)} if chosen else {}
        self.dirty = True
        self._refresh_cast()
        self.set_status("Saved voice details for %s." % self.selected_actor)

    def _backend_changed(self):
        """Say plainly what the chosen voice source can and can't do."""
        backend = self.backend_labels.get(self.v_backend.get(), "comfyui")
        if backend == "system":
            names = speech.voices()
            self.v_backend_note.set(
                "   %d voice%s installed - robotic, but needs nothing"
                % (len(names), "" if len(names) == 1 else "s") if names
                else "   no Windows voices found on this machine")
        else:
            ready = bool(proj.workflow_cfg(self.project, "voice").get("path"))
            self.v_backend_note.set("   using the voice workflow" if ready
                                    else "   no voice workflow set yet (tab 4)")
        self.dirty = True

    def preview_system_voice(self):
        """Speak this actor's sample line in the voice the film will use.

        Whichever backend the project is set to - so this cannot preview a
        Windows voice while the film is being made with a ComfyUI one.
        """
        actor = (self.project.get("characters") or {}).get(self.selected_actor)
        if not actor:
            return
        p = self._collect_ui_into_project()
        backend = (p.get("options") or {}).get("voice_backend", "comfyui")
        self.save_voice_details()
        line = actor.get("sample_line") or "This is how I sound in this film."
        out = os.path.join(self.v_outdir.get() or self._default_outdir(), "_previews")
        safe = actor["name"].replace(" ", "_")

        if backend == "system":
            if not speech.available():
                messagebox.showinfo(APP, "Windows speech isn't available on this machine.")
                return

            def work():
                try:
                    v = system_voice(actor)
                    path = speech.speak_to_wav(
                        line, os.path.join(out, "%s_system.wav" % safe),
                        voice=v["voice"], rate=v["rate"], pitch=v["pitch"])
                    self.events.put({"kind": "played", "path": path})
                except Exception as e:
                    self.events.put({"kind": "failed", "message": str(e), "cancelled": False})

            self.set_status("Speaking %s with the Windows voice..." % actor["name"])
            threading.Thread(target=work, daemon=True).start()
            return

        cfg = proj.workflow_cfg(p, "voice")
        if not cfg.get("path"):
            messagebox.showinfo(
                APP, "Voices come from a ComfyUI workflow, but no workflow is loaded "
                     "for speaking.\n\nSet one on the Setup tab under What draws and "
                     "speaks, or switch Where the voices come from back to Windows.")
            self.nb.select(self.tab_wf)
            return

        def work_comfy():
            try:
                client = ComfyClient(p["server"]["host"], p["server"]["port"], timeout=600)
                runner = SlotRunner(client, p, "voice")
                values = {"text": line, "seed": int(actor.get("seed", 0) or 0)}
                if runner.has("voice"):
                    if actor.get("voice_file") and os.path.exists(actor["voice_file"]):
                        values["voice"] = runner.upload(actor["voice_file"])
                    elif actor.get("voice_value"):
                        values["voice"] = actor["voice_value"]
                path = runner.run(values, os.path.join(out, "%s_comfy" % safe), AUDIO,
                                  params=actor.get("params") or {})
                self.events.put({"kind": "played", "path": path})
            except Exception as e:
                self.events.put({"kind": "failed", "message": str(e), "cancelled": False})

        self.set_status("Speaking %s through ComfyUI..." % actor["name"])
        threading.Thread(target=work_comfy, daemon=True).start()

    def pick_voice_file(self):
        path = filedialog.askopenfilename(
            title="Reference voice clip",
            filetypes=[("Audio", "*.wav *.mp3 *.flac *.ogg *.m4a"), ("All files", "*.*")])
        if path:
            self.v_voice_file.set(path)

    def _refresh_params(self, actor):
        self.param_tree.delete(*self.param_tree.get_children())
        wf = self.workflows.get("voice")
        for target, value in sorted((actor.get("params") or {}).items()):
            self.param_tree.insert("", "end", iid=target,
                                   text=proj.describe(wf, target) if wf else target,
                                   values=(value,))

    def set_param(self):
        actor = (self.project.get("characters") or {}).get(self.selected_actor)
        target = self.v_param_target.get().strip()
        if not actor or not target:
            messagebox.showinfo(APP, "Select an actor and a workflow input first.")
            return
        actor.setdefault("params", {})[target] = _coerce(self.v_param_value.get())
        self._refresh_params(actor)
        self.dirty = True

    def remove_param(self):
        actor = (self.project.get("characters") or {}).get(self.selected_actor)
        sel = self.param_tree.selection()
        if not actor or not sel:
            return
        for iid in sel:
            (actor.get("params") or {}).pop(iid, None)
        self._refresh_params(actor)
        self.dirty = True

    # ----------------------------------------------------------------- script

    def write_script(self):
        p = self._collect_ui_into_project()
        actors = proj.cast(p)
        if not actors:
            messagebox.showinfo(APP, "Create the cast first - the AI writes only for them.")
            self.nb.select(self.tab_premise)
            return
        if not self.ensure_llm():
            return
        p = self._collect_ui_into_project()
        if self.script_text.get("1.0", "end-1c").strip():
            if not messagebox.askyesno(APP, "Replace the current script?"):
                return
        self.nb.select(self.tab_script)
        self._start(_ScriptJob(p, self.events.put), "Writing the script...")

    def _on_tab_changed(self, _evt=None):
        """Re-read the script whenever the user moves on from it.

        Editing the script and then wondering where the characters went is the
        obvious trap, so leaving the tab quietly parses it.
        """
        if not hasattr(self, "script_text"):
            return
        if self.nb.select() in (str(self.tab_cast), str(self.tab_board)):
            self._collect_ui_into_project()
            text = self.project.get("script", "")
            if text.strip() and text != getattr(self, "_last_compiled", None):
                self.scan_script()

    def compile_script(self):
        """Read the script, add every speaker to the cast, and say what happened."""
        before = set(self.project.get("characters") or {})
        cues = self.scan_script()
        if not cues:
            messagebox.showinfo(
                APP, "Nothing to compile yet.\n\nPaste a script, or press Load a script "
                     "file, or write one on the Premise tab.")
            return []
        added = [n for n in (self.project.get("characters") or {}) if n not in before]
        speakers = script_parser.speakers(cues)
        self.set_status("Compiled %d lines from %d characters." % (len(cues), len(speakers)))
        messagebox.showinfo(
            APP, "%d spoken lines from %d characters.\n\n%s\n\n%s"
                 % (len(cues), len(speakers), ", ".join(speakers),
                    ("Added to the cast: %s" % ", ".join(added)) if added
                    else "Everyone was already in the cast."))
        self.nb.select(self.tab_cast)
        return cues

    def scan_script(self):
        self._collect_ui_into_project()
        o = self.project["options"]
        self.cues = script_parser.parse(
            self.project["script"], default_speaker=o["default_speaker"],
            strip_parentheticals=o["strip_parentheticals"],
            split_sentences=o["split_sentences"], max_chars=o["max_chars"])
        # MAN and SAM are the same person if the user has said so, and that has
        # to be true before the cast is counted or anything is rendered.
        proj.apply_aliases(self.project, self.cues)
        names = script_parser.speakers(self.cues)
        unknown = [n for n in names if n not in self.project["characters"]]
        for n in unknown:                       # keep the script renderable
            self.project["characters"][n] = proj.new_character(n)
            self.project.setdefault("cast_order", []).append(n)
        self._refresh_cast()
        self._refresh_cue_tree()
        self._refresh_board()
        self.script_info.set(
            "%d lines, %d speakers%s." % (len(self.cues), len(names),
                                          (" (%s added to the cast)" % ", ".join(unknown))
                                          if unknown else ""))
        self.dirty = True
        # Remember what was parsed, so moving between tabs does not re-parse an
        # unchanged script. This was never set, so every visit to Cast or
        # Storyboard re-read the whole script and rebuilt every list.
        self._last_compiled = self.project["script"]
        return self.cues

    # ----------------------------------------------------------------- render

    def render_dialogue(self):
        cues = self.scan_script()
        if not cues:
            messagebox.showinfo(APP, "There's nothing to speak - the script is empty.")
            return
        p = self._collect_ui_into_project()
        out = p["options"]["output_dir"] or self._default_outdir()
        self.v_outdir.set(out)
        self.progress.configure(maximum=len(cues), value=0)
        self.nb.select(self.tab_movie)
        self._log_clear()
        self._start(RenderJob(p, cues, os.path.join(out, "audio"), self.events.put),
                    "Recording dialogue...")

    def make_movie(self):
        cues = self.scan_script()
        if not cues:
            messagebox.showinfo(APP, "Write or generate a script first.")
            self.nb.select(self.tab_script)
            return
        p = self._collect_ui_into_project()
        actors = proj.cast(p)
        pending = [a["name"] for a in actors if not a.get("approved")]
        if pending:
            if not messagebox.askyesno(
                    APP, "These actors aren't approved yet:\n\n%s\n\n"
                         "Make the movie anyway?" % ", ".join(pending)):
                self.nb.select(self.tab_cast)
                return
        out = p["options"]["output_dir"] or self._default_outdir()
        self.v_outdir.set(out)
        self.progress.configure(maximum=len(cues), value=0)
        self._refresh_cue_tree()
        self.nb.select(self.tab_movie)
        self._log_clear()
        self._start(MovieJob(p, cues, out, self.events.put), "Making the movie...")

    def _refresh_cue_tree(self):
        self.cue_tree.delete(*self.cue_tree.get_children())
        for c in self.cues:
            self.cue_tree.insert("", "end", iid=str(c.index),
                                 values=(c.speaker, _short(c.text, 160), "queued", "queued"))

    def play_result(self):
        out = self.v_outdir.get() or self._default_outdir()
        for candidate in (os.path.join(out, "movie.mp4"),
                          os.path.join(out, "audio", "full_take.wav")):
            if os.path.exists(candidate):
                return _play(candidate)
        messagebox.showinfo(APP, "Nothing rendered yet.")

    def play_selected_cue(self, _e=None):
        sel = self.cue_tree.selection()
        if not sel:
            return
        tags = self.cue_tree.item(sel[0], "tags") or ()
        if tags and os.path.exists(tags[0]):
            _play(tags[0])
        else:
            self.set_status("That line hasn't been rendered yet.")

    def open_output_folder(self):
        out = self.v_outdir.get() or self._default_outdir()
        if not os.path.isdir(out):
            messagebox.showinfo(APP, "Nothing rendered yet.")
            return
        if sys.platform.startswith("win"):
            os.startfile(out)
        else:
            webbrowser.open("file://" + out)

    def pick_outdir(self):
        d = filedialog.askdirectory(title="Output folder")
        if d:
            self.v_outdir.set(d)

    # -------------------------------------------------------------- workflows

    def pick_workflow(self):
        slot = self._slot()
        path = filedialog.askopenfilename(
            title="Workflow for: %s" % proj.WORKFLOW_SLOTS[slot]["label"],
            filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if path:
            self._load_workflow(slot, path)

    def _load_workflow(self, slot, path, quiet=False):
        try:
            wf = proj.load_workflow(path)
        except Exception as e:
            if not quiet:
                messagebox.showerror(APP, str(e))
            return
        self.workflows[slot] = wf
        cfg = self.project["workflows"].setdefault(slot, {"path": "", "mapping": {}})
        changed = os.path.normcase(cfg.get("path") or "") != os.path.normcase(path)
        cfg["path"] = path
        self.v_wf_path.set(path)
        if changed or not (cfg.get("mapping") or {}):
            # A mapping names nodes inside the file it was made for ("6.text").
            # Keeping it across a different file points it at nodes that are not
            # there, and the slot then reads as ready while it cannot run.
            cfg["mapping"] = proj.guess_mapping(wf, slot)
        self._refresh_slot_tree()
        self._show_slot()
        self.dirty = True
        wrong_kind = proj.slot_mismatch(wf, slot)
        if wrong_kind and not quiet:
            messagebox.showwarning(
                APP, "%s\n\nIf you meant it for a different job, pick that row on the "
                     "Setup tab first and load it again." % wrong_kind)
            self.set_status(wrong_kind)
            return
        missing = [k for k, _, req in proj.WORKFLOW_SLOTS[slot]["keys"]
                   if req and not (cfg.get("mapping") or {}).get(k)]
        if missing and not quiet:
            messagebox.showwarning(
                APP, "That workflow was loaded for %s, but it has no %s input.\n\n"
                     "This job cannot run without one. If you meant it for a different "
                     "job, pick that row first and load it again."
                     % (proj.WORKFLOW_SLOTS[slot]["label"], " or ".join(missing)))
            self.set_status("Loaded, but %s has no %s input."
                            % (os.path.basename(path), " or ".join(missing)))
        elif not quiet:
            self.set_status("Loaded %d-node workflow for %s."
                            % (len(wf), proj.WORKFLOW_SLOTS[slot]["label"]))

    def autodetect_mapping(self):
        slot = self._slot()
        wf = self.workflows.get(slot)
        if not wf:
            messagebox.showinfo(APP, "Load a workflow for this job first.")
            return
        guess = proj.guess_mapping(wf, slot)
        for key, var in self.map_vars.items():
            var.set(guess.get(key, ""))
        self._store_mapping()
        self.set_status("Auto-detected: %s" % ", ".join(
            "%s=%s" % (k, v) for k, v in guess.items() if v) or "nothing found")

    def test_connection(self):
        self._collect_ui_into_project()
        c = ComfyClient(self.project["server"]["host"], self.project["server"]["port"])

        def work():
            try:
                self.events.put({"kind": "conn", "ok": True, "message": c.ping()})
                return
            except ComfyError as e:
                first = str(e)
            found = comfy_mod.find_server(self.project["server"]["host"])
            if found:
                self.events.put({"kind": "conn", "ok": True, "port": found,
                                 "message": ComfyClient(self.project["server"]["host"],
                                                        found).ping()})
            else:
                self.events.put({"kind": "conn", "ok": False, "message": first})

        threading.Thread(target=work, daemon=True).start()

    def open_comfy(self):
        self._collect_ui_into_project()
        webbrowser.open("http://%s:%d" % (self.project["server"]["host"],
                                          self.project["server"]["port"]))

    # ------------------------------------------------------------ job control

    def busy(self):
        return bool(self.job and self.job.is_alive())

    def _start(self, job, label):
        if self.busy():
            messagebox.showinfo(APP, "Something is already running.")
            return
        self.job = job
        self.set_status(label)
        self.btn_cancel.configure(state="normal")
        for b in (self.btn_cast, self.btn_movie, self.btn_movie_gate, self.btn_board):
            b.configure(state="disabled")
        job.start()

    def cancel_job(self):
        if self.busy():
            self.job.cancel()
            try:
                ComfyClient(self.project["server"]["host"],
                            self.project["server"]["port"]).interrupt()
            except Exception:
                pass
            self.set_status("Stopping...")

    def _job_finished(self):
        for card in self.cards.values():
            card.clear_progress()       # a failed job must not leave a bar marching
        self.btn_cancel.configure(state="disabled")
        for b in (self.btn_cast, self.btn_movie, self.btn_board):
            b.configure(state="normal")
        self._update_gate()

    # ---------------------------------------------------------- event pumping

    def _drain_events(self):
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        self.after(120, self._drain_events)

    def _handle_event(self, e):
        kind = e.get("kind")
        if kind == "log":
            self._log(e["message"])
        elif kind == "step":
            self.set_status(e["label"] + (" (%d/%d)" % (e["done"], e["total"])
                                          if e.get("total") else ""))
        elif kind == "conn":
            self._set_conn(comfy=e["message"] if e["ok"] else "unreachable")
            if not e["ok"]:
                messagebox.showerror(APP, "Couldn't reach ComfyUI.\n\n%s\n\n"
                                          "Start ComfyUI first, then try again." % e["message"])
        elif kind == "llm_found":
            self._servers = e["servers"]
            urls = [s[1] for s in self._servers]
            self.cb_llm["values"] = urls
            if urls:
                if self.v_llm_url.get() not in urls:
                    self.v_llm_url.set(urls[0])
                self.load_models()
                self.set_status("Found: %s" % ", ".join("%s (%s)" % (s[0], s[1])
                                                        for s in self._servers))
            else:
                self._set_conn(model="none found")
                messagebox.showinfo(
                    APP, "No local model server answered on 127.0.0.1.\n\n"
                         "Start LM Studio (Developer > Start Server) or run `ollama serve`, "
                         "then press Detect again.")
        elif kind == "llm_models":
            models = e.get("models") or []
            self.cb_model["values"] = models
            if models and self.v_llm_model.get() not in models:
                self.v_llm_model.set(models[0])
            self._set_conn(model=self.v_llm_model.get() or (e.get("error") or "no model"))
            self.dirty = True
        elif kind == "played":
            self.set_status("Played %s" % os.path.basename(e["path"]))
            _play(e["path"])
        elif kind == "cast_updated":
            self._refresh_cast()
            self.dirty = True
        elif kind == "actor_progress":
            card = self.cards.get(e.get("name"))
            if card is not None:
                card.set_progress(e.get("label", ""), e.get("done", 0), e.get("total", 0))
        elif kind == "actor_updated":
            self._refresh_cast()
            if e.get("name") == self.selected_actor:
                self.select_actor(e["name"])
        elif kind == "asset":
            name, asset = e.get("name"), e.get("asset")
            if name in self.cards:
                self.cards[name].refresh(self.project["characters"][name])
            if asset == "spin" and name == self.selected_actor:
                self.select_actor(name)
            elif asset == "spin_frame":
                self.set_status("%s turnaround frame %d/%d"
                                % (name, e.get("index", 0) + 1, e.get("total", 0)))
        elif kind == "cue_start":
            self._cue_state(e["index"], "voice", "speaking...")
            self.set_status("Line %d/%d - %s" % (e["index"] + 1, e["total"], e["speaker"]))
        elif kind == "cue_done":
            self._cue_state(e["index"], "voice", "re-used" if e.get("cached") else "done",
                            tag=e.get("file"))
            self.progress.configure(value=e["index"] + 1)
        elif kind == "shot_done":
            self._cue_state(e["index"], "shot", "re-used" if e.get("cached") else "done")
            iid = str(e["index"])
            if self.board_tree.exists(iid):
                self.board_tree.set(iid, "state", "re-used" if e.get("cached") else "drawn")
                self.board_tree.see(iid)
                if self.board_tree.selection() and self.board_tree.selection()[0] == iid:
                    self._on_board_select()
            self.board_progress.configure(value=self.board_progress["value"] + 1)
        elif kind == "finished":
            self._job_finished()
            self._on_job_result(e)
        elif kind == "failed":
            self._job_finished()
            self._log("ERROR: " + e["message"])
            self.set_status("Stopped." if e.get("cancelled") else "Failed.")
            if not e.get("cancelled"):
                messagebox.showerror(APP, e["message"])

    def _on_job_result(self, e):
        job = e.get("job")
        if job == "cast":
            self._refresh_cast()
            if e.get("visuals") is False:
                self.set_status("Cast written, but nothing drawn - ComfyUI is unreachable.")
                messagebox.showinfo(
                    APP, "The cast is written.\n\nNo portraits, turnarounds "
                         "or voice samples were made, because ComfyUI couldn't be "
                         "reached. Start it, press Test connection on the Setup tab, "
                         "then use New look on each actor.")
            else:
                self.set_status("Cast ready: %s" % ", ".join(e.get("cast") or []))
            self.nb.select(self.tab_cast)
        elif job == "regen":
            self._refresh_cast()
            if e.get("visuals") is False:
                self._no_server_dialog(e.get("name", "The character"), e.get("what", ""))
            else:
                self.set_status("%s regenerated." % e.get("name", ""))
        elif job == "script":
            self.script_text.delete("1.0", "end")
            self.script_text.insert("1.0", e.get("script", ""))
            self.dirty = True
            self.scan_script()
            self.set_status("Script written.")
        elif job == "voice":
            self.set_status("Dialogue recorded.")
            if e.get("stitched") and messagebox.askyesno(APP, "Dialogue done. Play the full take?"):
                _play(e["stitched"])
        elif job == "storyboard":
            self._refresh_board()
            self.set_status("Storyboard ready: %d shots drawn." % e.get("drawn", 0))
            children = self.board_tree.get_children()
            if children and not self.board_tree.selection():
                # only on a first full draw - never steal the row being edited
                self.board_tree.selection_set(children[0])
        elif job == "movie":
            if e.get("movie"):
                self.set_status("Movie ready: %s" % e["movie"])
                if messagebox.askyesno(APP, "The movie is cut.\n\n%s\n\nPlay it now?"
                                            % e["movie"]):
                    _play(e["movie"])
            else:
                self.set_status("Assets ready (no ffmpeg, so no movie file).")
                messagebox.showinfo(
                    APP, "Everything rendered: %d lines, %d shots, the full audio take and "
                         "movie.edl.json.\n\nffmpeg wasn't found, so the .mp4 wasn't muxed. "
                         "Install ffmpeg and press Make the movie again - the rendered pieces "
                         "are re-used, so it only does the cut."
                         % (e.get("lines", 0), e.get("shots", 0)))

    def _no_server_dialog(self, name, what):
        """Explain a half-done regenerate in terms of the button that was pressed."""
        addr = "%s:%s" % (self.project["server"]["host"], self.project["server"]["port"])
        got = {"actor": "%s has been rewritten - who they are, how they look on paper "
                        "and how they sound." % name,
               "voice": "%s has a new voice written for them." % name}.get(
                   what, "Nothing happened.")
        self.set_status("Nothing drawn - ComfyUI is not answering on %s." % addr)
        messagebox.showwarning(
            APP, "%s\n\nNo picture was drawn, because ComfyUI is not answering on %s.\n\n"
                 "Start ComfyUI, then press Test connection on the Setup tab. If it is "
                 "already running it may be on a different port - Test connection will "
                 "find it. Then press New look on this actor to draw them." % (got, addr))

    def _cue_state(self, index, column, value, tag=None):
        iid = str(index)
        if self.cue_tree.exists(iid):
            self.cue_tree.set(iid, column, value)
            self.cue_tree.see(iid)
            if tag:
                self.cue_tree.item(iid, tags=(tag,))

    # ------------------------------------------------------------- utilities

    def _set_conn(self, comfy=None, model=None):
        cur = self.conn.get()
        c = comfy if comfy is not None else cur.split("|")[0].replace("ComfyUI:", "").strip()
        m = model if model is not None else cur.split("|")[-1].replace("model:", "").strip()
        self.conn.set("ComfyUI: %s   |   model: %s" % (c or "not checked", m or "not checked"))

    def set_status(self, msg):
        self.status.set(msg)

    def _log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", str(msg).rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _log_clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def show_help(self):
        messagebox.showinfo(APP, HELP_TEXT)

    def _on_close(self):
        if self.busy():
            if not messagebox.askyesno(APP, "A job is running. Quit anyway?"):
                return
            self.job.cancel()
        if self._confirm_discard():
            self.master.destroy()


class _ScriptJob(Worker):
    """Premise + approved cast -> screenplay."""

    kind = "script"

    def __init__(self, project, on_event):
        Worker.__init__(self, on_event)
        self.project = project

    def execute(self):
        from .pipeline import make_llm
        llm = make_llm(self.project)
        self.step("Writing the screenplay")
        text = casting.write_script(
            llm, self.project.get("premise", ""), proj.cast(self.project),
            scenes=int((self.project.get("options") or {}).get("scene_count", 4)),
            cancel=self.cancelled)
        self.result = {"script": text}


HELP_TEXT = """\
ScriptVoice makes a film offline: a local LLM casts it, ComfyUI draws and speaks it.

1. Premise - describe the film in a sentence or two and point the app at your local
   model server (LM Studio, Ollama, llama.cpp - press Detect). "Create the cast" then
   invents the characters, says in a sentence what each one does in the script,
   renders a portrait and a full 360 turnaround, and records a voice sample.

2. Cast - one card per actor, with what they do in the script and how many lines
   they have. Find in script highlights their lines on the Script tab, which is the
   quickest way to tell who MAN or INVESTOR actually is. Regenerate the actor (a different
   person, same role), regenerate the voice (same person, new voice), or ask for a new
   look. Drag the turnaround to spin it. Tick Approved when you're happy; the movie
   button unlocks once the whole cast is approved. Every character is locked to a fixed
   appearance and a fixed seed, so they stay the same person in every shot.

3. Script - write it yourself, or have the AI write it for the cast you approved.

4. Workflows - one ComfyUI API workflow per job: voice (required), portrait, turnaround
   and shot (optional). Export them from ComfyUI with Dev mode on: Workflow > Export (API).

5. Movie - renders every line through the voice workflow, a shot for each line through
   the picture workflow, and cuts them together with ffmpeg into movie.mp4. Anything
   already rendered is re-used, so changing one line only re-renders that line.
"""


# ---------------------------------------------------------------- small helpers

def _short(v, n=90):
    s = str(v).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def _int(v, default):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def _float(v, default):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def _coerce(s):
    s = str(s).strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _play(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        else:
            webbrowser.open("file://" + path)
    except Exception as e:
        messagebox.showerror(APP, "Couldn't play %s:\n%s" % (path, e))


def _ask_choice(parent, title, prompt, choices):
    """A small modal that returns one of `choices`, or "" if cancelled."""
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent.winfo_toplevel())
    win.resizable(False, False)
    frame = ttk.Frame(win, padding=14)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=prompt).pack(anchor="w")
    var = tk.StringVar(value=choices[0] if choices else "")
    box = ttk.Combobox(frame, textvariable=var, values=list(choices),
                       state="readonly", width=32)
    box.pack(anchor="w", pady=(8, 12))
    box.focus_set()
    out = {"value": ""}

    def take():
        out["value"] = var.get()
        win.destroy()

    row = ttk.Frame(frame)
    row.pack(anchor="e")
    ttk.Button(row, text="OK", command=take).pack(side="left")
    ttk.Button(row, text="Cancel", command=win.destroy).pack(side="left", padx=6)
    win.bind("<Return>", lambda e: take())
    win.bind("<Escape>", lambda e: win.destroy())
    win.grab_set()
    parent.wait_window(win)
    return out["value"]


def _ask_string(parent, title, prompt):
    from tkinter import simpledialog
    return simpledialog.askstring(title, prompt, parent=parent)


def fit_rows_to_font(root):
    """Make table rows tall enough for the font actually in use.

    Tk's default Treeview row height is a fixed 20 pixels, so on a high-DPI
    display the text of one row overlaps the next.
    """
    from tkinter import font as tkfont
    try:
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
    except tk.TclError:
        return
    ttk.Style().configure("Treeview", rowheight=max(20, int(line * 1.45)))


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform.startswith("win") else "clam")
    except tk.TclError:
        pass
    fit_rows_to_font(root)
    App(root, reopen=True)
    root.mainloop()
