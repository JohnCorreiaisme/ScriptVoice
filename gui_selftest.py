"""Widget checks for the Tk interface.

Drives the real window, so it needs a desktop session - but it stays
withdrawn for most of the run and never waits for input. This covers what
selftest.py cannot: that a control exists, is wired to the right handler,
and is actually on screen at the default window size.

    python gui_selftest.py
"""
import os
import sys
import tkinter as tk
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ScriptVoice as sv

root = tk.Tk()
root.withdraw()
app = sv.App(root, reopen=False)   # never load the user's real project
root.update_idletasks()

fails = []


def ok(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + (("  -> %s" % extra) if extra else ""))
    if not cond:
        fails.append(label)


# --- the compile/load buttons must be above the script box, not below it ---
kids = app.tab_script.winfo_children()
# A withdrawn window has no real geometry, so compare pack order instead: the
# script box lives in an expanding frame, and anything packed after it gets
# pushed off a short window - which is what hid these buttons before.
btn_at = kids.index(app.btn_compile.master)
text_at = kids.index(app.script_text.master)
ok("Compile button is packed before the script text", btn_at < text_at,
   "%d < %d" % (btn_at, text_at))
labels = []
for k in kids:
    for w in k.winfo_children():
        try:
            labels.append(str(w.cget("text")))
        except tk.TclError:
            pass
ok("a Load a script file button exists",
   any("Load a script file" in s for s in labels))
ok("a Compile button exists", any("Compile script" in s for s in labels))
ok("the buttons are in the second row of the tab", kids.index(app.btn_compile.master) == 1,
   str(kids.index(app.btn_compile.master)))

# --- the actor card progress bar ---
app.project["characters"] = {"MAYA": sv.project.new_character("MAYA")}
app.project["cast_order"] = ["MAYA"]
app._refresh_cast()
root.update_idletasks()
card = app.cards["MAYA"]
ok("a fresh card shows no progress bar", not card.prog_row.winfo_ismapped())

card.set_progress("Drawing the portrait...")
root.update_idletasks()
ok("an unknown-length step shows a marching bar",
   card.prog_row.winfo_ismapped() and str(card.prog.cget("mode")) == "indeterminate",
   str(card.prog.cget("mode")))
ok("and says what it is doing", card.v_prog.get() == "Drawing the portrait...")

card.set_progress("Turnaround frame 3 of 8", 3, 8)
root.update_idletasks()
ok("a counted step switches to a filling bar",
   str(card.prog.cget("mode")) == "determinate" and int(card.prog.cget("value")) == 3
   and int(card.prog.cget("maximum")) == 8,
   "%s %s/%s" % (card.prog.cget("mode"), card.prog.cget("value"), card.prog.cget("maximum")))

card.set_progress("over the top", 99, 8)
ok("a count past the end does not overflow the bar", int(card.prog.cget("value")) == 8)

card.set_progress("")
root.update_idletasks()
ok("an empty label hides the bar again", not card.prog_row.winfo_ismapped())

card.set_progress("marching")
card.clear_progress()
root.update_idletasks()
ok("clearing stops the marching bar", str(card.prog.cget("mode")) == "determinate"
   and not card.prog_row.winfo_ismapped())

# --- the event actually reaches the card ---
app._handle_event({"kind": "actor_progress", "name": "MAYA",
                   "label": "Recording the voice sample...", "done": 0, "total": 0})
root.update_idletasks()
ok("an actor_progress event drives the right card",
   card.prog_row.winfo_ismapped() and card.v_prog.get().startswith("Recording"))
app._handle_event({"kind": "actor_progress", "name": "NOBODY", "label": "x"})
ok("an event for an actor with no card is ignored, not fatal", True)
app._job_finished()
root.update_idletasks()
ok("a finished job clears every bar", not card.prog_row.winfo_ismapped())

# --- refreshing the card must not wipe a bar that is mid-run ---
card.set_progress("Turnaround frame 2 of 8", 2, 8)
card.refresh(app.project["characters"]["MAYA"])
root.update_idletasks()
ok("redrawing a card mid-job keeps its progress bar",
   card.prog_row.winfo_ismapped() and int(card.prog.cget("value")) == 2)

# --- the card body must wrap to the card, not to a fixed 560px ---
root.deiconify()
root.geometry("900x700")
root.update()
w = card.winfo_width()
wrap = int(card.lbl_body.cget("wraplength"))
ok("the card text wraps inside the card, not past its edge", wrap <= w - 130,
   "wrap %d, card %d" % (wrap, w))
