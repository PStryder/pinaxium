# Developing Pinaxium

## Architecture

```
MCP client ──stdio──> pinaxium.server ──HTTP──> ComfyUI (host:port)
 (one server process     │      ▲                    ▲
  per client)            │      └─ resources watcher │ starts / stops
                         └─ wakes ─> pinaxium.supervisor (one, detached, shared)
```

| Module | Role |
|---|---|
| `server.py` | The MCP server: tools, resources, instructions. Builds graphs and sends them to the engine. Runs its own stdio loop so it can advertise `resources.listChanged`. Its `tool()`/`resource()` decorators turn expected errors into `ToolError`/`ResourceNotFoundError`; the SDK hides the message of anything else. |
| `projects.py` | Projects, drafts and finals: folders and URIs, the per-project manifest (`project.json`) and finals index (`finals.json`), final identity (URI stamped into the PNG, pixel hash, search of the finals tree, relinking), recycle-bin deletion, retention. |
| `engine.py` | Client side of the engine: wakes the supervisor, submits graphs, polls jobs, reads status. Checks the setup before starting anything. |
| `supervisor.py` | Owns the ComfyUI process. Holds `engine.lock` (an mtime heartbeat), writes `state.json`, and stops ComfyUI when idle or on request. Stdlib only. |
| `config.py` | Settings (env > file > defaults), ComfyUI detection, the config file, `python -m pinaxium.config`. |
| `registry.py` | Scans ComfyUI's model folders, caches detection results, matches files to families and companions, picks defaults. |
| `comfy_detect.py` | Runs **inside ComfyUI's Python**: classifies files with ComfyUI's own detection code, from safetensors headers only. |
| `workflows.py` | One graph builder per model family, each a copy of Comfy-Org's official template. |
| `resources.py` | Image, status and log resources; the watcher that turns changes into notifications. |
| `schemas.py` | Typed results, which give tools their `outputSchema`. |

### Engine lifecycle

1. A tool that needs the engine calls `Engine.ensure_ready()`, which checks the setup and then wakes the supervisor if no live one holds the lock.
2. On Windows the supervisor is created through WMI (`Win32_Process.Create`, under `pythonw`), so it doesn't belong to the MCP client's job object. Clients run servers in kill-on-close jobs that don't allow breakaway. A WMI-created process inherits no environment, so `PINAXIUM_*` settings travel in `spawn_env.json`. On other platforms it is started with `start_new_session`.
3. The supervisor starts ComfyUI, waits for its API, records the GPU memory in use as the baseline for "models loaded", then polls every 2 s. It stops ComfyUI on a `stop` file, on idle (no `activity` touch and an empty queue for `idle_minutes`), or if ComfyUI exits.
4. A supervisor that dies leaves a lock whose heartbeat goes stale after 30 s. Clients then start a new supervisor, which takes over the stale lock.

## Adding a model family

