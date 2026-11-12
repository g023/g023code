# g023 Code — Release Media

Screenshots of the real TUI, captured from a live session against a small demo
project (a four-file in-memory todo service). Nothing is mocked up: every still
is actual terminal output replayed through a terminal emulator and rendered
cell by cell.

**Captured on 1.5.0.** The stills show the current build: `deepseek-flash` as the
only model, thinking at 75, vision on, the 1,000,000-token window with
auto-compaction at 200k, and the client-side `WebSearch` / `FetchUrl` price note
in `/cost`.

**What the captures do not contain.** The session ran with `HOME` pointed at a
throwaway shoot folder, so every path prints as `~/todo-api` or `~/app` (the
1.5.0 UI prints paths under the home directory as `~`). The user name was
neutral, the clock was set to UTC so `/surge` does not reveal a time zone, and
the API key file held a dummy value. The PNGs carry no text, EXIF or timestamp
chunks, and the release's privacy gate scans them before an archive is built.

Terminal: 100 × 32 cells, DejaVu Sans Mono, dark theme.

## Screenshots (`screenshots/`)

| File | Shows |
|------|-------|
| `01-startup-banner.png` | Launch banner: project and home paths, model, thinking level, vision (a surge notice is added here during peak hours; this shot is off-peak) |
| `02-help-catalogue.png` | `/help`: the full command catalogue, grouped |
| `03-tools-and-permissions.png` | `/tools`: every tool with its permission level and a note on what it costs: local tools make no API call, `Agent` and `AnalyzeImage` say when they are not offered |
| `04-status.png` | `/status`: session, vision, project, context gauge, spend |
| `05-model.png` | `/model`: limits and prices of the one model (there is nothing to switch to) |
| `06-thinking-picker.png` | `/thinking`: reasoning-effort picker (low / high / off) |
| `09-usage-and-cost.png` | `/cost`: call, token and cost breakdown, cache hit/miss split and the price card |
| `13-vision-status.png` | `/vision status`: on/off, the vision model and the 800 px cap |
| `14-settings.png` | `/settings`: every setting with its live value, which ones are saved, and the command that changes it |
| `15-cache.png` | `/cache`: the project's SQLite cache and the machine-wide web cache |
| `16-signals.png` | `/signals`: prefix-cache hit rate by day, unknown item types, empty responses |
| `17-surge-pricing.png` | `/surge`: peak and off-peak windows on the local clock (UTC here), countdown, pause window |
| `19-config-scopes.png` | `/config`: global vs project config files and which scope is active |

The numbering keeps gaps where stills of live agent turns (an answer, the
context gauge mid-task, a permission prompt, a file write) used to sit. Those
need paid API calls and were not re-shot for 1.5.0, so they are not included
rather than shipped out of date.

## How they were made

The capture tooling is a development tool and is **not** in the release
archive. In outline:

- a recorder forks a PTY, runs `g023.sh`, types scripted keystrokes with human
  timing, answers cursor-position queries so `prompt_toolkit` draws its status
  bar, and records `(timestamp, output)` events plus named marks;
- scripted scenes (`tour`, `settings`, `panels`) only use slash commands, so
  they make no model calls and cost nothing;
- a renderer replays the recording through the `pyte` terminal emulator and
  paints each cell with Pillow (box-drawing glyphs drawn geometrically so
  borders join), writing one cropped still per mark. It refuses to write a still
  if any character would come out as a missing-glyph box.

The `/cost` figures in `09-usage-and-cost.png` are the session's own: zero,
because the scenes made no model calls.
