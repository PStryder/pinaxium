# Changelog

## 0.5.0 — 2026-10-09

- **The always-on service** (`python -m pinaxium.service`) is one process on `127.0.0.1:8190`:
  - MCP over streamable HTTP at `/mcp`, for any local MCP client;
  - the images at `/files/<project>/drafts/<file>` and `/files/<project>/finals/<id>`;
  - `install` sets it to start at logon via Task Scheduler (Windows), with `status`, `restart` and `uninstall` alongside.

  It holds no GPU memory and never needs ComfyUI to serve metadata or existing images. It accepts only requests addressed to this machine, which blocks DNS rebinding and foreign web origins.
- **Clickable links:**
  - render replies carry `open:` (a `file:///` link to the file on disk) and `browser:` (the service's `http://127.0.0.1` link);
  - listings, `finalize_image` and `get_final` carry the same as `file_uri` and `local_url`;
  - the instructions tell agents to give users these links.
- **`listChanged` is advertised over every transport** (the SDK's defaults turned it off for HTTP sessions too).
- **Claude Code** can now connect over HTTP. stdio remains for clients that only launch servers.

## 0.4.0 — 2026-10-09

### Projects, drafts and finals
- **Projects.** Every render is a draft in a project (`project` parameter, default `scratch`), recorded with its prompt, model, seed, settings and sources.
- **Finals with permanent identity.** `finalize_image` turns a draft into a final with an ID and URI that never change, e.g. `pinaxium://projects/hexy/finals/portrait-001`, plus a title, description, tags and provenance. Finals live in `finals_dir`, which defaults to your Pictures folder's `Pinaxium`.
- **IDs survive renames and moves.** Each final carries its URI inside the PNG (a text chunk; pixels and ComfyUI metadata untouched). Renames and moves inside `finals_dir` are followed automatically, a stripped stamp falls back to a pixel hash, and `relink_final` covers moves elsewhere. References resolve through Pinaxium, not file paths.
- **Search and reuse:**
  - `list_finals` searches across projects with paginated metadata and an optional single contact sheet, whose numbered tiles map one-to-one to `sheet_number`;
  - `get_final` shows one final at a chosen size;
  - `derive_from_final` branches into a new draft in any project, recording `derived_from`;
  - draft and final URIs work as `edit_image` references and in `view_image`.
- **Cleanup.**
  - `delete_images` sends drafts (one by one, or a whole project's with its uploads) to the OS recycle bin. It never deletes permanently and never deletes finals.
  - `draft_retention_days` recycles old unfinalized drafts automatically.
- **New tools:** `list_projects` and `list_drafts`. `list_drafts` replaces `list_images`.
- **New URI scheme:**
  - `pinaxium://projects/<project>`
  - `pinaxium://projects/<project>/drafts/<file>`
  - `pinaxium://projects/<project>/finals/<id>`

  It replaces `pinaxium://images/<file>`. Existing images move into `scratch` on first start.

### Fixes
- **Tool error messages now reach the model.** The MCP SDK showed only "Error executing tool …" for any exception other than its own `ToolError`, which hid every explanation of what to fix. Unknown or invalid resource URIs are now reported with their reason too.
- **`get_job` is no longer marked read-only:** it records finished renders into their project.

## 0.3.0 — 2026-10-09

### Configuration
- **Config file, created on first run.** Pinaxium detects ComfyUI (Desktop, the portable build, or clones in common folders and drive roots), together with the Python it runs under, and writes a commented TOML file. Where it lives: `%APPDATA%\pinaxium\config.toml` on Windows, the XDG config folder on Linux, Application Support on macOS, or `PINAXIUM_CONFIG`.
- **Settings precedence:** environment variables, then the file, then defaults.
- **`python -m pinaxium.config`** shows the effective settings and where each came from; `--init` re-detects and rewrites the file.
- **Separate data folder:** `base_dir` can differ from the code folder, as ComfyUI Desktop requires, and is passed to ComfyUI as `--base-directory`. `comfy_args` adds extra ComfyUI flags.
- **Fix:** overriding `comfy_dir` from the environment no longer borrows the file's `base_dir`/`comfy_python` from another install.
- **Setup problems are explained:** they appear in `engine_status` (`setup_problems`, `config_file`) and in tool errors, and no engine is started. Detector failures caused by a broken setup are no longer cached.
- **No machine-specific defaults remain.** Runtime state moved to the platform data folder (Windows: `%LOCALAPPDATA%\pinaxium\state`).

### Documentation
- **Instructions:** the server instructions are now a usage playbook: workflow, model choice, prompting, timing, failures, engine control, and when to use `run_workflow`.
- **Tool and result descriptions:** tool descriptions are clean paragraphs, and describe the reply's contents and the edge cases. Every result field is described.
- **Model notes:** `list_models` gives `notes` comparing the models (strengths, limits, relative speed), and `text_rendering` now uses strong/good/weak.
- **New documents:**
  - `docs/GUIDE.md`: usage, troubleshooting, FAQ;
  - `docs/CONFIGURATION.md`;
  - `docs/DEVELOPMENT.md`;
  - `docs/REFERENCE.md`: generated from the live server by `tools_docs.py`.
- **Doc tests:** the reference must match the server, and the hand-written docs may only name real tools, resources and settings.
- **Server title:** now "Pinaxium: local image generation (ComfyUI)".

### Other
- **Hermetic tests:** the test run never touches the real install, engine or config.
- **Linux and macOS:** the supervisor is started in its own session.

## 0.2.0 — 2026-10-09

First public version, as Pinaxium (previously developed as imagegen-mcp).

- **Shared engine.** A detached supervisor starts ComfyUI on demand and stops it after the idle period. On Windows it is started through WMI so that closing an MCP client doesn't kill it, and a crashed supervisor's lock is taken over.
- **Model discovery.** Files are identified by ComfyUI's own detection code from safetensors headers. Verified families: Qwen-Image 2.1, Z-Image and FLUX.2 [klein] 4B.
- **Tools:**
  - `generate_image`, `edit_image` (multi-reference) and `run_workflow`;
  - `view_image`, with crop;
  - engine control and job handling.
- **Complete catalogue:** titles, all four hints, parameter ranges, and output schemas.
- **Resources:** full-size images, subscribable engine status, and logs. Results carry resource links, and list-changed and updated notifications are sent.
- **`run_workflow` is open-world.**
- **Tests:** lifecycle tests against a fake ComfyUI, plus a mutation check and a benchmark.
