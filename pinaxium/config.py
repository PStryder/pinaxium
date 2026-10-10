"""Settings: environment variables > config file > auto-detected defaults.

The config file is TOML, at PINAXIUM_CONFIG or the platform's config folder:
  Windows %APPDATA%\\pinaxium\\config.toml, macOS ~/Library/Application Support/pinaxium/config.toml,
  Linux $XDG_CONFIG_HOME/pinaxium/config.toml (~/.config/...).
It is created on first use by detecting the ComfyUI install, unless PINAXIUM_COMFY_DIR is set.

    python -m pinaxium.config            show the effective settings and where each comes from
    python -m pinaxium.config --init     detect again and (re)write the config file
"""

from __future__ import annotations

import json
import os
import string
import sys
import tomllib
from dataclasses import dataclass, field

# key -> (environment variable, type, default; None = detected or derived)
FIELDS: dict[str, tuple[str, type, object]] = {
    "comfy_dir": ("PINAXIUM_COMFY_DIR", str, None),
    "comfy_python": ("PINAXIUM_COMFY_PYTHON", str, None),
    "base_dir": ("PINAXIUM_BASE_DIR", str, None),
    "comfy_args": ("PINAXIUM_COMFY_ARGS", list, []),
    "host": ("PINAXIUM_HOST", str, "127.0.0.1"),
    "port": ("PINAXIUM_PORT", int, 8188),
    "idle_minutes": ("PINAXIUM_IDLE_MINUTES", float, 10.0),
    "start_timeout_s": ("PINAXIUM_START_TIMEOUT_S", float, 180.0),
    "state_dir": ("PINAXIUM_STATE_DIR", str, None),
    "default_model": ("PINAXIUM_DEFAULT_MODEL", str, None),
    "finals_dir": ("PINAXIUM_FINALS_DIR", str, None),
    "draft_retention_days": ("PINAXIUM_DRAFT_RETENTION_DAYS", float, 0.0),
    "service_port": ("PINAXIUM_SERVICE_PORT", int, 8190),
}
WINDOWS = os.name == "nt"


# ---------- platform folders ----------

def _home() -> str:
    return os.path.expanduser("~")


def config_path() -> str:
    if os.environ.get("PINAXIUM_CONFIG"):
        return os.environ["PINAXIUM_CONFIG"]
    if WINDOWS:
        base = os.environ.get("APPDATA") or os.path.join(_home(), "AppData", "Roaming")
    elif sys.platform == "darwin":
        base = os.path.join(_home(), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(_home(), ".config")
    return os.path.join(base, "pinaxium", "config.toml")


def default_state_dir() -> str:
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(_home(), "AppData", "Local")
        return os.path.join(base, "pinaxium", "state")
    if sys.platform == "darwin":
        return os.path.join(_home(), "Library", "Application Support", "pinaxium", "state")
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(_home(), ".local", "state"), "pinaxium")


def pictures_dir() -> str:
    """The user's Pictures folder: on Windows the shell's known folder (follows OneDrive redirection), else
    ~/Pictures, else the home folder."""
    if WINDOWS:
        try:
            import ctypes
            import uuid
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("data", ctypes.c_ubyte * 16)]

            folder_id = GUID()
            ctypes.memmove(folder_id.data, uuid.UUID("33E28130-4E1E-4676-835A-98395C3BC3BB").bytes_le, 16)
            out = ctypes.c_wchar_p()
            shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
            if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(out)) == 0:
                path = out.value
                ole32.CoTaskMemFree(ctypes.cast(out, wintypes.LPVOID))
                if path:
                    return path
        except (OSError, AttributeError, ValueError):
            pass
    p = os.path.join(_home(), "Pictures")
    return p if os.path.isdir(p) else _home()


def default_finals_dir() -> str:
    return os.path.join(pictures_dir(), "Pinaxium")


# ---------- ComfyUI detection ----------

def is_comfy_code_dir(d: str) -> bool:
    return (os.path.isfile(os.path.join(d, "main.py")) and os.path.isdir(os.path.join(d, "comfy"))
            and os.path.isfile(os.path.join(d, "folder_paths.py")))


