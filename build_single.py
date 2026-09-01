"""Flatten the scriptvoice package into one self-contained ScriptVoice.py.

    python build_single.py

The generated file is the shippable program: pure standard library, no package
directory, no installer. Edit the modules in scriptvoice/ and re-run this.
"""

import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "scriptvoice")
OUT = os.path.join(HERE, "ScriptVoice.py")

# dependency order: a module may only be listed after everything it subclasses
ORDER = ["audio", "runtime", "speech", "script_parser", "llm", "comfy", "casting", "project", "jobs",
         "worker", "visuals", "movie", "render", "pipeline", "widgets", "gui"]

# module-qualified names used inside the package, e.g. casting.look_prompt(...)
ALIASES = ["audio", "runtime", "speech", "script_parser", "llm", "llm_mod", "comfy", "casting", "project",
           "proj", "comfy_mod", "jobs", "worker", "visuals", "movie", "movie_mod", "render",
           "pipeline", "widgets", "gui"]

PKG_IMPORT = re.compile(r"^(\s*)from\s+\.\s*\w*\s+import\s+.*$|^(\s*)from\s+\.\w+\s+import\s+.*$")
# a package import that opens a bracket and continues on the next lines
PKG_IMPORT_OPEN = re.compile(r"^\s*from\s+\.\w*\s+import\s+\([^)]*$")

HEADER = '''\
"""ScriptVoice - an offline AI film studio in a single file.

A local LLM casts the film from your premise, judges each character against the
plot, and a local ComfyUI renders their portraits, a 360 turnaround, their voices,
and finally the movie itself. Nothing leaves the machine.

    python ScriptVoice.py

Requires: Python 3.8+ with tkinter (both ship with python.org installers).
Optional: Pillow for nicer image scaling and animated turnaround GIFs;
          ffmpeg on PATH to mux the final movie.mp4.

This file is generated from the scriptvoice/ package by build_single.py -
edit there and rebuild, or edit here and keep it, whichever you prefer.
"""
'''

FOOTER = '''

# --------------------------------------------------------------------------
# In the package these were separate modules; flattened into one file they all
# live in this namespace, so module-qualified references point back at it.
# --------------------------------------------------------------------------
import sys as _sys
_self = _sys.modules[__name__]
%s

if __name__ == "__main__":
    main()
'''


def module_body(name):
    path = os.path.join(PKG, name + ".py")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    kept = []
    carry = False
    for line in src.split("\n"):
        if carry:
            # continuation of a bracketed package import; comment it out too,
            # or the flattened file is left with a stray "  speech)" line
            kept.append("#   %s" % line.strip())
            carry = line.count(")") <= line.count("(")
            continue
        if PKG_IMPORT.match(line) or PKG_IMPORT_OPEN.match(line):
            indent = re.match(r"^\s*", line).group(0)
            kept.append("%s# (flattened) %s" % (indent, line.strip()))
            carry = PKG_IMPORT_OPEN.match(line) is not None
            continue
        kept.append(line)
    body = "\n".join(kept).strip("\n")
    # the module docstring becomes a section comment
    tree = ast.parse(src)
    doc = ast.get_docstring(tree) or name
    return doc.strip().split("\n")[0], body


def top_level_names(src, module):
    """Every name this module defines at top level, for collision detection."""
    names = []
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.append(t.id)
    return [(n, module) for n in names]


def main():
    seen = {}
    clashes = []
    chunks = [HEADER]
    for name in ORDER:
        path = os.path.join(PKG, name + ".py")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        for n, mod in top_level_names(src, name):
            if n in seen and seen[n] != name and not n.startswith("__"):
                clashes.append("%s: defined in %s and %s" % (n, seen[n], name))
            seen[n] = name
        title, body = module_body(name)
        chunks.append("\n\n# %s\n# %s: %s\n# %s\n\n%s\n"
                      % ("=" * 74, name, title, "=" * 74, body))

    ignorable = {"_int_key", "_read_json", "_write_json", "Image", "ImageTk",
                 "HAVE_PIL", "VERSION", "APP"}
    real = [c for c in clashes if c.split(":")[0] not in ignorable]
    if real:
        print("Name collisions would break the flattened build:")
        for c in real:
            print("  " + c)
        return 1
    if clashes:
        print("Harmless duplicate definitions (identical in both modules):")
        for c in clashes:
            print("  " + c)

    alias_line = " = ".join(ALIASES) + " = _self"
    chunks.append(FOOTER % alias_line)
    text = "".join(chunks)

    ast.parse(text)                      # never ship a file that doesn't parse
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print("Wrote %s  (%d lines, %.0f KB)"
          % (OUT, text.count("\n") + 1, len(text.encode("utf-8")) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