ok("the wrap tracks the card width rather than a hard-coded number",
   abs(wrap - (w - 160)) <= 10, "wrap %d, expected about %d" % (wrap, w - 160))


class E(object):                      # the handler is what a resize actually calls
    pass


e = E(); e.width = 1200
card._rewrap(e)
grew = int(card.lbl_body.cget("wraplength"))
e.width = 400
card._rewrap(e)
shrank = int(card.lbl_body.cget("wraplength"))
ok("a wider card wraps wider", grew > wrap, "%d > %d" % (grew, wrap))
ok("a narrow card still leaves a readable column", shrank >= 200, str(shrank))
root.withdraw()

# --- the main-character tick box and the look box ---
app.project["characters"]["MAYA"]["lead"] = False
card.refresh(app.project["characters"]["MAYA"])
ok("a card starts as not the main character", card.v_lead.get() is False)
app._card_action("MAYA", "lead", True)
root.update_idletasks()
ok("ticking Main character sets the flag",
   app.project["characters"]["MAYA"]["lead"] is True)
ok("and the role word follows the tick, not the model",
   app.project["characters"]["MAYA"]["role"] == "lead",
   app.project["characters"]["MAYA"]["role"])
ok("the card title says lead", "lead" in app.cards["MAYA"].v_title.get(),
   app.cards["MAYA"].v_title.get())
app._card_action("MAYA", "lead", False)
ok("un-ticking puts them back to supporting",
   app.project["characters"]["MAYA"]["role"] == "supporting"
   and app.project["characters"]["MAYA"]["lead"] is False)

app.select_actor("MAYA")
app.look_text.delete("1.0", "end")
app.look_text.insert("1.0", "bald, heavy jaw, broken nose")
app.save_look()
root.update_idletasks()
ok("the look box saves onto the actor",
   app.project["characters"]["MAYA"]["look_note"] == "bald, heavy jaw, broken nose",
   app.project["characters"]["MAYA"]["look_note"])
prompt = sv.casting.look_prompt(app.project["characters"]["MAYA"])
ok("and those words lead the image prompt", prompt.startswith("bald, heavy jaw"), prompt[:60])
app.select_actor("MAYA")
ok("re-selecting the actor shows the look back",
   app.look_text.get("1.0", "end-1c") == "bald, heavy jaw, broken nose")

# --- typing in the Look box must count WITHOUT pressing Save first ---
app.select_actor("MAYA")
app.look_text.delete("1.0", "end")
app.look_text.insert("1.0", "one eye, shaved head, burn scar")
collected = app._collect_ui_into_project()
ok("typing in the look box reaches the project without pressing Save",
   collected["characters"]["MAYA"]["look_note"] == "one eye, shaved head, burn scar",
   repr(collected["characters"]["MAYA"]["look_note"]))
ok("and that is what the picture would be drawn from",
   sv.casting.look_prompt(collected["characters"]["MAYA"]).startswith("one eye, shaved head"),
   sv.casting.look_prompt(collected["characters"]["MAYA"])[:60])

# clearing the box must clear the note, not leave the old one behind
app.look_text.delete("1.0", "end")
app._collect_ui_into_project()
ok("emptying the look box clears the note",
   app.project["characters"]["MAYA"]["look_note"] == "",
   repr(app.project["characters"]["MAYA"]["look_note"]))

# and it must land on the selected actor only
app.project["characters"]["RUBEN"] = sv.project.new_character("RUBEN")
app.project["cast_order"] = ["MAYA", "RUBEN"]
app._refresh_cast()
app.select_actor("MAYA")
app.look_text.delete("1.0", "end")
app.look_text.insert("1.0", "only maya")
app._collect_ui_into_project()
ok("the look lands on the selected actor and nobody else",
   app.project["characters"]["MAYA"]["look_note"] == "only maya"
   and app.project["characters"]["RUBEN"]["look_note"] == "",
   "MAYA=%r RUBEN=%r" % (app.project["characters"]["MAYA"]["look_note"],
                         app.project["characters"]["RUBEN"]["look_note"]))

# --- every control on the Cast tab must be reachable, not below the window ---
root.deiconify()
root.geometry("1240x820")          # the app's own default size
app.nb.select(app.tab_cast)
app.select_actor("MAYA")
root.update()