def _venv_python(venv: str) -> str:
    return os.path.join(venv, "Scripts", "python.exe") if WINDOWS else os.path.join(venv, "bin", "python")


def find_comfy_python(code_dir: str, base_dir: str | None = None) -> str | None:
    """The interpreter ComfyUI runs under: its venv, the portable build's embedded Python, or the Desktop
    app's venv in its base folder."""
    parent = os.path.dirname(os.path.abspath(code_dir))
    candidates = [_venv_python(os.path.join(code_dir, v)) for v in (".venv", "venv", "env")]
    candidates.append(os.path.join(parent, "python_embeded", "python.exe" if WINDOWS else "python"))  # portable
    if base_dir:
        candidates.append(_venv_python(os.path.join(base_dir, ".venv")))                          # Desktop
    candidates.append(_venv_python(os.path.join(parent, ".venv")))
    return next((c for c in candidates if os.path.isfile(c)), None)


@dataclass
class Install:
    comfy_dir: str
    base_dir: str
    comfy_python: str | None
    kind: str


def _desktop_installs() -> list[Install]:
    """ComfyUI Desktop keeps its code inside the app and its data (models, output, .venv) in basePath from
    its config.json. Best effort: the paths follow the Desktop app's published layout."""
    if WINDOWS:
        settings = [os.path.join(os.environ.get("APPDATA", ""), "ComfyUI", "config.json")]
        local = os.environ.get("LOCALAPPDATA", "")
        code = [os.path.join(local, "Programs", n, "resources", "ComfyUI")
                for n in ("@comfyorgcomfyui-electron", "ComfyUI", "comfyui-electron")]
    elif sys.platform == "darwin":
        settings = [os.path.join(_home(), "Library", "Application Support", "ComfyUI", "config.json")]
        code = ["/Applications/ComfyUI.app/Contents/Resources/ComfyUI"]
    else:
        settings = [os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(_home(), ".config"),
                                 "ComfyUI", "config.json")]
        code = []
    out = []
    for s in settings:
        try:
            with open(s, encoding="utf-8") as f:
                base = json.load(f).get("basePath")
        except (OSError, ValueError, AttributeError):
            continue
        for c in code:
            if base and os.path.isdir(base) and is_comfy_code_dir(c):
                out.append(Install(c, base, find_comfy_python(c, base), "ComfyUI Desktop"))
    return out


def _search_roots() -> list[str]:
    h = _home()
    roots = [h, os.path.join(h, "Documents"), os.path.join(h, "Desktop"), os.path.join(h, "Downloads")]
    if WINDOWS:
        roots += [f"{d}:\\" for d in string.ascii_uppercase if os.path.isdir(f"{d}:\\")]
    else:
        roots += ["/opt", "/srv", "/workspace"]
    return [r for r in roots if os.path.isdir(r)]


def _folder_installs(roots: list[str]) -> list[Install]:
    """ComfyUI clones and portable builds in common places: <root>/ComfyUI, <root>/ComfyUI_windows_portable/
    ComfyUI, and the same one folder deeper (e.g. D:\\AI\\ComfyUI)."""
    names = ("ComfyUI", "comfyui", os.path.join("ComfyUI_windows_portable", "ComfyUI"))
    skip = {"windows", "program files", "program files (x86)", "programdata", "$recycle.bin", "system volume information",
            "appdata", "proc", "sys", "dev"}
    seen, out = set(), []

    def consider(d: str, kind: str):
        real = os.path.realpath(d)
        if real in seen or not is_comfy_code_dir(d):
            return
        seen.add(real)
        out.append(Install(d, d, find_comfy_python(d), kind))

    for root in roots:
        for n in names:
            consider(os.path.join(root, n), "portable" if "portable" in n else "folder")
        try:
            children = [e for e in os.scandir(root) if e.is_dir() and e.name.lower() not in skip
                        and not e.name.startswith(".")]
        except OSError:
            continue
        for e in children:
            for n in names:
                consider(os.path.join(e.path, n), "portable" if "portable" in n else "folder")
    return out


