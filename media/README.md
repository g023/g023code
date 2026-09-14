# g023 Code — Release Media

Screenshots captured from a real TUI session against a small demo project
(a four-file in-memory todo service) launched from `/tmp/g023-shoot`.
Nothing here is mocked up: every frame is actual terminal output.

> **The screenshots were re-shot on 1.3.4** from `/tmp/g023-shoot/...` —
> see the banner in `01-startup-banner.png`. They show the current build:
> the sent model id is `deepseek-flash`, `/model` has nothing to switch to,
> thinking is the 0–100 dial (75 default), the window is 256k, and
> `web_search` has no permission entry because DeepSeek runs it
> server-side. Live agent turns use **only** `deepseek-flash`. Ollama and
> OpenRouter models are not exercised; `/vision status` and `/or status`
> are the default-off / no-key panels.
>
> The captures use **sanitised paths** (`/tmp/g023-shoot/todo-api`,
> `/tmp/g023-shoot/home`) so nothing in them exposes a machine's real home
> directory, user name, or API keys — the shot set is safe to publish as-is.

Terminal: 100 × 32 cells, DejaVu Sans Mono, dark theme.

## Screenshots (`screenshots/`)

| File | Shows |
|------|-------|
| `01-startup-banner.png` | Launch banner — project/home paths, model, thinking level, Ollama probe |
| `02-help-catalogue.png` | `/help` — the full command catalogue, grouped |
| `03-tools-and-permissions.png` | `/tools` — every tool with its permission level and subagent isolation |
| `04-status.png` | `/status` — session, vision, Ollama, project, cache summary |
| `05-model.png` | `/model` — flash is the only model the Responses API serves, so there is nothing to switch to |
| `06-thinking-picker.png` | `/thinking` — reasoning-effort picker (low / high / off; max aliases to 75) |
| `09-usage-and-cost.png` | `/cost` — per-model call/token/cost breakdown, cache hit/miss split and price card |
| `12-ollama-daemon.png` | `/ollama` — daemon host, reachability, version, latency, model count |
| `13-vision-picker.png` | `/vision status` — vision disabled (the shipped default) |
| `14-settings.png` | `/settings` — effective configuration and where each value came from |
| `15-cache.png` | `/cache` — SQLite cache stats for the project |
| `16-signals.png` | `/signals` — prefix hit-rate by day, unknown item types, empty responses |
| `17-surge-pricing.png` | `/surge` — peak/off-peak windows on the local clock, countdown, pause window |
| `18-openrouter.png` | `/or status` — OpenRouter vision with no `OK.dat` (the shipped default) |
| `19-config-scopes.png` | `/config` — global vs project config files and which scope is active |

Live agent-turn stills (`07` answer, `08` context, `10` permission,
`11` write) and the video set were **not re-shot this cut**: a
`deepseek-flash` `/responses` probe timed out (DeepSeek infra overload).
Panel stills above do not need the API and are current. The capture
tooling under `tools/` is how to re-shoot them later; it is not part of
the release archive.

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
