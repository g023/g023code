#!/usr/bin/env python3
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rec import record

S = os.path.dirname(os.path.abspath(__file__))
# Sanitised paths for public screenshots: no user home dir, no machine identity.
# The demo project is a throwaway FastAPI todo service copied to /tmp for the
# shoot; the app install lives next to it under a neutral folder name.
PROJ = "/tmp/g023-shoot/todo-api"
APP = ["bash", "/tmp/g023-shoot/home/g023.sh"]

def cmd(text, wait=2.0, mark=None):
    steps = [("type", text), ("key", "\r", wait)]
    if mark:
        steps.append(("mark", mark))
    return steps

SCENES = {
    "tour": [("wait", 5.0), ("mark", "banner")]
      + cmd("/help", 3.0, "help")
      + cmd("/tools", 3.0, "tools")
      + cmd("/status", 3.0, "status")
      + cmd("/model", 3.0, "model")
      + cmd("/thinking", 3.0, "thinking_picker") + [("type", "1"), ("key", "\r", 2.0)]
      + cmd("/cost", 3.0, "cost"),
    "session": [("wait", 5.0), ("mark", "banner"), ("arm",)]
      + cmd("Explain what this project does and how the API layer talks to the store.", 95.0, "answer")
      + cmd("/context", 4.0, "context")
      + cmd("/cost", 4.0, "cost"),
    "edit": [("wait", 5.0), ("mark", "banner")]
      + [("type", "Add a one-line module docstring to app/store.py describing what it holds."),
         ("key", "\r", 70.0), ("mark", "permission")]
      + [("type", "y"), ("arm",), ("key", "\r", 45.0), ("mark", "written")]
      + cmd("/cost", 4.0, "cost"),
    "ollama": [("wait", 5.0), ("mark", "banner")]
      + cmd("/ollama", 5.0, "ollama")
      + cmd("/vision", 4.0, "vision_picker") + [("type", "1"), ("key", "\r", 2.0)]
      + cmd("/settings", 4.0, "settings")
      + cmd("/cache", 4.0, "cache"),
    "panels": [("wait", 5.0), ("mark", "banner")]
      + cmd("/signals", 4.0, "signals")
      + cmd("/surge", 4.0, "surge")
      + cmd("/or", 6.0, "or")
      + cmd("/config", 4.0, "config"),
}

if __name__ == "__main__":
    name = sys.argv[1]
    out = f"{S}/casts/{name}.json"
    os.makedirs(f"{S}/casts", exist_ok=True)
    # session and edit are live agent turns. Session arms the deny at once so
    # any permission prompt the model raises (e.g. a Bash inventory) is answered
    # "n" and the shot ends on the read-only answer; edit arms it only after
    # the scene's own WriteFile approval has been sent, so a post-write
    # verification command is denied instead of stranding the next keystrokes.
    deny = name in ("session", "edit")
    record(APP, SCENES[name], out, PROJ, cols=100, rows=32, deny_on_prompt=deny)
    print("recorded", out)