def detect(roots: list[str] | None = None) -> list[Install]:
    """All ComfyUI installs found, best first: ones with a usable Python before ones without."""
    found = _desktop_installs() + _folder_installs(_search_roots() if roots is None else roots)
    return sorted(found, key=lambda i: i.comfy_python is None)


# ---------- the config file ----------

def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(v)          # JSON string and string-array escapes are valid TOML


def render_config(values: dict, notes: list[str]) -> str:
    lines = ["# Pinaxium settings. Environment variables (PINAXIUM_<KEY>) override these;",
             "# `python -m pinaxium.config` shows the effective values, `--init` detects again.", ""]
    lines += [f"# {n}" for n in notes] + ([""] if notes else [])
    help_ = {
        "comfy_dir": "ComfyUI's code folder (contains main.py)",
        "comfy_python": "Python interpreter that runs ComfyUI",
        "base_dir": "ComfyUI data folder: models, input, output (differs from comfy_dir for ComfyUI Desktop)",
        "comfy_args": "extra command-line arguments for ComfyUI, e.g. [\"--lowvram\"]",
        "host": "address ComfyUI listens on; keep 127.0.0.1 unless you mean to expose it",
        "port": "ComfyUI port; a ComfyUI already on this port is used but never shut down",
        "idle_minutes": "stop the engine (freeing all GPU memory) after this long without requests",
        "start_timeout_s": "give up waiting for ComfyUI to start after this long",
        "state_dir": "logs, engine lock, model detection cache",
        "default_model": "model id used when a request names none (see list_models)",
        "finals_dir": "where finalize_image copies finished images, one folder per project",
        "draft_retention_days": "send unfinalized drafts older than this to the recycle bin; 0 = never",
        "service_port": "port of the always-on Pinaxium service (MCP at /mcp, image links at /files), on 127.0.0.1",
    }
    for key in FIELDS:
        lines.append(f"# {help_[key]}")
        v = values.get(key)
        lines.append(f"{key} = {_toml_value(v)}" if v is not None else f"# {key} = ")
    return "\n".join(lines) + "\n"


