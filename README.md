# pinaxium

An MCP server for local image generation and editing on this machine's RTX 4080, through ComfyUI (`F:\ComfyUI`).

The engine sits idle with nothing loaded. On the first request it starts ComfyUI, and models load into VRAM on the first render. After 10 minutes with no requests and no running job, it shuts ComfyUI down, which frees all GPU memory.

## How it fits together

```
Claude Desktop ─┐  (each client spawns its own stdio server)
Claude Code  ───┼─> pinaxium.server ──HTTP──> ComfyUI :8188
                │          │ wakes                    ▲
                └──────────┴─> pinaxium.supervisor ┘  (one, detached, shared)
```

- **`supervisor.py`** owns the single ComfyUI process. It runs detached, so it outlives the client that woke it, and any client can use it.
  - It shuts the engine down once nothing has happened for the idle period. "Something happening" means a client request or a job in ComfyUI's queue.
  - Single ownership is enforced by `state/engine.lock`, whose mtime is a heartbeat. A lock left by a crashed supervisor goes stale after 30 s and is taken over.
- **`engine.py`** is the client side. It wakes the supervisor and talks to ComfyUI's HTTP API.
- **External ComfyUI:** if a ComfyUI you started yourself (`F:\ComfyUI\run_comfyui.bat`) is already on port 8188, the service uses it and never shuts it down.
- **`registry.py` + `comfy_detect.py`** discover models automatically (see below).
- **`workflows.py`** has one graph builder per model family, each copied from Comfy-Org's official template for that family.

## Model discovery

Every file in ComfyUI's `models/` folders (`diffusion_models`, `unet`, `checkpoints`, `text_encoders`, `clip`, `vae`, `loras`) is classified by **ComfyUI's own detection code**.
- It runs in ComfyUI's venv and reads only the safetensors header, using stand-in tensors that hold no data. Renamed files are still recognised.
- Results are cached by path, size and mtime, so dropping in a new file costs one detection pass (about 15 s) on the next `list_models` or render.

A diffusion file becomes a usable model when its architecture belongs to a supported family and that family's text encoder and VAE are present.
- **Companion choice:** when several files fit, int8/fp8 text encoders are preferred, and name hints pick between same-shaped VAEs.
- **Default steps and CFG** come from the file name: turbo, distilled or base.

Supported families. Each builder was copied from the official template and verified with a real render:

| Family | Architecture | Text encoder | VAE | Edit |
|---|---|---|---|---|
| Qwen-Image 2.1 | `QwenImage21` | Qwen3-VL-8B | Qwen-Image 2.1 VAE | yes (up to 10 refs) |
| Z-Image | `ZImage` | Qwen3-4B | 16-ch (`ae`) | no |
| FLUX.2 [klein] 4B | `Flux2`, ≤ 6B | Qwen3-4B | 32-ch FLUX.2 VAE | yes (up to 4 refs) |

Anything else is still listed by `list_models` with the reason it isn't offered, such as an unsupported architecture, a missing text encoder, or a LoRA. Those files can still be used through `run_workflow`.

## Tools