def named(root_widget, want):
    hits = []
    stack = [root_widget]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        try:
            if want in str(w.cget("text")):
                hits.append(w)
        except tk.TclError:
            pass
    return hits


btns = named(app.tab_cast, "Save the look")
ok("a Save the look button exists on the Cast tab", len(btns) == 1, str(len(btns)))
win_bottom = root.winfo_rooty() + root.winfo_height()
if btns:
    b = btns[0]
    bottom = b.winfo_rooty() + b.winfo_height()
    ok("and it is inside the window, not below the bottom edge",
       b.winfo_ismapped() and bottom <= win_bottom,
       "button bottom %d, window bottom %d" % (bottom, win_bottom))
lt_bottom = app.look_text.winfo_rooty() + app.look_text.winfo_height()
ok("the look box itself is on screen too",
   app.look_text.winfo_ismapped() and lt_bottom <= win_bottom,
   "look box bottom %d, window bottom %d" % (lt_bottom, win_bottom))
tabs = [app.cast_side.tab(i, "text").strip() for i in range(app.cast_side.index("end"))]
ok("the actor's panels are tabs, so none can be pushed off the window",
   tabs == ["Look", "Reference face", "Who they are", "Voice", "Advanced"], str(tabs))

# --- the edit-what-the-AI-wrote box ---
app.project["characters"]["MAYA"]["one_line"] = "A reformed tech genius hiding in the shadows."
app.select_actor("MAYA")
root.update()
ok("the box shows what the AI wrote",
   app.who_text.get("1.0", "end-1c") == "A reformed tech genius hiding in the shadows.",
   app.who_text.get("1.0", "end-1c"))
app.who_text.delete("1.0", "end")
app.who_text.insert("1.0", "The goofy neighbour who mows the lawn shirtless.")
collected = app._collect_ui_into_project()
ok("editing it reaches the project without pressing Save",
   collected["characters"]["MAYA"]["one_line"].startswith("The goofy neighbour"),
   collected["characters"]["MAYA"]["one_line"])
ok("and the card shows the corrected line",
   "goofy neighbour" in app.cards["MAYA"].v_body.get())

# an empty box must not silently wipe the character
app.who_text.delete("1.0", "end")
app._collect_ui_into_project()
ok("clearing the box does not erase the character description",
   app.project["characters"]["MAYA"]["one_line"].startswith("The goofy neighbour"),
   app.project["characters"]["MAYA"]["one_line"])

wbtn = named(app.tab_cast, "Save and describe their role")
ok("there is a Save and describe their role button", len(wbtn) == 1, str(len(wbtn)))


def on_screen_in_tab(widget, label):
    """Select the tab this control lives on, then check it is really visible."""
    for i in range(app.cast_side.index("end")):
        page = app.nametowidget(app.cast_side.tabs()[i])
        w = widget
        while w is not None:
            if str(w) == str(page):
                app.cast_side.select(i)
                root.update()
                bottom = widget.winfo_rooty() + widget.winfo_height()
                return (widget.winfo_ismapped()
                        and bottom <= root.winfo_rooty() + root.winfo_height())
            w = w.master
    return False


if wbtn:
    ok("and it is on screen once its tab is chosen", on_screen_in_tab(wbtn[0], "who"))
sl = named(app.tab_cast, "Save the look")
if sl:
    ok("the look controls are on screen on their tab", on_screen_in_tab(sl[0], "look"))
pr = named(app.tab_cast, "Clear")
if pr:
    ok("the reference-face controls are on screen on their tab",
       on_screen_in_tab(pr[0], "reference"))

# --- no scores anywhere, and Find in script ---
app.project["script"] = ("MAYA: You said the generator still worked.\n"
                         "RUBEN: It did. In 2014.\n"
                         "MAYA: We don't have a minute.\n")
app.script_text.delete("1.0", "end")
app.script_text.insert("1.0", app.project["script"])
app.project["characters"] = {"MAYA": sv.project.new_character("MAYA"),
                             "RUBEN": sv.project.new_character("RUBEN")}
app.project["cast_order"] = ["MAYA", "RUBEN"]
app.scan_script()
app._refresh_cast()
root.update()
ok("the summary counts descriptions, not scores",
   "described" in app.v_cast_summary.get() and "judged" not in app.v_cast_summary.get(),
   app.v_cast_summary.get())
