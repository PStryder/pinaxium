"""Config: ComfyUI detection on fake layouts, the config file round trip, precedence, problems, launch command."""

import json
import os
import tomllib

import pytest

from pinaxium import config as C
from pinaxium.supervisor import comfy_command

PY = os.path.join("Scripts", "python.exe") if os.name == "nt" else os.path.join("bin", "python")
EMBED = "python.exe" if os.name == "nt" else "python"


def make_comfy(d, venv: str | None = ".venv"):
    os.makedirs(os.path.join(d, "comfy"), exist_ok=True)
    for f in ("main.py", "folder_paths.py"):
        open(os.path.join(d, f), "w").close()
    if venv:
        py = os.path.join(d, venv, PY)
        os.makedirs(os.path.dirname(py), exist_ok=True)
        open(py, "w").close()
    return d


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for key, (env, _, _) in C.FIELDS.items():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("PINAXIUM_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    return tmp_path


# ---------- detection ----------

def test_detects_a_clone_with_its_venv(tmp_path):
    d = make_comfy(str(tmp_path / "ComfyUI"))
    (inst,) = C._folder_installs([str(tmp_path)])
    assert (inst.comfy_dir, inst.base_dir, inst.kind) == (d, d, "folder")
    assert inst.comfy_python == os.path.join(d, ".venv", PY)


def test_detects_the_portable_build_and_its_embedded_python(tmp_path):
    portable = tmp_path / "ComfyUI_windows_portable"
    d = make_comfy(str(portable / "ComfyUI"), venv=None)
    os.makedirs(portable / "python_embeded")
    open(portable / "python_embeded" / EMBED, "w").close()
    (inst,) = C._folder_installs([str(tmp_path)])
    assert inst.comfy_dir == d and inst.kind == "portable"
    assert inst.comfy_python == str(portable / "python_embeded" / EMBED)


def test_detects_one_folder_deeper_and_ignores_lookalikes(tmp_path):
    real = make_comfy(str(tmp_path / "AI" / "ComfyUI"))
    os.makedirs(tmp_path / "Other" / "ComfyUI")                 # a folder called ComfyUI that isn't one
    assert [i.comfy_dir for i in C._folder_installs([str(tmp_path)])] == [real]


def test_installs_with_a_python_rank_first(tmp_path, monkeypatch):
    make_comfy(str(tmp_path / "a" / "ComfyUI"), venv=None)
    with_py = make_comfy(str(tmp_path / "b" / "ComfyUI"))
    monkeypatch.setattr(C, "_desktop_installs", lambda: [])
    found = C.detect([str(tmp_path)])
    assert found[0].comfy_dir == with_py and found[1].comfy_python is None


@pytest.mark.skipif(os.name != "nt", reason="Windows Desktop layout")
def test_detects_comfyui_desktop_code_and_data_apart(tmp_path, monkeypatch):
    appdata, local, base = tmp_path / "Roaming", tmp_path / "Local", tmp_path / "Documents" / "ComfyUI"
    code = make_comfy(str(local / "Programs" / "@comfyorgcomfyui-electron" / "resources" / "ComfyUI"), venv=None)
    os.makedirs(appdata / "ComfyUI")
    (appdata / "ComfyUI" / "config.json").write_text(json.dumps({"basePath": str(base)}))
    os.makedirs(base / ".venv" / "Scripts")
    open(base / ".venv" / "Scripts" / "python.exe", "w").close()
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    (inst,) = C._desktop_installs()
    assert (inst.comfy_dir, inst.base_dir, inst.kind) == (code, str(base), "ComfyUI Desktop")
    assert inst.comfy_python == str(base / ".venv" / "Scripts" / "python.exe")


# ---------- config file ----------

def test_toml_round_trip_survives_quotes_backslashes_and_unicode(tmp_path):
    tricky = {"comfy_dir": 'C:\\path "with" quotes\\ünï', "comfy_args": ["--x", 'a "b" \\c'], "port": 1, "idle_minutes": 0.5}
    C.write_config(str(tmp_path / "c.toml"), tricky, ['note with "quotes"'])
    with open(tmp_path / "c.toml", "rb") as f:
        data = tomllib.load(f)
    assert {k: data[k] for k in tricky} == tricky


def test_init_writes_a_parseable_file_that_load_reads_back(clean_env, monkeypatch):
    weird = make_comfy(str(clean_env / "Pete's AI stuff" / "ComfyUI"))    # spaces, apostrophe, OS separators
    monkeypatch.setattr(C, "_desktop_installs", lambda: [])
    path, values, notes = C.init_config(roots=[os.path.dirname(weird)])
    with open(path, "rb") as f:
        data = tomllib.load(f)
    assert data["comfy_dir"] == weird and data["comfy_python"] == os.path.join(weird, ".venv", PY)
    assert any("Detected folder install" in n for n in notes)
    cfg = C.load()
    assert cfg.comfy_dir == weird and cfg.sources["comfy_dir"] == "config file" and not cfg.problems()


def test_first_load_creates_the_file_by_detection(clean_env, monkeypatch):
    d = make_comfy(str(clean_env / "ComfyUI"))
    monkeypatch.setattr(C, "_desktop_installs", lambda: [])
    monkeypatch.setattr(C, "_search_roots", lambda: [str(clean_env)])
    assert not os.path.exists(C.config_path())
    cfg = C.load()
    assert os.path.isfile(C.config_path()) and cfg.comfy_dir == d


def test_no_file_is_created_when_the_environment_names_comfyui(clean_env, monkeypatch):
    d = make_comfy(str(clean_env / "ComfyUI"))
    monkeypatch.setenv("PINAXIUM_COMFY_DIR", d)
    cfg = C.load()
    assert not os.path.exists(C.config_path())
    assert cfg.comfy_dir == d and cfg.comfy_python == os.path.join(d, ".venv", PY)    # python found next to it


def test_environment_beats_file_beats_default(clean_env):
    d = make_comfy(str(clean_env / "ComfyUI"))
    C.write_config(C.config_path(), {"comfy_dir": d, "port": 9000, "idle_minutes": 3.0}, [])
    os.environ["PINAXIUM_PORT"] = "9100"
    try:
        cfg = C.load()
    finally:
        del os.environ["PINAXIUM_PORT"]
    assert (cfg.port, cfg.sources["port"]) == (9100, "env PINAXIUM_PORT")
    assert (cfg.idle_minutes, cfg.sources["idle_minutes"]) == (3.0, "config file")
    assert (cfg.start_timeout_s, cfg.sources["start_timeout_s"]) == (180.0, "default")
    assert cfg.base_dir == d and cfg.models_dir == os.path.join(d, "models")


def test_nothing_found_still_writes_a_file_that_explains_what_to_set(clean_env, monkeypatch):
    monkeypatch.setattr(C, "_desktop_installs", lambda: [])
    path, values, notes = C.init_config(roots=[str(clean_env)])
    text = open(path, encoding="utf-8").read()
    assert "No ComfyUI install was found" in text and "# comfy_dir = " in text
    cfg = C.load()
    (problem,) = cfg.problems()
    assert "not configured" in problem and path in problem


def test_overriding_comfy_dir_does_not_borrow_the_files_other_install(clean_env, monkeypatch):
    first = make_comfy(str(clean_env / "one" / "ComfyUI"))
    second = make_comfy(str(clean_env / "two" / "ComfyUI"))
    C.write_config(C.config_path(), {"comfy_dir": first, "base_dir": str(clean_env / "one-data"),
                                     "comfy_python": os.path.join(first, ".venv", PY)}, [])
    monkeypatch.setenv("PINAXIUM_COMFY_DIR", second)
    cfg = C.load()
    assert cfg.base_dir == second and cfg.comfy_python == os.path.join(second, ".venv", PY)
    monkeypatch.setenv("PINAXIUM_BASE_DIR", str(clean_env / "two-data"))     # set alongside: kept
    assert C.load().base_dir == str(clean_env / "two-data")


def test_problems_name_the_fix(clean_env):
    d = make_comfy(str(clean_env / "ComfyUI"), venv=None)
    C.write_config(C.config_path(), {"comfy_dir": d, "comfy_python": str(clean_env / "missing.exe")}, [])
    (problem,) = C.load().problems()
    assert "comfy_python" in problem and "--init" in problem
    C.write_config(C.config_path(), {"comfy_dir": str(clean_env)}, [])
    assert any("not a ComfyUI folder" in p for p in C.load().problems())


def test_engine_refuses_to_start_on_a_broken_setup_without_spawning(clean_env, monkeypatch):
    import asyncio
    from pinaxium.engine import Engine, EngineError
    monkeypatch.setenv("PINAXIUM_STATE_DIR", str(clean_env / "state"))
    monkeypatch.setenv("PINAXIUM_PORT", "1")                   # nothing listens there
    C.write_config(C.config_path(), {"comfy_dir": str(clean_env)}, [])     # not a ComfyUI folder
    eng = Engine(C.load())
    with pytest.raises(EngineError, match="not a ComfyUI folder"):
        asyncio.run(eng.ensure_ready())
    assert not os.path.exists(clean_env / "state" / "spawn_env.json"), "a supervisor was spawned anyway"


@pytest.mark.skipif(os.name != "nt", reason="Windows known folders")
def test_folders_resolve_without_any_environment_variables(monkeypatch):
    """An MCP client may launch the server with a near-empty environment: the config and state folders must
    still be the user's real ones, found through the shell rather than APPDATA/USERPROFILE."""
    expected_cfg = os.path.join(C._known_folder(C.FOLDER_ROAMING_APPDATA), "pinaxium", "config.toml")
    expected_state = os.path.join(C._known_folder(C.FOLDER_LOCAL_APPDATA), "pinaxium", "state")
    for var in ("PINAXIUM_CONFIG", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME"):
        monkeypatch.delenv(var, raising=False)
    assert C.config_path() == expected_cfg
    assert C.default_state_dir() == expected_state


# ---------- launch command ----------

def test_launch_command_adds_base_directory_only_when_data_lives_elsewhere(clean_env):
    d = make_comfy(str(clean_env / "ComfyUI"))
    C.write_config(C.config_path(), {"comfy_dir": d, "comfy_args": ["--lowvram"]}, [])
    cmd = comfy_command(C.load())
    assert "--base-directory" not in cmd and cmd[-1] == "--lowvram" and cmd[0].endswith(PY)
    data = str(clean_env / "data")
    C.write_config(C.config_path(), {"comfy_dir": d, "base_dir": data}, [])
    cfg = C.load()
    cmd = comfy_command(cfg)
    assert cmd[cmd.index("--base-directory") + 1] == data
    assert cfg.output_dir == os.path.join(data, "output") and cfg.models_dir == os.path.join(data, "models")
