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
| `finals_dir` | `PINAXIUM_FINALS_DIR` | `<Pictures>/Pinaxium` | Where finals are kept, one folder per project. On Windows "Pictures" is the shell's known Pictures folder, so OneDrive redirection is followed. |
| `draft_retention_days` | `PINAXIUM_DRAFT_RETENTION_DAYS` | `0` (never) | Send unfinalized drafts older than this to the recycle bin; checked hourly while the service (or any client) runs |
| `service_port` | `PINAXIUM_SERVICE_PORT` | `8190` | Port of the always-on service on 127.0.0.1: MCP at `/mcp`, image links at `/files/...` |
| `service_allowed_origins` | `PINAXIUM_SERVICE_ALLOWED_ORIGINS` | none | Web origins allowed to call `/mcp`, answered with CORS. Only needed for clients that send an `Origin` header; as an environment variable, separate them with spaces. |

For testing only, `PINAXIUM_TRASH_DIR` makes deletions move files into that folder instead of the OS recycle bin.

**MCP clients pass a server only a minimal environment.** Variables set in your shell don't reach Pinaxium when a client starts it. Set them in the client's server entry (`env`), or use the config file.

Environment variables override the file key by key. One exception keeps things consistent: if `PINAXIUM_COMFY_DIR` is set but `PINAXIUM_BASE_DIR` / `PINAXIUM_COMFY_PYTHON` are not, those two are derived from the environment's install, not taken from the file (which may describe a different install).

## The always-on service

`python -m pinaxium.service` runs Pinaxium as one long-lived process on `127.0.0.1:<service_port>` (default 8190):

| Path | What |
|---|---|
| `/mcp` | MCP over streamable HTTP, for any local MCP client |
| `/files/<project>/drafts/<file>`, `/files/<project>/finals/<id>` | The images, so the `local_url` links in results open in a browser |
| `/health` | Liveness |

**What it holds:**
- **GPU memory:** none. ComfyUI still starts on the first render and stops when idle.
- **ComfyUI dependence:** none for serving MCP metadata and existing images. The image archive stays available with GPU inference completely shut down.

**What it accepts:** only requests addressed to `127.0.0.1`/`localhost` on its port. A web page that rebinds a domain to 127.0.0.1 is refused, and so are MCP requests from pages on other origins.

| Command | Effect |
|---|---|
| `python -m pinaxium.service install` | Windows: start at logon (Task Scheduler, restarted if it fails) and start now |
| `python -m pinaxium.service status` | Task state, and whether the service answers |
| `python -m pinaxium.service restart` / `uninstall` | As named |
| `python -m pinaxium.service run` | Foreground; what the task runs. On Linux/macOS, run this from a systemd user unit or a launchd agent. |

Its log is `service.log` in `state_dir`. After updating Pinaxium, run `restart` so the service loads the new code.

## Registering with MCP clients

Clients reach Pinaxium in one of two ways, and both share the same engine, projects and images:

| Client can… | Use |
|---|---|
| connect to an HTTP MCP server (URL) | `http://127.0.0.1:8190/mcp`: the always-on service |
| only launch a server itself (stdio) | `<pinaxium>\.venv\Scripts\python.exe -m pinaxium.server` with `PYTHONPATH=<pinaxium>` (Linux/macOS: `<pinaxium>/.venv/bin/python`) |

**Claude Code** (HTTP):
```
claude mcp add --transport http --scope user pinaxium http://127.0.0.1:8190/mcp
```

**ChatGPT** (desktop app, custom MCP connection, fully local):

| Field | Value |
|---|---|
| Name | `Pinaxium` |
| Type | `Streamable HTTP` |
| URL | `http://127.0.0.1:8190/mcp` |
| Authentication | none |

The service must be running (`python -m pinaxium.service status`). What was verified against the live service, speaking raw HTTP:
- **Protocol versions:** `initialize` works with every handshake version from 2024-11-05 to 2025-11-25, and returns a session (`Mcp-Session-Id`).
- **Tools:** `tools/list` returns all tools, and structured results, image content and the clickable links all come through.
- **No engine needed:** none of this starts ComfyUI.
- **Sessions:** idle sessions are kept for 24 hours. After a service restart, clients start a new session as MCP requires.

**If ChatGPT can't connect** (check `service.log` in `state_dir` for `Invalid Origin header`), its MCP client is sending a web `Origin` header. The service refuses unknown origins so that web pages can't drive it. Add the origin from the log to the config file, then run `python -m pinaxium.service restart`:

```toml
service_allowed_origins = ["<origin from the log>"]
```

Listed origins are also answered with CORS headers; everything else stays refused.

**Connected but no tools?**
- **Codex/ChatGPT desktop loads MCP tools when a thread starts,** so start a new thread after adding the server.
- **Its log shows the server's state:** `~/.codex/logs_2.sqlite`, table `logs`. Search it for `pinaxium`. "Service initialized" means the connection works.

**STDIO fallback** (if the HTTP connection doesn't work):

| Field | Value |
|---|---|
| Command | `<pinaxium>\.venv\Scripts\python.exe` |
| Arguments | `-m pinaxium.server` |
| Working directory | `<pinaxium>` |
| Environment | `PYTHONPATH=<pinaxium>` (needed only if the client can't set a working directory) |

This was verified with an empty environment: Pinaxium finds its config, data and finals folders through Windows itself. A STDIO process belongs to its client, but it shares the same engine, projects and images as the service.

**Not supported:** ChatGPT's cloud connectors reach only servers on the internet. Pinaxium deliberately has no tunnel or public endpoint.

**Claude Desktop** (stdio): its config file can only launch servers. Add the server to `claude_desktop_config.json` (on Windows, `%APPDATA%\Claude\`), then restart Desktop:
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

**Other clients:** use the URL if they support HTTP, otherwise the stdio command. For stdio, settings can also be passed as `PINAXIUM_*` variables in the server's `env`.

Any number of clients can be connected at once. HTTP clients share the service; each stdio client starts its own small server process. All of them share one engine.

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
| `service.log` | The always-on service's output |
| `jobs/<job id>.json` | What a render was (project, prompt, model, ...) until its images are recorded; it outlives the call when a render exceeds `max_wait_s` |

These can all be deleted while the engine is stopped. Pinaxium recreates them as needed.

**In ComfyUI's data folder (`base_dir`)**, these are working files:
- `output/pinaxium/<project>/`: drafts, plus `project.json` (what made each draft);
- `input/pinaxium/<project>/`: reference images uploaded for edits.

`delete_images` cleans these up.

**In `finals_dir`:** `<project>/<id>.png` (each final, stamped with its own URI) and `<project>/finals.json` (the finals index: IDs, titles, descriptions, tags, provenance, last known location). Keep these: the index is what gives finals their permanent identities. Pinaxium never deletes anything here.
