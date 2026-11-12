# `images.py` + `subagents/vision.py` — AnalyzeImage

Vision is one isolated call on the same model as everything else
(`deepseek-flash`, DeepSeek V4.1 Flash). There is no second provider, no
daemon and no model choice: `/vision on|off|status` is the only knob
(`settings.vision_enabled`, plus `vision_timeout`, default 180 s).

## Loading — `g023_code/images.py`

`load_image_for_vision(path_or_url, timeout=30)` → `LoadedImage`:

1. **Read.** `http(s)://` URLs are downloaded (redirects followed). Anything
   else is a path resolved with `paths.resolve_in_project` — the same
   confinement as ReadFile, so `../` and absolute paths outside the project
   raise `PathOutsideProject`.
2. **Sniff.** `sniff_media_type(data)` reads the magic bytes: `image/jpeg`,
   `image/png`, `image/gif`, `image/webp` (the formats the Anthropic
   `image` block accepts). Anything else is refused with
   "not a supported image".
3. **Cap.** `constrain_image_bytes(data)` fits the image into
   `VISION_SEND_MAX_SIDE = 800` px (aspect kept, never upscaled). An image
   that already fits is sent **byte-for-byte unchanged with its real media
   type**; a resized one is re-encoded as PNG with Pillow. Without Pillow an
   oversized or unmeasurable image is refused rather than sent uncapped.
   `image_dimensions` reads PNG/JPEG headers without Pillow;
   `constrained_size(w, h)` is the pure geometry.

`LoadedImage` carries `b64`, `raw`, `media_type`, `original_size`,
`final_size`, `downscaled`, `source`, `bytes_sent` and `data_uri`
(`data:<media_type>;base64,...`).

## The call — `subagents/vision.py`

`run_vision(path_or_url, question)` returns compact JSON and never raises:

- vision off → `{"error": "Vision is disabled.", "hint": "Run /vision on…"}`
  and no network call;
- load failure → `{"error": "Could not load image: …"}`;
- cache hit (SHA-256 of the bytes sent + question + `deepseek:<MODEL>`) →
  the stored answer with `"cached": true`;
- otherwise `DeepSeekClient.vision(question, data_uri, system=VISION_SYSTEM,
  timeout=vision_timeout)` — one `/anthropic/v1/messages` request, thinking
  disabled, `max_output_tokens` 2048. Usage is recorded under the
  **subagent** scope, so `/cost` includes it. The payload adds `media_type`,
  `image_pixels`, `image_bytes_sent` and, when resized, `downscaled_from`.

The orchestrator only offers the `AnalyzeImage` tool while vision is on;
the instructions/tool prefix is keyed on `vision_enabled`, so toggling it
changes the prefix once.

## Related

- `docs/api.md` — `DeepSeekClient.vision`.
- `docs/subagents.md` — the router that dispatches AnalyzeImage.
- `docs/cache.md` — `get_vision` / `put_vision`.
- `tests/test_images.py`.