1. **Get the official workflow.** In [Comfy-Org/workflow_templates](https://github.com/Comfy-Org/workflow_templates), find the family's template under `templates/`, or an API-format copy under `benchmarks/`. Its nodes, sampler, scheduler, steps, cfg and shift are the reference.
2. **Find how ComfyUI identifies the model.** Find the class name in `comfy/supported_models.py`, and how the text encoder is detected (`detect_te_model` in `comfy/sd.py`). Run `comfy_detect.py` on the real files to see the `arch` values and `params_b` it reports. If the VAE needs a new rule, add it to `comfy_detect.vae_arch`.
3. **Write the builder** in `workflows.py`: a class with `name`, `supports_edit`, `text_to_image(files, prompt, negative_prompt, width, height, p, transparent=False)` and, if supported, `edit(...)`. Copy the template's graph exactly; add it to `BUILDERS`.
4. **Add a `Family`** in `registry.py` with:
   - `arch` (and `max_params_b` if several sizes share the class);
   - the text encoder and VAE `arch`s;
   - `capabilities`, `license` and `notes`;
   - a `variants` function giving default steps and cfg per file name.
5. **Verify with real renders** before merging: text-to-image and, if supported, a single-image and a multi-image edit, through `smoke_client.py render <model id>`. Look at the images.
6. **Add tests:**
   - graph tests in `tests/test_workflows.py`;
   - matching tests in `tests/test_registry.py`;
   - the real files in `tests/test_detector_real.py`;
   - mutations for each guarantee in `tools_mutate.py`.
7. **Regenerate the reference** (`uv run python tools_docs.py`), update the families table in the README and the model guidance in `docs/GUIDE.md`, then run `bench.py`.

## Tests

`uv run pytest` is hermetic. `tests/conftest.py` points every setting at throwaway values: a config file, an empty ComfyUI folder, a state folder and an unused port, so no test touches a real install unless it opts in.

| Test file | Covers |
|---|---|
| `test_config.py` | Detection on fake layouts, the config file round trip, precedence, problems, the launch command |
| `test_workflows.py` | Every builder's graph, against the official templates |
| `test_registry.py` | Scanning, caching, family and companion matching, defaults |
| `test_detector_real.py` | ComfyUI's detection on the real model files. It finds an install by auto-detection, writes nothing, and skips without models. |
| `test_lifecycle.py` | Supervisor and engine against `fake_comfy.py`: idle shutdown, keep-alive, stop, single owner, stale lock, external ComfyUI, surviving a closed client job |
| `test_catalogue.py` | The tool catalogue over real stdio: titles, all hints, parameter descriptions and ranges, output schemas, described result fields |
| `test_resources.py` | Resources over real stdio: listing, full-size reads, URI safety, list-changed and updated notifications |
| `test_projects.py` | Project names and URIs; drafts and provenance; finalize (stamp, IDs, idempotence); finals surviving renames, moves and stripped metadata; relinking; recycle-bin-only deletion; retention |
| `test_project_tools.py` | The project tools over real stdio: paging, finalize, search, the numbered contact sheet, `get_final`, `relink_final`, `delete_images` |
| `test_docs.py` | `docs/REFERENCE.md` matches the live server; hand-written docs name only real tools, resources, settings and environment variables |

**Mutation check:** `uv run python tools_mutate.py [substring ...]` removes each guarantee in turn (the list is in the file), runs the suite, and requires a failure. Every claim the code makes should have a mutation. It edits the source while running and restores it afterwards; don't edit files during a run.

## Tools

| Command | Purpose |
|---|---|
| `uv run python tools_docs.py` | Regenerate `docs/REFERENCE.md` from the live server (`--check` to verify) |
| `uv run python smoke_client.py [render [model ...]]` | A real MCP session: list, status, and with `render` real generations, edits and a crop view |
| `uv run python bench.py [--warm N] [--models id,...]` | Engine boot, cold load and render, warm renders, 2048² render and unload timings per model; writes `results/` |
| `uv run python -m pinaxium.config [--init]` | Effective settings, or re-detect and rewrite the config file |

## Known traps

These were found the hard way; the code handles each, and the tests cover it:
- **MCP clients on Windows kill their servers' job object on close, and breakaway is refused.** Hence the WMI spawn.
- **ComfyUI uses `cudaMallocAsync`,** so torch's memory counters read about 0. Loaded-model memory is whole-GPU memory minus the baseline at engine start.
- **The Qwen-Image 2.1 VAE decodes RGBA, with noise in the alpha channel (251–255).** The builder strips it unless `transparent=True`.
- **The MCP Python SDK 2.x creates a new `ServerSession` per request.** Clients are told apart by the underlying connection.
- **`MCPServer.run()` advertises `resources.listChanged = false` to clients before protocol 2026-07-28.** The server runs its own stdio loop with the right notification options.
- **The detection cache is keyed by the file's size and modification time at scan time.** A file that is still downloading is detected again on the next scan instead of losing its result.
- **A uv venv breaks when its folder is moved,** because the launchers hold absolute paths. Recreate it.
- **The MCP SDK hides the message of any exception that isn't a `ToolError` or `ResourceError`.** Clients see only "Error executing tool …".
- **MCP stdio clients pass the server only a minimal environment** unless they're given `env` explicitly. Test and smoke clients pass `os.environ`.
- **Final identity can't rely on a hash of the file's bytes.** Stamping or stripping metadata changes the bytes. Pinaxium stamps the URI and keeps a hash of the decoded pixels.

## Releasing

1. Bump `version` in `pyproject.toml` and `pinaxium/__init__.py`.
2. Add a `CHANGELOG.md` entry.
3. Run `uv run python tools_docs.py`, `uv run pytest` and `uv run python tools_mutate.py`, then do a real render with `smoke_client.py render`.
4. Commit and tag (`vX.Y.Z`).

## Hardening for untrusted use

`run_workflow` is open-world (see [GUIDE.md](GUIDE.md#run_workflow-and-the-trust-boundary)). To remove it, delete its function from `server.py` and its rows from the tests. To restrict it instead, check `class_type` values against an allowlist before `_run`. `host` should stay `127.0.0.1`.
