"""Engine supervisor: owns one ComfyUI process and shuts it down when idle.

Started detached by the MCP server (`python -m pinaxium.supervisor`), so it is shared by every MCP
client (Claude Desktop, Claude Code) and outlives any one of them. Stdlib only.

Files in the state dir:
  engine.lock   exclusive lock; its mtime is the supervisor's heartbeat (touched every POLL_S)
  state.json    {"status": starting|ready|stopped, ...}
  activity      touched by MCP servers on every request; idle time counts from the latest of this,
                the last time ComfyUI's queue was non-empty, and the engine start
  stop          request flag: the supervisor stops ComfyUI and exits when it appears
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

from pinaxium.config import Config, load

POLL_S = 2.0
STALE_S = 30.0          # a lock whose heartbeat is older than this belongs to a dead supervisor
STOP_GRACE_S = 15.0


def write_state(cfg: Config, **fields) -> None:
    tmp = cfg.state_path("state.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({**fields, "updated_at": time.time()}, f)
    os.replace(tmp, cfg.state_path("state.json"))


def read_state(cfg: Config) -> dict:
    try:
        with open(cfg.state_path("state.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"status": "stopped"}


def lock_is_fresh(cfg: Config) -> bool:
    try:
        return time.time() - os.path.getmtime(cfg.state_path("engine.lock")) < STALE_S
    except OSError:
        return False


def acquire_lock(cfg: Config) -> bool:
    path = cfg.state_path("engine.lock")
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            if lock_is_fresh(cfg):
                return False
            try:
                os.remove(path)      # stale: its supervisor died without cleaning up
            except OSError:
                return False
    return False


def touch(path: str) -> None:
    with open(path, "a"):
        os.utime(path, None)


def mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def api_get(cfg: Config, route: str, timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(cfg.base_url + route, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def queue_busy(cfg: Config) -> bool | None:
    q = api_get(cfg, "/queue")
    if q is None:
        return None
    return bool(q.get("queue_running") or q.get("queue_pending"))


def comfy_command(cfg: Config) -> list[str]:
    override = os.environ.get("PINAXIUM_ENGINE_CMD")      # tests substitute a fake engine
    if override:
        return json.loads(override)
    cmd = [cfg.comfy_python, "main.py", "--listen", cfg.host, "--port", str(cfg.port),
           "--disable-auto-launch", "--preview-method", "none"]
    if os.path.normcase(os.path.abspath(cfg.base_dir)) != os.path.normcase(os.path.abspath(cfg.comfy_dir)):
        cmd += ["--base-directory", cfg.base_dir]          # data kept apart from the code (ComfyUI Desktop)
    return cmd + list(cfg.comfy_args)


def stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(5)


def run(cfg: Config) -> int:
    os.makedirs(cfg.state_dir, exist_ok=True)
    if not acquire_lock(cfg):
        return 0                                   # another live supervisor owns the engine
    lock, stop_flag = cfg.state_path("engine.lock"), cfg.state_path("stop")
    proc = None
    reason = "unknown"
    try:
        if os.path.exists(stop_flag):
            os.remove(stop_flag)
        if api_get(cfg, "/system_stats") is not None:
            reason = f"port {cfg.port} is already served by another process"
            return 1
        problems = [] if os.environ.get("PINAXIUM_ENGINE_CMD") else cfg.problems()
        if problems:
            reason = "; ".join(problems)
            return 1
        log = open(cfg.state_path("comfyui.log"), "w", encoding="utf-8", errors="replace")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.Popen(comfy_command(cfg), cwd=cfg.comfy_dir, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags)
        started = time.time()
        base = {"supervisor_pid": os.getpid(), "engine_pid": proc.pid, "started_at": started,
                "port": cfg.port, "idle_minutes": cfg.idle_minutes}
        write_state(cfg, status="starting", **base)

        while api_get(cfg, "/system_stats") is None:
            touch(lock)
            if proc.poll() is not None:
                reason = f"engine exited during startup with code {proc.returncode}; see comfyui.log"
                return 1
            if os.path.exists(stop_flag):
                reason = "stop requested"
                return 0
            if time.time() - started > cfg.start_timeout_s:
                reason = f"engine not ready after {cfg.start_timeout_s:.0f}s; see comfyui.log"
                return 1
            time.sleep(0.5)
        dev = ((api_get(cfg, "/system_stats") or {}).get("devices") or [{}])[0]
        used_gb = (dev.get("vram_total", 0) - dev.get("vram_free", 0)) / 2**30
        # baseline for "are models loaded": torch's own counters are unreliable under cudaMallocAsync
        write_state(cfg, status="ready", ready_at=time.time(), vram_used_at_ready_gb=round(used_gb, 2), **base)

        last_busy = time.time()
        while True:
            touch(lock)
            if os.path.exists(stop_flag):
                reason = "stop requested"
                return 0
            if proc.poll() is not None:
                reason = f"engine exited with code {proc.returncode}; see comfyui.log"
                return 1
            if queue_busy(cfg):
                last_busy = time.time()
            idle_since = max(last_busy, mtime(cfg.state_path("activity")), started)
            if time.time() - idle_since > cfg.idle_minutes * 60:
                reason = f"idle for {cfg.idle_minutes:g} min"
                return 0
            time.sleep(POLL_S)
    finally:
        if proc is not None:
            stop_process(proc)
        write_state(cfg, status="stopped", reason=reason, stopped_at=time.time())
        for p in (lock, stop_flag):
            try:
                os.remove(p)
            except OSError:
                pass


def main(argv: list[str]) -> int:
    """--env-file F: apply PINAXIUM_* settings from a JSON file first (a process created through WMI, as the
    engine does to escape the MCP client's job object, inherits no environment from its requester).
    --log F: append stdout/stderr to F (pythonw has neither)."""
    if "--env-file" in argv:
        path = argv[argv.index("--env-file") + 1]
        with open(path, encoding="utf-8") as f:
            os.environ.update({k: v for k, v in json.load(f).items() if k.startswith("PINAXIUM_")})
    if "--log" in argv:
        sys.stdout = sys.stderr = open(argv[argv.index("--log") + 1], "a", encoding="utf-8", buffering=1)
    return run(load())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
