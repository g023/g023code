# g023 Code — Release Media

Screenshots and screen recordings captured from real sessions against a small
demo project (a four-file FastAPI todo service). Nothing here is mocked up:
every frame is actual TUI output, including live DeepSeek turns, token
counters, and cost figures.

> **The screenshots were re-shot on 1.3.3** — see the banner in
> `01-startup-banner.png`. They show the current build: `/model` no longer
> offers a picker (flash is the only model `/responses` serves), and the
> `web_search` tool has no permission entry because DeepSeek runs it
> server-side. New frames cover the 1.3.3 panels — `/signals`, `/surge`,
> `/or`, and `/config` (16–19).
>
> The captures use **sanitised paths** (`/tmp/g023-shoot/...`) so nothing in
> them exposes a machine's real home directory, user name, or API keys — the
> shot set is safe to publish as-is.

Terminal: 100 × 32 cells, DejaVu Sans Mono, dark theme.

## Screenshots (`screenshots/`)

| File | Shows |
|------|-------|
| `01-startup-banner.png` | Launch banner — project/home paths, model, thinking level, Ollama probe |
| `02-help-catalogue.png` | `/help` — the full command catalogue, grouped |
| `03-tools-and-permissions.png` | `/tools` — every tool with its permission level and subagent isolation |
| `04-status.png` | `/status` — session, vision, Ollama, project, cache summary |
| `05-model.png` | `/model` — flash is the only model the Responses API serves, so there is nothing to switch to |
| `06-thinking-picker.png` | `/thinking` — reasoning-effort picker (low / high / max / off) |
| `07-agent-answer.png` | A real answer: tool trace, then a project explanation with syntax-highlighted inline code |
| `08-context-and-spend.png` | `/context` — what is filling the window, and the cheapest fix |
| `09-usage-and-cost.png` | `/cost` — per-model call/token/cost breakdown, cache hit/miss split and price card |
| `10-permission-prompt.png` | A `WriteFile` call pausing for approval (`y` / `a` / `n`) with the content excerpt |
| `11-file-written.png` | The approved write completing, with the auto-backup line and the tool-call trail |
| `12-ollama-daemon.png` | `/ollama` — daemon host, reachability, version, latency, model count |
| `13-vision-picker.png` | `/vision` — the picker of the daemon's installed, vision-capable models |
| `14-settings.png` | `/settings` — effective configuration and where each value came from |
| `15-cache.png` | `/cache` — SQLite cache stats for the project |
| `16-signals.png` | `/signals` — prefix hit-rate by day, unknown item types, empty responses |
| `17-surge-pricing.png` | `/surge` — peak/off-peak windows on the local clock, countdown, pause window |
| `18-openrouter.png` | `/or` — OpenRouter vision: key present, tier, ceiling, model list, usage |
| `19-config-scopes.png` | `/config` — global vs project config files and which scope is active |

## Videos (`videos/`)

Re-recorded on 1.3.3 from the same sanitised casts as the screenshots — no
private paths, no keys.

| File | Length | Content |
|------|--------|---------|
| `quickstart.gif` | ~28 s | Startup → `/help` → `/tools` → `/status` → `/model` → `/thinking` → `/cost`. README-sized loop (860 px wide) |
| `01-command-tour.mp4` | ~28 s | `/help`, `/tools`, `/status`, `/model`, `/thinking`, `/cost` |
| `02-agent-session.mp4` | ~113 s | A full read-only agent turn: file reads, streamed answer, then `/context` and `/cost` |
| `03-file-edit-with-permission.mp4` | ~129 s | Agent proposes a `WriteFile`, waits for approval, applies it, with the auto-backup line |
| `04-ollama-and-settings.mp4` | ~27 s | `/ollama`, `/vision` picker, `/settings`, `/cache` |
| `05-panels-1.3.3.mp4` | ~26 s | The 1.3.3 panels: `/signals`, `/surge`, `/or`, `/config` |

MP4: H.264, yuv420p, 12 fps, 1044 × 846 — plays inline on GitHub and in any
browser.

## How these were captured (`tools/`)

The app is a terminal program, so capture is done by driving it inside a real
PTY and re-rendering the byte stream, rather than by pointing a camera at a
window. That keeps output crisp at any size and makes the whole set
reproducible.

- `rec.py` — forks a PTY, runs `g023.sh`, types scripted keystrokes with human
  timing, and records `(timestamp, output)` events. It answers cursor-position
  requests so `prompt_toolkit` renders its bottom status bar normally, and
  supports `mark` events that name the moments worth screenshotting. The
  optional `deny_on_prompt` flag answers permission prompts with `n` — armed
  by an `("arm",)` scene step — so a scene stays read-only no matter what the
  model tries; a reroll of the model's plan should not reroll the screenshots.
- `scenes.py` — the scripted sessions (`tour`, `session`, `edit`, `ollama`,
  `panels`) and their marks.
- `render.py` — replays a recording through a `pyte` terminal emulator and
  paints each cell with Pillow. Box-drawing glyphs are drawn geometrically so
  borders join seamlessly, block glyphs are drawn as blended fills, and emoji
  come from Noto Color Emoji. `marks` mode writes one cropped still per mark;
  `frames` mode writes video frames.
- `frames.py` — renders video frames across all cores.

Regenerating, from a checkout with `pyte` and `Pillow` installed:

```bash
python3 tools/scenes.py tour                                  # record
python3 tools/render.py casts/tour.json shots marks "g023 code" "tour-"
python3 tools/frames.py casts/tour.json frames/tour "g023 code" 12
ffmpeg -framerate 12 -i frames/tour/f%05d.png \
       -c:v libx264 -pix_fmt yuv420p -crf 20 videos/01-command-tour.mp4
```

`scenes.py` points at `/tmp/g023-shoot/todo-api` and
`/tmp/g023-shoot/home/g023.sh` — a sanitised demo project and install copied
away from any real home directory before shooting. Change `PROJ` and `APP` to
re-shoot against a different project or install.

Recordings that include agent turns make real API calls, so re-shooting costs
whatever those turns cost. The `/cost` panel visible in `09-usage-and-cost.png`
is the session's own figure — read it there rather than trusting a number
quoted here.
