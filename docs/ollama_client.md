# `ollama_client.py` — local/remote Ollama integration

`g023_code/ollama_client.py` is the Ollama backend used by vision
(`subagents/vision.py`) and by the `/ollama` command family. It handles
host resolution, capability probing, model listing, image loading, and
the two vision chat calls.

## Host resolution

- `normalize_host(raw)` — accept `host:port`, `http://…`, `https://…`,
  bare IPs; produce a clean URL.
- `env_host()` — `OLLAMA_HOST` if set.
- `resolve_host(override)` — explicit `settings.vision_host` > `OLLAMA_HOST`
  > `http://localhost:11434`.
- `host_source()` / `is_local(host)` / `get_host()` — where the host came
  from, whether it's local, and the resolved default.

## Probing & models

- `probe(host, timeout, count_models)` → `HostProbe` (reachable?
  version? model count? why not?). `is_available(host, timeout)` is the
  quick form; `unreachable_hint(host)` explains *how* to fix it (start the
  daemon, install Ollama, correct the host).
- `list_models(host, timeout, probe_capabilities)` → `OllamaModel`
  objects: name, size (`size_gb`), whether it plausibly supports vision
  (`supports_vision` by tag heuristics) and how sure we are
  (`vision_certainty`).
- `find_model(models, wanted)` — fuzzy match of a requested name.
- `running_models(host, timeout)` — models currently loaded in VRAM
  (backs `/ollama ps`).

## Images

- `load_image(path_or_url, max_dim, timeout)` → `LoadedImage` (base64 +
  original/payload bytes; `bytes_sent` reports how much goes over the
  wire). Loads local files or downloads URLs.
- `_dimensions(data)` / `_maybe_downscale(data, max_dim)` — Pillow reads
  the image and downscales. Vision **send** always uses
  `VISION_SEND_MAX_SIDE = 800` on each edge (`load_image_for_vision`).
  `settings.vision_max_image_dim` is a legacy display knob and does not
  change the bytes sent (`tests/test_vision_resize.py`).
- `load_image_b64(path_or_url, max_dim, timeout)` — convenience wrapper.

## Vision chat

- `vision_chat_detailed(model, prompt, image_b64)` → `VisionResult`
  (answer text, timing, `tokens_per_second`). Sends the image as a
  base64-embedded messages request with `num_ctx` and `keep_alive` from
  settings.
- `vision_chat(model, prompt, image_b64)` — the same call shaped for the
  orchestrator's AnalyzeImage path.

## Related

- `docs/subagents.md` — `subagents/vision.py` orchestrates which backend
  runs and caches results.
- `docs/cli.md` — `/ollama` status/set-host/models/test/ps handlers.
- `docs/config.md` — `vision_host`, `vision_max_image_dim`,
  `vision_num_ctx`, `vision_keep_alive`, `vision_timeout`.
