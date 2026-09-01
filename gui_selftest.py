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
app = sv.App(root)
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
ok("the side column scrolls, so the panels below it are reachable",
   hasattr(app, "cast_side") and app.cast_side.sb.winfo_exists())

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
if wbtn:
    b = wbtn[0]
    ok("and it is on screen at the default window size",
       b.winfo_ismapped()
       and b.winfo_rooty() + b.winfo_height() <= root.winfo_rooty() + root.winfo_height())

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

root.destroy()
print("\n%d failed" % len(fails))
sys.exit(1 if fails else 0)
