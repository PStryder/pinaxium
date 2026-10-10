"""Engine lifecycle against a fake ComfyUI: start, idle shutdown, keep-alive, stop flag, single owner,
stale-lock takeover, and never killing a ComfyUI this service didn't start."""

import asyncio
import ctypes
import json
import os
import socket
import subprocess
import sys
import time

import pytest

from pinaxium import supervisor as sup
from pinaxium.config import load
from pinaxium.engine import Engine

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
FAKE = os.path.join(HERE, "fake_comfy.py")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    k.GetExitCodeProcess(h, ctypes.byref(code))
    k.CloseHandle(h)
    return code.value == 259                           # STILL_ACTIVE


def wait_for(cond, timeout=20.0, step=0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


@pytest.fixture
def env(tmp_path, monkeypatch):
    port = free_port()
    state = tmp_path / "state"
    state.mkdir()
    busy = tmp_path / "busy"
    monkeypatch.setenv("PINAXIUM_PORT", str(port))
    monkeypatch.setenv("PINAXIUM_STATE_DIR", str(state))
    monkeypatch.setenv("PINAXIUM_COMFY_DIR", str(tmp_path))
    monkeypatch.setenv("PINAXIUM_IDLE_MINUTES", str(6 / 60))        # 6 s
    monkeypatch.setenv("PINAXIUM_START_TIMEOUT_S", "20")
    monkeypatch.setenv("PINAXIUM_ENGINE_CMD", json.dumps([sys.executable, FAKE, str(port), str(busy)]))
    cfg = load()
    procs = []

    def start_supervisor():
        p = subprocess.Popen([sys.executable, "-m", "pinaxium.supervisor"], cwd=PROJECT, env=os.environ.copy())
        procs.append(p)
        return p

    def fake_pid():
        return int((tmp_path / "fake_comfy.pid").read_text())

    yield cfg, start_supervisor, fake_pid, busy, tmp_path
    (state / "stop").touch()
    for p in procs:
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.kill()


def ready(cfg):
    return sup.read_state(cfg).get("status") == "ready" and sup.api_get(cfg, "/system_stats") is not None


def test_starts_then_shuts_down_when_idle(env):
    cfg, start, fake_pid, _, _ = env
    p = start()
    assert wait_for(lambda: ready(cfg)), "engine never became ready"
    pid = fake_pid()
    assert alive(pid) and sup.lock_is_fresh(cfg)
    assert p.wait(30) == 0
    st = sup.read_state(cfg)
    assert st["status"] == "stopped" and "idle" in st["reason"]
    assert wait_for(lambda: not alive(pid), 10), "engine process survived idle shutdown"
    assert not os.path.exists(cfg.state_path("engine.lock"))


def test_activity_keeps_engine_alive(env):
    cfg, start, fake_pid, _, _ = env
    p = start()
    assert wait_for(lambda: ready(cfg))
    end = time.time() + 12                       # twice the idle limit
    while time.time() < end:
        sup.touch(cfg.state_path("activity"))
        time.sleep(1)
    assert p.poll() is None and alive(fake_pid()), "engine stopped despite activity"
    assert p.wait(30) == 0 and "idle" in sup.read_state(cfg)["reason"]


def test_busy_queue_keeps_engine_alive(env):
    cfg, start, fake_pid, busy, _ = env
    busy.touch()
    p = start()
    assert wait_for(lambda: ready(cfg))
    time.sleep(12)
    assert p.poll() is None and alive(fake_pid()), "engine stopped while a job was running"
    busy.unlink()
    assert p.wait(30) == 0 and "idle" in sup.read_state(cfg)["reason"]


def test_stop_flag_stops_engine(env, monkeypatch):
    cfg, start, fake_pid, _, _ = env
    p = start()
    assert wait_for(lambda: ready(cfg))
    pid = fake_pid()
    assert Engine(cfg).request_stop()
    assert p.wait(15) == 0
    assert sup.read_state(cfg)["reason"] == "stop requested"
    assert wait_for(lambda: not alive(pid), 10)


def test_second_supervisor_defers_to_the_first(env):
    cfg, start, fake_pid, _, tmp = env
    p1 = start()
    assert wait_for(lambda: ready(cfg))
    pid = fake_pid()
    p2 = start()
    assert p2.wait(15) == 0                      # exits at once without starting an engine
    assert fake_pid() == pid and p1.poll() is None


def test_stale_lock_is_taken_over(env):
    cfg, start, _, _, _ = env
    lock = cfg.state_path("engine.lock")
    with open(lock, "w") as f:
        f.write("99999")
    old = time.time() - sup.STALE_S - 5
    os.utime(lock, (old, old))
    start()
    assert wait_for(lambda: ready(cfg)), "stale lock blocked startup"


def test_fresh_lock_is_respected(env):
    cfg, start, _, _, tmp = env
    sup.touch(cfg.state_path("engine.lock"))
    assert start().wait(15) == 0
    assert not (tmp / "fake_comfy.pid").exists(), "started an engine despite a live lock"


def test_external_comfyui_is_used_but_never_stopped(env):
    cfg, start, fake_pid, _, tmp = env
    ext = subprocess.Popen([sys.executable, FAKE, str(cfg.port)], cwd=str(tmp))
    try:
        assert wait_for(lambda: sup.api_get(cfg, "/system_stats") is not None)
        eng = Engine(cfg)
        assert asyncio.run(eng.ensure_ready()) == "external"
        assert asyncio.run(eng.status())["status"] == "external"
        assert not eng.request_stop()
        assert start().wait(15) == 1             # a supervisor refuses to take the port
        assert "already served" in sup.read_state(cfg)["reason"]
        assert ext.poll() is None, "external ComfyUI was killed"
    finally:
        ext.kill()


def test_engine_wakes_supervisor_on_demand(env):
    cfg, _, fake_pid, _, _ = env
    eng = Engine(cfg)
    assert asyncio.run(eng.status())["status"] == "stopped"
    assert asyncio.run(eng.ensure_ready()) == "managed"
    s = asyncio.run(eng.status())
    assert s["status"] == "ready" and s["ready_to_render"] is True
    pid = fake_pid()
    assert eng.request_stop()
    assert wait_for(lambda: sup.read_state(cfg).get("status") == "stopped" and not alive(pid), 20)
    assert asyncio.run(eng.status())["status"] == "stopped"


SPAWN_FROM_CHILD = (
    "import asyncio, time; time.sleep(1.5)\n"           # give the parent time to put us in the job
    "from pinaxium.config import load\n"
    "from pinaxium.engine import Engine\n"
    "print(asyncio.run(Engine(load()).ensure_ready()), flush=True)\n"
    "time.sleep(600)\n"
)


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_engine_survives_the_mcp_clients_job_being_closed(env):
    """MCP clients run servers in a kill-on-close job object without breakaway; closing the session must
    not take the shared engine down with it."""
    from ctypes import wintypes
    cfg, _, fake_pid, _, _ = env
    k = ctypes.windll.kernel32
    k.CreateJobObjectW.restype = wintypes.HANDLE
    job = k.CreateJobObjectW(None, None)

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    info = EXTENDED()
    info.Basic.LimitFlags = 0x2000                      # KILL_ON_JOB_CLOSE, and no BREAKAWAY_OK
    assert k.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
    child = subprocess.Popen([sys.executable, "-c", SPAWN_FROM_CHILD], cwd=PROJECT, env=os.environ.copy(),
                             stdout=subprocess.PIPE, text=True)
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x1F0FFF, False, child.pid)
    assert k.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(h))
    k.CloseHandle(wintypes.HANDLE(h))
    try:
        assert child.stdout.readline().strip() == "managed"
        assert wait_for(lambda: ready(cfg))
        pid = fake_pid()
    finally:
        k.CloseHandle(wintypes.HANDLE(job))            # the "session" ends: everything in the job dies
    assert child.wait(10) is not None
    time.sleep(3)
    assert alive(pid), "engine died with the client's job"
    assert sup.lock_is_fresh(cfg) and sup.api_get(cfg, "/system_stats") is not None


def test_dead_supervisor_is_replaced_after_its_lock_goes_stale(env, monkeypatch):
    """A killed supervisor leaves a lock that looks alive; a request in that window must still get an engine."""
    import pinaxium.supervisor as supmod
    monkeypatch.setattr(supmod, "STALE_S", 4.0)       # the client's view; the spawned supervisor uses its own
    cfg, _, _, _, _ = env
    sup.touch(cfg.state_path("engine.lock"))           # "fresh" lock, nobody behind it
    old = time.time() - 26                             # goes stale for the real supervisor (30 s) within ~4 s
    os.utime(cfg.state_path("engine.lock"), (old, old))
    assert asyncio.run(Engine(cfg).ensure_ready()) == "managed"


def test_failed_start_is_reported(env, monkeypatch):
    cfg, _, _, _, _ = env
    monkeypatch.setenv("PINAXIUM_ENGINE_CMD", json.dumps([sys.executable, "-c", "raise SystemExit(3)"]))
    eng = Engine(load())
    with pytest.raises(Exception, match="code 3"):
        asyncio.run(eng.ensure_ready())
