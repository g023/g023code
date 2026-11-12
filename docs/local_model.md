# Local model (llama.cpp) — `local_llm.py`, `local_ask.py`, `scout.py`

Automatic (`local_model: auto`, the default). A small GGUF model served by
`llama-server` on the user's GPU takes jobs that do not need the paid model's
reasoning: bulk reading of files (LocalAsk, with or without paths), the
turn-zero scout card (docs/scout.md), reranking excerpt lines, and first-pass
screenshot questions. The main loop always stays on DeepSeek.

## Auto-tune (`autotune.py`)

`local_model` is `auto` | `on` | `off` (`G023_LOCAL=1/0` → on/off). `auto` asks
`autotune.profile()` (cached per process, re-probed when a `local_*` path or
file setting changes; `/local tune` re-runs it):

1. **Binary**: `llama-server` under `local_llama_dir` (build/bin, bin,
   build/bin/Release, root), then `~/llama.cpp`, `~/src|code|git|projects/llama.cpp`,
   `/opt/llama.cpp`, `/usr/local/llama.cpp`, then `PATH`.
2. **Models folder**: `local_models_dir`, then `~/models`, `~/Models`,
   `~/.cache/llama.cpp`, `~/llama.cpp/models`, LM Studio's folders (first one
   holding a `.gguf`).
3. **Model**: a set `local_vision_file`, else a set `local_model_file`
   (`picked_by: setting`), else the best candidate: the measured table
   `KNOWN` first (`measured`), then unmeasured models closest to a 2 GB file
   (`size`). Skipped: projectors, embeddings, rerankers, whisper/TTS, drafts,
   LoRAs, split shards, < 256 MiB, > 6 GiB, or more than 90 % of VRAM.
4. **Vision**: on when the chosen model has a projector next to it (`mmproj-<model>` or
   `<family>-mmproj-<quant>`, Q8 before
   F16), unless `local_vision_file` is `off`. One server serves both, so vision
   never costs a model swap. A projector that does not fit is dropped first.
5. **GPU**: `nvidia-smi` name + total VRAM, or Apple Silicon (2/3 of RAM).
   No GPU → auto stays off: the scout's time box (8 s + 4 s expand) would cost
   a CPU-only machine seconds per turn. `/local on` still forces it.

`Settings.local_enabled` is the one switch every caller reads (registry,
system prompt, scout, LocalAsk, vision, GPU yield). `/local off` saves
`"off"`; a boolean in config.json reads as on/off.

### Model evidence (scout card eval, local only, no paid calls)

| model | first set (20, older package snapshot) | held-out set (18, current package) | s/card |
|---|---|---|---|
| LFM3-VL-2B Q8 (+mmproj) | 16 (17 incl. also-matching) | 12 (13) | 1.1 |
| LFM3-1.7B Q8 | 14 (17) | 12 (14) | 1.1 |
| LFM3-1.7B QAD Q4_0 | 14 (17) | – | 0.9 |
| Qwen3.5-4B Q4_K_M | 17 (19) | 12 (13) | 3.0–3.3 |
| Spark-X2.6-4B Q4_K_M | 16 (19) | 12 (13) | 2.8–3.1 |
| Nanbeige4.2-3B Q6 | 14 | – | 5–10 |

4B models do not beat the 2B on the held-out set and take 3× longer per turn;
the VL-2B equals the 1.7B on text and adds vision, so it ranks first.

## Setup (only to override auto)

```
/local tune                           # what auto found
/settings set local_llama_dir ~/llamacpp/llama.cpp   # needs build/bin/llama-server
/settings set local_models_dir ~/MODELS
/local model LFM3-1.7B-Q8_0.gguf      # pin (name, path, or unique prefix); 'auto' unpins
/local vision LFM3-VL-2B-Q8_0.gguf    # pin a VL model; 'off' = DeepSeek vision; 'auto'
/local auto | on | off                # or env G023_LOCAL=1 / 0
/local status | models | unload
/local scout local|always|off         # turn-zero scout card (docs/scout.md)
```

