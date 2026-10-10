# Pinaxium

An MCP server that gives Claude (Desktop, Code, or any MCP client) local image generation and editing on your own GPU, through [ComfyUI](https://github.com/comfyanonymous/ComfyUI).

- **Holds no GPU memory while idle.** The first request starts ComfyUI; after 10 idle minutes it is shut down again. One engine is shared by all connected clients.
- **Finds your models by itself.** Files in ComfyUI's model folders are identified by ComfyUI's own detection code and matched with their text encoder and VAE. Each family uses its official ComfyUI workflow.
- **Covers the whole loop:** text-to-image, instruction edits and multi-image composition, full-size results as MCP resources, a crop viewer for checking detail, and engine status you can subscribe to.
- **Organises the work.** Every render is a draft in a project, recorded with what made it. Keepers become finals with permanent URIs (`pinaxium://projects/hexy/finals/portrait-001`) that survive renames and moves, and can be searched, reused and branched from across projects. Leftover drafts go to the recycle bin in one call.
- **Sets itself up.** On first run it detects your ComfyUI install (Desktop, portable or a clone) and writes a commented config file.

## Documentation

| Document | For |
|---|---|
| [docs/GUIDE.md](docs/GUIDE.md) | Using it: workflow, choosing models, prompting, editing, results, the engine, troubleshooting, FAQ |
| [docs/REFERENCE.md](docs/REFERENCE.md) | Every tool, parameter, result field and resource, generated from the server |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Settings, auto-detection, registering with MCP clients, the files Pinaxium keeps |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Architecture, adding a model family, tests, tooling, releasing |
| [CHANGELOG.md](CHANGELOG.md) | What changed |

## Quick start

**Requirements:**
- ComfyUI 0.37 or later: the Desktop app, the Windows portable build, or a clone with its own venv;
- a GPU ComfyUI can use;
- Python 3.12+ and [uv](https://docs.astral.sh/uv/).

1. **Install Pinaxium:**
   ```
   git clone https://github.com/PStryder/pinaxium.git
   cd pinaxium
   uv sync
   uv run python -m pinaxium.config --init
   ```
   The last command finds ComfyUI, writes the config file, and prints `ready` or what to fix. It is optional, because the first use does the same.
2. **Get models.** Download a supported family's files into ComfyUI's model folders (table below).
3. **Register the server.** Use the command `<pinaxium>/.venv/Scripts/python.exe -m pinaxium.server` (`.venv/bin/python` on Linux/macOS) with `PYTHONPATH=<pinaxium>`. For Claude Code:
   ```
   claude mcp add pinaxium --scope user -e PYTHONPATH=<pinaxium> -- <pinaxium>/.venv/Scripts/python.exe -m pinaxium.server
   ```
   For Claude Desktop and other clients, see [CONFIGURATION.md](docs/CONFIGURATION.md#registering-with-mcp-clients).
4. **Ask your assistant for an image.**

## Supported models

Each family's workflow is copied from Comfy-Org's official template.
- **Verified with real renders:** Qwen-Image 2.1 base and turbo, Z-Image Turbo, FLUX.2 klein 4B distilled.
- **Supported but not rendered yet:** the undistilled variants, Z-Image base and klein 4B base. They use their own template's settings.
- **Everything else:** other model files are listed with the reason they aren't offered, and can still be used through `run_workflow`.

| Family | Files (Comfy-Org repackages on Hugging Face) | Edit | Notes | License |
|---|---|---|---|---|
| Qwen-Image 2.1 | `Comfy-Org/Qwen-Image-2.1`: a diffusion model (base and/or turbo), `qwen3vl_8b` text encoder, `qwen_image_2.1_vae` | up to 10 refs | Best quality and lettering, transparency; turbo is several times faster | Qwen Research (non-commercial) |
| Z-Image | `Comfy-Org/z_image_turbo`: diffusion model, `qwen_3_4b` text encoder, `ae` VAE | no | Fast, clean lettering | Apache-2.0 |
| FLUX.2 [klein] 4B | `Comfy-Org/vae-text-encorder-for-flux-klein-4b`: `flux-2-klein-4b`, `qwen_3_4b` text encoder, `flux2-vae` | up to 4 refs | Fastest; unreliable lettering | Apache-2.0 |

The diffusion models go in `models/diffusion_models`, the text encoders in `models/text_encoders`, and the VAEs in `models/vae`. Z-Image and klein share the same `qwen_3_4b` text encoder.

## Example benchmark

RTX 4080 16 GB, Windows 11, 2026-10-09; details in `results/bench-20261009-184104.md`.

Engine boot (ComfyUI start to API ready): 13–14 s. Same prompt for all models, 1024² unless noted.

| model | steps | cold (load + render) | warm 1024² | warm 2048² | peak VRAM | unload |
|---|---|---|---|---|---|---|
| Qwen-Image 2.1 (base) | 25 | 12.5 s | 8.3 s | 61.3 s | 13.7 GB | 0.3 s |
| Qwen-Image 2.1 turbo | 8 | 5.4 s | 3.4 s | 15.0 s | 14.6 GB | 0.3 s |
| Z-Image Turbo | 8 | 7.3 s | 3.1 s | 19.8 s | 14.2 GB | 0.2 s |
| FLUX.2 [klein] 4B | 4 | 5.2 s | 2.1 s | 10.4 s | 13.8 GB | 0.3 s |

Notes:
- **Cold** means not in VRAM and not in ComfyUI's cache. The files were probably in the OS file cache, so a first load after a reboot reads 14–17 GB from disk and takes longer.
- **Peak VRAM** is whole-GPU memory above the idle engine. ComfyUI fills what is free and offloads the rest.

## Platform support

Developed and tested on **Windows 11** with an NVIDIA GPU. Linux and macOS are supported by design but untested. ComfyUI Desktop detection is tested against a simulated install only.

## Security

The built-in tools only touch the local ComfyUI and local files. `run_workflow` runs arbitrary ComfyUI graphs and is **open-world**: partner API nodes call paid external services, and custom nodes can run any code. See [GUIDE.md](docs/GUIDE.md#run_workflow-and-the-trust-boundary) before exposing Pinaxium to untrusted input.

## License

Pinaxium is MIT-licensed (see `LICENSE`). The models it runs have their own licenses (table above).