def write_config(path: str, values: dict, notes: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(render_config(values, notes))
    with open(tmp, "rb") as f:
        tomllib.load(f)            # never leave an unparseable file behind
    os.replace(tmp, path)


def detected_values(installs: list[Install]) -> tuple[dict, list[str]]:
    values = {k: d for k, (_, _, d) in FIELDS.items() if d is not None}
    values["state_dir"] = default_state_dir()
    values["finals_dir"] = default_finals_dir()
    if not installs:
        return values, ["No ComfyUI install was found. Set comfy_dir (and comfy_python if ComfyUI uses its own",
                        "Python) below, or install ComfyUI and run `python -m pinaxium.config --init`."]
    best = installs[0]
    values.update(comfy_dir=best.comfy_dir, base_dir=best.base_dir, comfy_python=best.comfy_python)
    notes = [f"Detected {best.kind} install: {best.comfy_dir}"]
    if best.comfy_python is None:
        notes.append("Its Python interpreter was not found: set comfy_python.")
    notes += [f"Also found ({i.kind}): {i.comfy_dir}" for i in installs[1:]]
    return values, notes


def init_config(path: str | None = None, roots: list[str] | None = None) -> tuple[str, dict, list[str]]:
    path = path or config_path()
    values, notes = detected_values(detect(roots))
    write_config(path, values, notes)
    return path, values, notes


# ---------- effective settings ----------

@dataclass(frozen=True)
class Config:
    comfy_dir: str
    comfy_python: str
    base_dir: str
    comfy_args: tuple[str, ...]
    host: str
    port: int
    idle_minutes: float
    start_timeout_s: float
    state_dir: str
    default_model: str | None
    finals_dir: str
    draft_retention_days: float
    service_port: int
    config_file: str
    sources: dict = field(default_factory=dict, compare=False)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def service_url(self) -> str:
        """The always-on Pinaxium service (python -m pinaxium.service): MCP at /mcp, image links at /files."""
        return f"http://127.0.0.1:{self.service_port}"

    @property
    def models_dir(self) -> str:
        return os.path.join(self.base_dir, "models")

    @property
    def output_dir(self) -> str:
        return os.path.join(self.base_dir, "output")

    @property
    def input_dir(self) -> str:
        return os.path.join(self.base_dir, "input")

    @property
    def temp_dir(self) -> str:
        return os.path.join(self.base_dir, "temp")

    def state_path(self, name: str) -> str:
        return os.path.join(self.state_dir, name)

    def problems(self) -> list[str]:
        """What stops the engine from starting, phrased as how to fix it."""
        fix = f"edit {self.config_file} or run `python -m pinaxium.config --init`"
        if not self.comfy_dir:
            return [f"ComfyUI location is not configured ({fix})"]
        out = []
        if not is_comfy_code_dir(self.comfy_dir):
            out.append(f"comfy_dir {self.comfy_dir} is not a ComfyUI folder (no main.py / comfy/) ({fix})")
        if not self.comfy_python or not os.path.isfile(self.comfy_python):
            out.append(f"comfy_python {self.comfy_python or '(unset)'} does not exist ({fix})")
        return out


def _coerce(key: str, raw):
    typ = FIELDS[key][1]
    if typ is list:
        if isinstance(raw, str):
            return raw.split()
        return [str(x) for x in raw]
    return typ(raw)


def load(create: bool = True) -> Config:
    path = config_path()
    file_values: dict = {}
    if os.path.isfile(path):
        with open(path, "rb") as f:
            file_values = tomllib.load(f)
    elif create and not os.environ.get("PINAXIUM_COMFY_DIR"):
        try:
            _, file_values, _ = init_config(path)
        except OSError:
            file_values = detected_values(detect())[0]

    values, sources = {}, {}
    for key, (env, _, default) in FIELDS.items():
        if os.environ.get(env) not in (None, ""):
            values[key], sources[key] = _coerce(key, os.environ[env]), f"env {env}"
        elif file_values.get(key) is not None:
            values[key], sources[key] = _coerce(key, file_values[key]), "config file"
        else:
            values[key], sources[key] = default, "default"

    comfy_dir = values["comfy_dir"] or ""
    # base_dir and comfy_python belong to the install comfy_dir names: when comfy_dir is overridden from the
    # environment, the config file's values (another install's) don't carry over
    if sources["comfy_dir"].startswith("env"):
        for key in ("base_dir", "comfy_python"):
            if not sources[key].startswith("env"):
                values[key] = None
    if not values["base_dir"]:
        values["base_dir"], sources["base_dir"] = comfy_dir, "= comfy_dir"
    if not values["comfy_python"] and comfy_dir:
        values["comfy_python"] = find_comfy_python(comfy_dir, values["base_dir"]) or ""
        sources["comfy_python"] = "detected next to comfy_dir"
    if not values["state_dir"]:
        values["state_dir"], sources["state_dir"] = default_state_dir(), "default"
    if not values["finals_dir"]:
        values["finals_dir"], sources["finals_dir"] = default_finals_dir(), "default"
    return Config(comfy_dir=comfy_dir, comfy_python=values["comfy_python"] or "", base_dir=values["base_dir"],
                  comfy_args=tuple(values["comfy_args"] or ()), host=values["host"], port=values["port"],
                  idle_minutes=values["idle_minutes"], start_timeout_s=values["start_timeout_s"],
                  state_dir=values["state_dir"], default_model=values["default_model"],
                  finals_dir=values["finals_dir"], draft_retention_days=values["draft_retention_days"],
                  service_port=values["service_port"], config_file=path,
                  sources=sources)


def main(argv: list[str]) -> int:
    if "--init" in argv:
        path, values, notes = init_config()
        print(f"wrote {path}")
        for n in notes:
            print("  " + n)
        print()
    cfg = load(create=True)
    print(f"config file: {cfg.config_file}{'' if os.path.isfile(cfg.config_file) else ' (not created)'}")
    for key in FIELDS:
        print(f"  {key:16} = {getattr(cfg, key)!r:60}  [{cfg.sources.get(key)}]")
    problems = cfg.problems()
    print("\nready" if not problems else "\nproblems:\n  " + "\n  ".join(problems))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