Settings: `local_model` (auto), `local_llama_dir`, `local_models_dir`,
`local_model_file` (""), `local_vision_file` (""), `local_gpu_layers` (99),
`local_ctx` (16384), `local_port` (0 = pick a free one), `local_gpu_guard`.

## Server (`local_llm.py`)

- One server per machine, shared by all g023 sessions: state file and log in
  `<global cache>/local_llm/`. `ensure(wait)` reuses it, spawns it, or
  restarts it when the wanted model changed; `wait=False` starts it in the
  background so a turn never blocks on model load.
- `--sleep-idle-seconds 300`: after 5 idle minutes the weights leave VRAM
  (measured 2160 → 120 MiB) and wake on the next request (~0.8 s).
- `chat_json` (thinking off via `chat_template_kwargs`, JSON-schema output),
  `parse_json_reply` (strips think blocks and fences, first JSON object).
  Some vocabularies cannot build llama.cpp's grammar sampler (Nanbeige4.2-3B:
  HTTP 400 "Failed to initialize samplers"); `chat_json` then retries once
  with the schema in the system prompt and no grammar, and remembers that for
  the (port, model) pair. Before this fallback every local feature was
  silently off with such a model.
- `pick_lines` (rerank, 4 s time box, fail-open), `vision`, `status`.
- `release()` stops the server (SIGTERM after checking `/proc/<pid>/cmdline`
  really is llama-server).

## GPU etiquette

- `gpu_busy()` (cached 5 s, `local_gpu_guard`): another compute process
  holding ≥ 1 GiB blocks the local model; desktop graphics clients (browsers,
  compositors) are ignored; free VRAM must fit model + mmproj ×1.2 + 768 MiB
  when our server is not up yet.
- `note_command(cmd)`: every Bash command is checked by `is_gpu_work` (CUDA,
  nvidia-smi, torch/training, llama.cpp/ollama/vllm, ...). A match makes the
  local model yield for the rest of the turn and releases the server.
- `request_wants_gpu(text)`: a request that talks about CUDA, VRAM, training,
  llama.cpp etc. gets no scout card, so the model is not loaded right before
  the user's own GPU job.
- The prompt addendum (orchestrator `LOCAL_MODEL_ADDENDUM`) tells DeepSeek
  **not** to use LocalAsk when the task involves GPU work.

## LocalAsk (`local_ask.py`)

Tool `LocalAsk` (arguments `question`, optional `paths`), offered only when `local_model` is on.
Without `paths` it searches the whole project with the scout's index
(`scout.locate`) and drafts the answer from the verbatim lines it found
(result field `searched`).

1. Files are cut into 250-line chunks (≤ 12; BM25-ranked against the
   question when there are more).
2. For each chunk the model returns line ids; ids outside the shown chunk
   are dropped (`verify_ids`) and the evidence text is **copied from the
   file**, never from the model (cite-or-drop).
3. Each evidence line gets an anchor (`section_of`): the markdown heading
   path ("# Changelog > ## 1.5.0 > ### Added") or the enclosing def/class.
   This turned a 1.7B "not found" on a changelog question into the correct
   answer.
4. A final call drafts a short answer from the evidence only. The JSON
   result has `backend`, `found`, `answer` (a small-model lead), `evidence`
   (verbatim), `read`, `note`, and `skipped` / `unread` when relevant.

When the local model is off or the GPU is busy, LocalAsk returns an error
with a hint (ReadFile ranges / SearchContent).

## Scout card

With the local model on, each request gets a `[scout: ...]` block: verbatim
lines from the units that best match it, picked by the local model (~1 s). See
docs/scout.md. `/local scout local|always|off`.

## Vision

`subagents/vision._try_local`: with the local model on and a served model
that can see (`local_llm.vision_model()`: an mmproj is loaded), AnalyzeImage asks the local VL model first (8/9 on
the screenshot bench, ~0.2–1.9 s). Asking the same image+question again goes
to DeepSeek, so the model can escalate without any extra prompt text.

## Cost reality

DeepSeek V4.1 Flash is cheap (a task ~$0.0005–0.001, a vision call
~$0.00006). The local model's value is free bulk reading and vision and
keeping long text out of the paid context; the excerpt rerank adds +0–1 of
14 over the deterministic picks.
