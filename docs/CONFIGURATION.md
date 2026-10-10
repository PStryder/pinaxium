# Configuration

## Where settings come from

Highest priority first:

1. **Environment variables** (`PINAXIUM_<KEY>`), e.g. set in the MCP client's server entry.
2. **The config file**, a TOML file:
   - Windows: `%APPDATA%\pinaxium\config.toml`
   - macOS: `~/Library/Application Support/pinaxium/config.toml`
   - Linux: `$XDG_CONFIG_HOME/pinaxium/config.toml` (usually `~/.config/...`)
   - `PINAXIUM_CONFIG` points at a different file.
3. **Defaults and auto-detection.**

`python -m pinaxium.config` prints the effective value of every setting, where it came from, and whether the engine can start (with what to fix if not). `python -m pinaxium.config --init` detects ComfyUI again and rewrites the file.

## First run and auto-detection

When no config file exists, the first use detects ComfyUI and writes one, with a comment explaining each key. The exception is when `PINAXIUM_COMFY_DIR` is set: then no file is written. Detection looks for:

| Install type | How it's recognised | Python used |
|---|---|---|
| ComfyUI Desktop | Desktop's `config.json` (`basePath`) plus the app's bundled ComfyUI code | `<basePath>/.venv` |
| Windows portable build | `ComfyUI_windows_portable/ComfyUI` | `python_embeded/python.exe` |
| Git clone / manual install | A `ComfyUI` folder | its `.venv`, `venv` or `env` |

- **Where it searches:** your home, Documents, Desktop and Downloads folders, every drive root on Windows (`/opt`, `/srv` and `/workspace` elsewhere), and one folder below each of those.
- **What counts:** a folder only counts if it has ComfyUI's `main.py`, `comfy/` package and `folder_paths.py`.
- **Ranking:** installs with a usable Python rank first. The file names the install chosen and lists any others found.
- **If nothing is found:** the file is still written, explaining what to set. Pinaxium runs, and `engine_status` reports the problem.

## Settings

| Key | Environment variable | Default | Meaning |
|---|---|---|---|
| `comfy_dir` | `PINAXIUM_COMFY_DIR` | detected | ComfyUI's code folder, the one containing `main.py` |
| `comfy_python` | `PINAXIUM_COMFY_PYTHON` | found next to `comfy_dir` | Python interpreter that runs ComfyUI; also used to detect models |
| `base_dir` | `PINAXIUM_BASE_DIR` | `comfy_dir` | ComfyUI's data folder (`models`, `input`, `output`, `temp`). It differs from `comfy_dir` only for ComfyUI Desktop, and is then passed to ComfyUI as `--base-directory`. |
| `comfy_args` | `PINAXIUM_COMFY_ARGS` | `[]` | Extra ComfyUI arguments, e.g. `["--lowvram"]`. As an environment variable, separate them with spaces. |
| `host` | `PINAXIUM_HOST` | `127.0.0.1` | Address ComfyUI listens on. Keep it local unless you mean to expose ComfyUI on your network. |
| `port` | `PINAXIUM_PORT` | `8188` | ComfyUI's port. A ComfyUI already running on it is used, never stopped. |
| `idle_minutes` | `PINAXIUM_IDLE_MINUTES` | `10` | Stop the engine (freeing all GPU memory) after this long with no engine requests and no running job. Status checks and `list_models` don't count, so polling doesn't keep it alive. |
| `start_timeout_s` | `PINAXIUM_START_TIMEOUT_S` | `180` | Give up waiting for ComfyUI to start after this long |
| `state_dir` | `PINAXIUM_STATE_DIR` | platform data folder | Logs, lock and caches (see below). Windows: `%LOCALAPPDATA%\pinaxium\state`; macOS: `~/Library/Application Support/pinaxium/state`; Linux: `$XDG_STATE_HOME/pinaxium` |
| `default_model` | `PINAXIUM_DEFAULT_MODEL` | first usable model | Model id used when a request names none |

Environment variables override the file key by key. One exception keeps things consistent: if `PINAXIUM_COMFY_DIR` is set but `PINAXIUM_BASE_DIR` / `PINAXIUM_COMFY_PYTHON` are not, those two are derived from the environment's install, not taken from the file (which may describe a different install).

## Registering with MCP clients

Pinaxium is a stdio MCP server. Run it with its own environment's Python and `PYTHONPATH` set to the cloned folder (`<pinaxium>` below):

| Platform | Command |
|---|---|
| Windows | `<pinaxium>\.venv\Scripts\python.exe -m pinaxium.server` |
| Linux / macOS | `<pinaxium>/.venv/bin/python -m pinaxium.server` |

**Claude Code:**
```
claude mcp add pinaxium --scope user -e PYTHONPATH=<pinaxium> -- <pinaxium>\.venv\Scripts\python.exe -m pinaxium.server
```

**Claude Desktop:** add the server to `claude_desktop_config.json` (on Windows, `%APPDATA%\Claude\`), then restart Desktop:
```json
{
  "mcpServers": {
    "pinaxium": {
      "command": "<pinaxium>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "pinaxium.server"],
      "env": {"PYTHONPATH": "<pinaxium>"}
    }
  }
}
```

**Other clients:** use the same command and environment. Settings can also be passed as `PINAXIUM_*` variables in the server's `env`.

Several clients can be registered at once. Each starts its own small server process, and all of them share one engine.

## Files Pinaxium keeps

In `state_dir`:

| File | Purpose |
|---|---|
| `engine.lock` | Held by the engine supervisor; its modification time is a heartbeat (stale after 30 s) |
| `state.json` | Engine state: status, process ids, start time, last stop reason |
| `activity` | Touched by every request that uses the engine (renders, edits, `get_job`, `start_engine`); the idle timer counts from it |
| `stop` | Present briefly when a stop has been requested |
| `comfyui.log` | ComfyUI's output for the current or last engine run (also the `pinaxium://logs/comfyui` resource) |
| `supervisor.log` | The supervisor's own messages (also `pinaxium://logs/supervisor`) |
| `scan_cache.json` | Model detection results, keyed by file path, size and modification time |
| `spawn_env.json` | `PINAXIUM_*` settings handed to the supervisor at start-up |

In ComfyUI's data folder (`base_dir`):
- `output/pinaxium/`: every image Pinaxium saves;
- `input/pinaxium/`: reference images uploaded for `edit_image`.

All of these can be deleted while the engine is stopped. Pinaxium recreates them as needed.