| Tool | What it does |
|---|---|
| `list_models` | Usable models (id, family, capabilities, defaults, files), unusable ones with what's missing, and other files. |
| `generate_image` | Text to image with `model` (default: the first family's base model), size 256–2048, steps, seed, cfg. |
| `edit_image` | Instruction-based edit, or a composition from reference images (edit-capable models only). |
| `run_workflow` | Any ComfyUI API-format graph, for LoRAs, ControlNet or unsupported models. |
| `view_image` | Look at any local image; `crop=[x,y,w,h]` inspects a detail at full resolution. |
| `engine_status` | Reports stopped / starting / ready (ready to render), whether it's busy, the queue, VRAM, whether models are loaded, and idle time. Never starts anything. |
| `start_engine`, `stop_engine`, `free_vram` | Lifecycle by hand. |
| `get_job`, `cancel_render`, `list_images` | Long renders and past outputs. |

Images are saved full-size to `F:\ComfyUI\output\pinaxium\`. Results come with a preview of up to 1024 px.

## Resources

| URI | Content |
|---|---|
| `pinaxium://images/{filename}` | A saved image at **full size** (PNG). `resources/list` lists the newest 500 with size and last-modified. |
| `pinaxium://engine/status` | The `engine_status` JSON. Subscribe to it to hear when the engine starts or stops, gets busy, or loads models. |
| `pinaxium://logs/comfyui` | The last 256 KB of ComfyUI's log, useful for diagnosing failed renders. |
| `pinaxium://logs/supervisor` | The last 256 KB of the supervisor's log: engine start, idle shutdown, spawn problems. |

**How clients find out about changes:**
- `generate_image`, `edit_image` and `get_job` return a `resource_link` to each full-size image, next to the preview.
- New or removed images send `notifications/resources/list_changed`.
- Changes to a subscribed URI send `notifications/resources/updated`. A background watcher polls every 2 s, and only changes made after it first looks are reported.
- Both the older `resources/subscribe` protocol (2025-11-25 and earlier) and the newer `subscriptions/listen` (2026-07-28) are supported.
- Image URIs resolve only to PNGs directly inside `output\pinaxium`. Anything else, including path traversal, is refused.

## Trust boundary: `run_workflow` is open-world

`generate_image`, `edit_image` and the other tools run only fixed, built-in ComfyUI graphs. Those graphs touch nothing but ComfyUI on 127.0.0.1 and files on this machine, so their `openWorldHint` is false.

`run_workflow` executes whatever graph it is given, and **local hosting does not make that closed-world**:
- **Partner API nodes:** ComfyUI ships over 40 provider modules of these (`comfy_api_nodes`: OpenAI, Google Gemini and Veo, Anthropic, Kling, Runway and others). They send prompts and images to external paid services, billed to whatever Comfy account is signed in.
- **Custom nodes** run arbitrary Python with network and file access.
- **Any workflow** can write or overwrite files through save nodes.

`run_workflow` is therefore marked **open-world and destructive**, and its description says so. Before a public release, or before connecting this server to an agent that takes untrusted input, consider removing `run_workflow` or restricting it to an allowlist of node types.

**Catalogue (MCP 2025-11-25):**
- Every tool has a title, a description, and all four annotation hints set explicitly. `openWorldHint` is false everywhere except `run_workflow` (see above).
- Every parameter has a description, with its range declared in the input schema; out-of-range values are rejected before the call.
- `engine_status`, `start_engine`, `list_models` and `list_images` declare an `outputSchema` and return `structuredContent` plus a JSON text copy.
- `tests/test_catalogue.py` checks all of this over a real stdio session.
- The empty `prompts`/`resources` capabilities are advertised by the SDK's `MCPServer` itself. They're left as they are, which is valid under the spec.

## Configuration (environment variables)

| Variable | Default |
|---|---|
| `PINAXIUM_COMFY_DIR` | `F:\ComfyUI` |
| `PINAXIUM_PORT` | `8188` |
| `PINAXIUM_IDLE_MINUTES` | `10` |
| `PINAXIUM_START_TIMEOUT_S` | `180` |
| `PINAXIUM_DEFAULT_MODEL` | first available model |
| `PINAXIUM_STATE_DIR` | `<project>\state` (logs: `supervisor.log`, `comfyui.log`; `scan_cache.json`) |

## Registration

- **Claude Code** (user scope): `claude mcp add pinaxium --scope user -e PYTHONPATH=F:\HexyLab\pinaxium -- F:\HexyLab\pinaxium\.venv\Scripts\python.exe -m pinaxium.server`
- **Claude Desktop:** the `pinaxium` entry under `mcpServers` in `%APPDATA%\Claude\claude_desktop_config.json`, with the same command and `PYTHONPATH`. Restart Desktop to pick it up.

## Tests and tools

```
uv run pytest                          # graphs, registry, real detector on real files, lifecycle vs a fake ComfyUI
uv run python tools_mutate.py          # every guarantee removed in turn must fail the suite
uv run python smoke_client.py render   # real MCP stdio session with real renders
uv run python bench.py                 # boot, load, render, unload timings for every usable model
```

## Benchmark (RTX 4080 16 GB, 2026-10-09; `results/bench-20261009-184104.md`)

Engine boot (ComfyUI start to API ready): 13–14 s. Same prompt for all models, 1024² unless noted.

| model | steps | cold (load + render) | warm 1024² | warm 2048² | peak VRAM | unload |
|---|---|---|---|---|---|---|
| Qwen-Image 2.1 (base) | 25 | 12.5 s | 8.3 s | 61.3 s | 13.7 GB | 0.3 s |
| Qwen-Image 2.1 turbo | 8 | 5.4 s | 3.4 s | 15.0 s | 14.6 GB | 0.3 s |
| Z-Image Turbo | 8 | 7.3 s | 3.1 s | 19.8 s | 14.2 GB | 0.2 s |
| FLUX.2 [klein] 4B | 4 | 5.2 s | 2.1 s | 10.4 s | 13.8 GB | 0.3 s |

Notes:
- **Cold** means not in VRAM and not in ComfyUI's cache. The files were probably in the Windows file cache, so a first load after a reboot reads 14–17 GB from disk and takes longer.
- **Peak VRAM** is whole-GPU memory above the idle engine. ComfyUI fills what is free and offloads the rest.

## Licenses

Pinaxium itself is MIT-licensed (see `LICENSE`). The models it runs have their own licenses:

Qwen-Image 2.1 is released under the **Qwen Research License**, which is not a commercial license. Z-Image and FLUX.2 [klein] 4B are Apache-2.0.
