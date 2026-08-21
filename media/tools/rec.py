#!/usr/bin/env python3
"""Drive g023 Code inside a PTY, record output with timing to a .cast-like JSON."""
import json, os, pty, select, signal, sys, time, fcntl, termios, struct, argparse

COLS, ROWS = 100, 30

# The actual modal permission-prompt lines. When deny_on_prompt is set, a prompt
# matching one of these is answered "n" so a scene stays read-only no matter
# what the agent tries (a reroll of the model's plan should not reroll the
# screenshots). Only the prompt lines themselves are matched — the trace header
# ("Permission required …") appears earlier, and reacting to it races the
# prompt's read and can leak the "n" into the conversation as a user message.
PROMPT_MARKERS = ("Allow?", "Fetch it?")


def record(cmd, script, out_path, cwd, cols=COLS, rows=ROWS, idle_cap=2.0,
           deny_on_prompt=False):
    env = dict(os.environ)
    env.update(TERM="xterm-256color", COLUMNS=str(cols), LINES=str(rows),
               PYTHONUNBUFFERED="1", FORCE_COLOR="1")
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvpe(cmd[0], cmd, env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    events = []
    t0 = time.time()
    buf = ""       # recently echoed text, used for prompt detection
    checked = 0    # index into buf already reacted to (a prompt is answered once)
    armed = False  # deny only once an ("arm",) step has run — e.g. after the
                   # edit scene's own WriteFile approval has been sent

    def pump(duration):
        nonlocal buf, checked, armed
        end = time.time() + duration
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], min(0.05, max(0.0, end - time.time())))
            if fd in r:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    return False
                if not data:
                    return False
                text = data.decode("utf-8", "replace")
                if "\x1b[6n" in text:  # answer cursor-position requests (prompt_toolkit CPR)
                    os.write(fd, f"\x1b[{rows};1R".encode())
                events.append([round(time.time() - t0, 3), "o", text])
                if deny_on_prompt and armed:
                    buf += text
                    if len(buf) > 8192:
                        buf = buf[-8192:]
                        checked = max(0, checked - (8192 - len(buf)))
                    # overlap the boundary so a marker split across chunks is seen
                    if any(m in buf[max(0, checked - 2):] for m in PROMPT_MARKERS):
                        os.write(fd, b"n\r")
                        checked = len(buf)
        return True

    for step in script:
        kind = step[0]
        payload = step[1] if len(step) > 1 else None
        if kind == "mark":
            events.append([round(time.time() - t0, 3), "m", payload])
            continue
        if kind == "arm":
            armed = True
            continue
        if kind == "wait":
            if not pump(payload):
                break
        elif kind == "type":
            delay = step[2] if len(step) > 2 else 0.045
            for ch in payload:
                os.write(fd, ch.encode())
                pump(delay)
        elif kind == "key":
            os.write(fd, payload.encode())
            pump(step[2] if len(step) > 2 else 0.35)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    os.close(fd)
    with open(out_path, "w") as f:
        json.dump({"cols": cols, "rows": rows, "events": events}, f)
    return out_path

if __name__ == "__main__":
    print("module")