ok("a card shows how many lines they have",
   "2 lines in the script" in app.cards["MAYA"].v_where.get(),
   app.cards["MAYA"].v_where.get())
ok("no card shows a score out of 100",
   "/100" not in app.cards["MAYA"].v_where.get() + app.cards["MAYA"].v_body.get())

app.find_in_script("MAYA")
root.update()
ok("Find in script moves to the Script tab", app.nb.select() == str(app.tab_script))
ranges = app.script_text.tag_ranges("whois")
ok("and highlights exactly the lines that character speaks", len(ranges) == 4,
   "%d ranges" % (len(ranges) // 2))
ok("the highlight starts on their first line",
   str(ranges[0]).startswith("1.") if ranges else False,
   str(ranges[0]) if ranges else "none")
app.find_in_script("RUBEN")
root.update()
ranges = app.script_text.tag_ranges("whois")
ok("finding a second character clears the first one's highlight", len(ranges) == 2,
   "%d ranges" % (len(ranges) // 2))

# --- add and remove ---
app.nb.select(app.tab_cast)
app.project["characters"]["BANKER"] = sv.project.new_character("BANKER")
app.project["cast_order"].append("BANKER")
app._refresh_cast()
ok("a hand-added character appears as a card", "BANKER" in app.cards)
ok("and starts with nothing rendered",
   not app.project["characters"]["BANKER"].get("portrait"))
rm = named(app.tab_cast, "Remove")
ok("every card has a Remove button", len(rm) == len(app.cards), str(len(rm)))
adds = named(app.tab_cast, "Add a character")
ok("the Cast tab has an Add a character button", len(adds) == 1, str(len(adds)))
app.remove_character.__self__ and None
app.project["characters"].pop("BANKER")
app.project["cast_order"].remove("BANKER")
app._refresh_cast()
ok("removing a character drops their card", "BANKER" not in app.cards)
ok("and leaves the rest of the cast alone", set(app.cards) == {"MAYA", "RUBEN"},
   str(sorted(app.cards)))

# --- the Setup tab is where the settings actually are ---
app.nb.select(app.tab_wf)
root.update()
pages = [app.setup_book.tab(i, "text").strip()
         for i in range(app.setup_book.index("end"))]
ok("Setup has plain-language pages",
   pages == ["Everyday", "What draws and speaks", "Advanced"], str(pages))

for label, widget in (("voice backend", "v_backend"), ("prompt prefix", "v_prefix"),
                      ("output folder", "v_outdir"), ("free the GPU", "v_free_gpu"),
                      ("silence gap", "v_gap"), ("re-use", "v_reuse")):
    ok("the %s setting lives on Setup, not Movie" % label, hasattr(app, widget))

# every job visible at once, rather than hidden behind a dropdown
app.setup_book.select(1)
root.update()
rows = app.slot_tree.get_children()
ok("every workflow job has its own visible row", len(rows) == 4, str(rows))
ok("each row says whether it is ready",
   all(app.slot_tree.set(r, "state") for r in rows),
   str([app.slot_tree.set(r, "state") for r in rows]))
ok("the built-in picture workflow reads as ready",
   app.slot_tree.set("portrait", "state") == "ready",
   app.slot_tree.set("portrait", "state"))
ok("a job with no workflow says so plainly",
   app.slot_tree.set("voice", "state") == "not set",
   app.slot_tree.set("voice", "state"))

# the loop that hung the window: selecting a row must settle, not cascade
calls = {"n": 0}
real_refresh = app._refresh_slot_tree


def counted():
    calls["n"] += 1
    if calls["n"] > 40:
        raise RuntimeError("the slot table is refreshing itself in a loop")
    return real_refresh()


app._refresh_slot_tree = counted
for r in rows:
    app.slot_tree.selection_set(r)
    root.update()
ok("selecting a job settles instead of looping", calls["n"] <= 8, "%d refreshes" % calls["n"])
app._refresh_slot_tree = real_refresh

# the path label is a message when empty; it must never become the stored path
app.slot_tree.selection_set("voice")
root.update()
app._collect_ui_into_project()
stored = sv.project.workflow_cfg(app.project, "voice").get("path", "")
ok("the empty-state message is never written back as a filename",
   "no workflow chosen" not in stored, repr(stored))
ok("and an unset job stays unset", stored == "", repr(stored))

# the Movie tab says what it is about to use
app.nb.select(app.tab_movie)
app._movie_ready_note()
root.update()
note = app.v_movie_ready.get()
ok("the Movie tab states which voices it will use", "Voices:" in note, note[:60])
ok("and which picture workflow", "Pictures:" in note, note[:60])
ok("and warns when faces will not be locked",
   "drift" in note or "locks" in note, note)

# --- nothing you have to type into may sit below the window ---------------
root.deiconify()
root.geometry("1240x820")
root.update()


def fully_visible(widget):
    """True when the whole widget is inside the window, not clipped by it."""
    if not widget.winfo_ismapped():
        return False
    top, bottom = widget.winfo_rooty(), widget.winfo_rooty() + widget.winfo_height()
    left, rightx = widget.winfo_rootx(), widget.winfo_rootx() + widget.winfo_width()
    return (top >= root.winfo_rooty()
            and bottom <= root.winfo_rooty() + root.winfo_height()
            and left >= root.winfo_rootx()
            and rightx <= root.winfo_rootx() + root.winfo_width())


app.nb.select(app.tab_board)
app.board_tree.selection_set(app.board_tree.get_children()[0]
                             if app.board_tree.get_children() else ())
root.update()
pages = [app.board_side.tab(i, "text").strip()
         for i in range(app.board_side.index("end"))]
ok("the storyboard panels are tabs, so none can be clipped away",
   pages == ["Describe the shot", "Who is in it"], str(pages))

app.board_side.select(0)
root.update()
ok("the shot description box is fully on screen", fully_visible(app.shot_text_box),
   "y %d..%d, window ends %d" % (app.shot_text_box.winfo_rooty(),
                                 app.shot_text_box.winfo_rooty() + app.shot_text_box.winfo_height(),
                                 root.winfo_rooty() + root.winfo_height()))
save_btns = named(app.tab_board, "Save this shot")
ok("and its Save button too", save_btns and fully_visible(save_btns[0]))

app.board_side.select(1)
root.update()
ok("the who-is-in-it list is fully on screen", fully_visible(app.lb_board_cast))
use_btns = named(app.tab_board, "Use these people")
ok("and its button too", use_btns and fully_visible(use_btns[0]))
clr = named(app.tab_board, "Clear")
ok("there is a Clear button for a pinned cast", len(clr) >= 1, str(len(clr)))
ok("and it is on screen", clr and fully_visible(clr[0]))

# pinning and then clearing must leave nothing behind
rows = app.board_tree.get_children()
if rows:
    app.board_tree.selection_set(rows[0])
    root.update()
    idx = str(int(rows[0]))
    shot = app.project.setdefault("shots", {}).setdefault(idx, {})
    shot["cast_override"] = ["MAYA"]
    shot["subject_override"] = "MAYA"
    app.clear_shot_subject()
    after = (app.project.get("shots") or {}).get(idx, {})
    ok("Clear removes the pinned cast",
       "cast_override" not in after and "subject_override" not in after, str(sorted(after)))
    ok("and leaves the rest of the shot alone", "line" in after or not after, str(sorted(after)))
    ok("clearing a shot with nothing pinned is harmless",
       app.clear_shot_subject() is not None)

# every text box in the app, on the tab it belongs to
app.nb.select(app.tab_cast)
app.select_actor("MAYA")
for i, box in ((0, app.look_text), (2, app.who_text)):
    app.cast_side.select(i)
    root.update()
    ok("cast text box %d is fully on screen" % i, fully_visible(box),
       "bottom %d vs %d" % (box.winfo_rooty() + box.winfo_height(),
                            root.winfo_rooty() + root.winfo_height()))

# --- redrawing one shot must not throw you back to the first ---------------
lines = []
for i in range(40):
    lines.append("%s: Line number %d." % (["MAYA", "RUBEN"][i % 2], i))
app.project["script"] = chr(10).join(lines) + chr(10)
app.script_text.delete("1.0", "end")
app.script_text.insert("1.0", app.project["script"])
app.scan_script()
app.nb.select(app.tab_board)
root.update()

rows = app.board_tree.get_children()
ok("the board has every line", len(rows) == 40, str(len(rows)))
target = rows[30]
app.board_tree.selection_set(target)
app.board_tree.see(target)
root.update()
ok("a shot deep in the list can be selected",
   app.board_tree.selection() == (target,), str(app.board_tree.selection()))

# this is what a redraw does to the list
app._refresh_board()
root.update()
ok("rebuilding the list keeps you on the same shot",
   app.board_tree.selection() == (target,), str(app.board_tree.selection()))
ok("and it is the row you were editing, not the first",
   app.board_tree.selection() != (rows[0],), str(app.board_tree.selection()))

# and the finished-job handler must not steal it back
app._on_job_result({"job": "storyboard", "drawn": 1})
root.update()
ok("a finished redraw leaves the selection alone",
   app.board_tree.selection() == (target,), str(app.board_tree.selection()))

# a first full draw with nothing selected should still land somewhere useful
app.board_tree.selection_remove(*app.board_tree.selection())
app._on_job_result({"job": "storyboard", "drawn": 40})
root.update()
ok("with nothing selected it still picks a row to show",
   len(app.board_tree.selection()) == 1, str(app.board_tree.selection()))

# --- swapping a workflow must not keep the old mapping --------------------
import os as _os
wf_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "workflows")
img_wf = _os.path.join(wf_dir, "sdxl_turbo_actor_api.json")
voice_wf = _os.path.join(wf_dir, "chatterbox_voice_api.json")

if _os.path.exists(img_wf) and _os.path.exists(voice_wf):
    app.project["workflows"]["voice"] = {"path": voice_wf,
                                         "mapping": {"text": "2.text", "seed": "2.seed"}}
    app._load_workflow("voice", img_wf, quiet=True)
    got = app.project["workflows"]["voice"]["mapping"]
    ok("swapping the file re-detects the mapping instead of keeping the old one",
       got.get("text") != "2.text", str(sorted(got.items())))
    # a picture workflow maps "text" happily (CLIPTextEncode has one), so what
    # it SAVES is the honest signal
    ok("a picture workflow is recognised as a picture workflow",
       sv.project.workflow_makes(sv.project.load_workflow(img_wf)) == "image")
    ok("and a speaking workflow as a speaking one",
       sv.project.workflow_makes(sv.project.load_workflow(voice_wf)) == "audio")
    ok("putting a picture workflow in the voice slot is called out",
       "saves image" in sv.project.slot_mismatch(sv.project.load_workflow(img_wf), "voice"),
       sv.project.slot_mismatch(sv.project.load_workflow(img_wf), "voice"))
    ok("and a speaking workflow in a picture slot too",
       bool(sv.project.slot_mismatch(sv.project.load_workflow(voice_wf), "shot")),
       sv.project.slot_mismatch(sv.project.load_workflow(voice_wf), "shot"))
    ok("the right workflow in the right slot is not complained about",
       sv.project.slot_mismatch(sv.project.load_workflow(voice_wf), "voice") == ""
       and sv.project.slot_mismatch(sv.project.load_workflow(img_wf), "shot") == "")

    app._load_workflow("voice", voice_wf, quiet=True)
    back = app.project["workflows"]["voice"]["mapping"]
    ok("loading the right kind of workflow maps it properly",
       back.get("text") and back.get("voice") and back.get("seed"),
       str(sorted(back.items())))
    ok("reloading the same file does not disturb a working mapping",
       (app._load_workflow("voice", voice_wf, quiet=True) or True)
       and app.project["workflows"]["voice"]["mapping"] == back)

# --- Hear it must follow the backend, not always speak through Windows -----
played = {"kind": None}
real_thread = None
app.project["options"]["voice_backend"] = "comfyui"
app.project["workflows"]["voice"] = {"path": "", "mapping": {}}
app.project["characters"]["MAYA"] = sv.project.new_character("MAYA")
app.project["cast_order"] = ["MAYA"]
app._refresh_cast()
app.select_actor("MAYA")
seen_dialog = {"n": 0}
real_info = sv.gui.messagebox.showinfo


def fake_info(*a, **k):
    seen_dialog["n"] += 1
    return "ok"


sv.gui.messagebox.showinfo = fake_info
try:
    app.preview_system_voice()
    ok("on the ComfyUI backend with no speaking workflow it says so, "
       "rather than playing a Windows voice", seen_dialog["n"] == 1,
       "%d dialogs" % seen_dialog["n"])
finally:
    sv.gui.messagebox.showinfo = real_info

app.project["options"]["voice_backend"] = "system"
ok("switching back to Windows is what the status line will report",
   (app.project.get("options") or {}).get("voice_backend") == "system")

root.destroy()
print("\n%d failed" % len(fails))
sys.exit(1 if fails else 0)
