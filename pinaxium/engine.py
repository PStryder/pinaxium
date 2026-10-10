"""Client side of the engine: wake the shared supervisor, talk to ComfyUI's HTTP API."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable

import httpx

from pinaxium import supervisor as sup
from pinaxium.config import Config

Progress = Callable[[str], Awaitable[None]]
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class EngineError(RuntimeError):
    pass


async def _noop(_: str) -> None:
    pass


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client_id = uuid.uuid4().hex
        os.makedirs(cfg.state_dir, exist_ok=True)

    # ---------- lifecycle ----------

    def touch_activity(self) -> None:
        sup.touch(self.cfg.state_path("activity"))

    async def _get(self, route: str, timeout: float = 5.0):
        async with httpx.AsyncClient(base_url=self.cfg.base_url, timeout=timeout) as c:
            r = await c.get(route)
            r.raise_for_status()
            return r.json()

    async def api_up(self) -> bool:
        try:
            await self._get("/system_stats", timeout=2.0)
            return True
        except Exception:
            return False

    def managed(self) -> bool:
        return sup.lock_is_fresh(self.cfg)

    def _spawn_supervisor(self) -> None:
        """Start the shared supervisor so that it outlives this MCP server.

        MCP clients run their servers inside a Windows job object that kills every member when the session
        closes, and it doesn't allow breakaway. A process created through WMI (Win32_Process.Create) belongs to
        the WMI service instead, so it survives. It inherits no environment, hence the env file."""
        env_file = self.cfg.state_path("spawn_env.json")
        with open(env_file, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in os.environ.items() if k.startswith("PINAXIUM_")}, f)
        log = self.cfg.state_path("supervisor.log")
        if os.name == "nt":
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            cmd = [pyw if os.path.isfile(pyw) else sys.executable, "-m", "pinaxium.supervisor",
                   "--env-file", env_file, "--log", log]
            ps = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
                  "@{CommandLine=$env:PINAXIUM_SPAWN_CMD; CurrentDirectory=$env:PINAXIUM_SPAWN_CWD}; "
                  "exit $r.ReturnValue")
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                               env={**os.environ, "PINAXIUM_SPAWN_CMD": subprocess.list2cmdline(cmd),
                                    "PINAXIUM_SPAWN_CWD": PROJECT_DIR},
                               capture_output=True, text=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode == 0:
                return
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"WMI spawn failed ({r.returncode}): {r.stderr.strip()[-500:]}; falling back to a child process\n")
        cmd = [sys.executable, "-m", "pinaxium.supervisor", "--env-file", env_file, "--log", log]
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
        subprocess.Popen(cmd, cwd=PROJECT_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)

    async def ensure_ready(self, progress: Progress = _noop) -> str:
        """Start the engine if needed and wait until ComfyUI answers. Returns 'managed' or 'external'."""
        self.touch_activity()
        if await self.api_up():
            return "managed" if self.managed() else "external"
        spawned_at = time.time()
        if not self.managed():
            self._spawn_supervisor()
            await progress("starting engine")
        deadline = time.time() + self.cfg.start_timeout_s + 15
        while time.time() < deadline:
            await asyncio.sleep(1.0)
            self.touch_activity()
            if await self.api_up():
                return "managed"
            if not self.managed() and time.time() - spawned_at > 10:
                # no live supervisor (a killed one leaves a lock that looks alive until it goes stale)
                self._spawn_supervisor()
                spawned_at = time.time()
                await progress("restarting engine supervisor")
                continue
            state = sup.read_state(self.cfg)
            # a supervisor that has already given up says so in state.json
            if state.get("status") == "stopped" and state.get("updated_at", 0) > spawned_at and not self.managed():
                raise EngineError(f"engine failed to start: {state.get('reason')} "
                                  f"(log: {self.cfg.state_path('comfyui.log')})")
            await progress(f"waiting for engine ({time.time() - spawned_at:.0f}s)")
        raise EngineError(f"engine not ready after {self.cfg.start_timeout_s:.0f}s "
                          f"(log: {self.cfg.state_path('comfyui.log')})")

    def request_stop(self) -> bool:
        if not self.managed():
            return False
        sup.touch(self.cfg.state_path("stop"))
        return True

    async def status(self) -> dict:
        state = sup.read_state(self.cfg)
        up = await self.api_up()
        out: dict = {"engine_url": self.cfg.base_url, "idle_shutdown_minutes": self.cfg.idle_minutes}
        if up:
            out["status"] = "ready" if self.managed() else "external"
            stats = await self._get("/system_stats")
            q = await self._get("/queue")
            dev = (stats.get("devices") or [{}])[0]
            gb = 1024 ** 3
            used = (dev.get("vram_total", 0) - dev.get("vram_free", 0)) / gb
            out.update({
                "ready_to_render": True,
                "busy": bool(q.get("queue_running")),
                "queued_jobs": len(q.get("queue_pending", [])),
                "gpu": dev.get("name"),
                "vram_used_gb": round(used, 2),
                "vram_total_gb": round(dev.get("vram_total", 0) / gb, 2),
                "comfyui_version": stats.get("system", {}).get("comfyui_version"),
            })
            # GPU memory in use beyond what was in use when the engine became ready (whole GPU, so other
            # apps' growth counts too, hence "estimate"); torch's counters are unreliable under cudaMallocAsync
            base = state.get("vram_used_at_ready_gb")
            if self.managed() and base is not None:
                out["models_loaded_estimate"] = round(used - base, 1) > 1.5
                out["vram_used_by_models_gb"] = max(0.0, round(used - base, 1))
            if out["status"] == "ready":
                idle = time.time() - max(sup.mtime(self.cfg.state_path("activity")), state.get("ready_at", 0))
                out["idle_minutes"] = round(idle / 60, 1)
            else:
                out["note"] = "a ComfyUI not started by this service is on the port; it is used but never shut down"
        else:
            starting = self.managed() and state.get("status") == "starting"
            out.update({"status": "starting" if starting else "stopped", "ready_to_render": False})
            if state.get("reason"):
                out["last_stop_reason"] = state["reason"]
        return out

    # ---------- jobs ----------

    async def upload(self, path: str) -> str:
        if not os.path.isfile(path):
            raise EngineError(f"no such image: {path}")
        async with httpx.AsyncClient(base_url=self.cfg.base_url, timeout=60) as c:
            with open(path, "rb") as f:
                r = await c.post("/upload/image", files={"image": (os.path.basename(path), f)},
                                 data={"overwrite": "true", "subfolder": "pinaxium"})
            r.raise_for_status()
            j = r.json()
        return f"{j['subfolder']}/{j['name']}" if j.get("subfolder") else j["name"]

    async def submit(self, graph: dict) -> str:
        self.touch_activity()
        async with httpx.AsyncClient(base_url=self.cfg.base_url, timeout=30) as c:
            r = await c.post("/prompt", json={"prompt": graph, "client_id": self.client_id})
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or body.get("node_errors"):
            raise EngineError(f"ComfyUI rejected the workflow: {body.get('error') or r.text} "
                              f"{body.get('node_errors') or ''}")
        return body["prompt_id"]

    async def job(self, job_id: str) -> dict:
        """{'state': queued|running|done|error|unknown, 'images': [paths], 'error': str|None}"""
        hist = (await self._get(f"/history/{job_id}")).get(job_id)
        if hist is None:
            q = await self._get("/queue")
            if any(item[1] == job_id for item in q.get("queue_running", [])):
                return {"state": "running", "images": [], "error": None}
            if any(item[1] == job_id for item in q.get("queue_pending", [])):
                return {"state": "queued", "images": [], "error": None}
            return {"state": "unknown", "images": [], "error": None}
        st = hist.get("status", {})
        if st.get("status_str") == "error":
            msgs = [m[1] for m in st.get("messages", []) if m[0] == "execution_error"]
            err = "; ".join(f"{m.get('node_type')}: {m.get('exception_message', '').strip()}" for m in msgs)
            return {"state": "error", "images": [], "error": err or "execution failed"}
        images = []
        dirs = {"output": self.cfg.output_dir, "temp": os.path.join(self.cfg.comfy_dir, "temp"),
                "input": os.path.join(self.cfg.comfy_dir, "input")}
        for node_out in hist.get("outputs", {}).values():
            for im in node_out.get("images", []):
                if im.get("type") in dirs:
                    images.append(os.path.join(dirs[im["type"]], im.get("subfolder", ""), im["filename"]))
        return {"state": "done", "images": images, "error": None}

    async def wait(self, job_id: str, max_wait_s: float, progress: Progress = _noop) -> dict:
        t0 = time.time()
        while True:
            self.touch_activity()
            j = await self.job(job_id)
            if j["state"] in ("done", "error"):
                return j
            if time.time() - t0 > max_wait_s:
                return j
            await progress(f"{j['state']} ({time.time() - t0:.0f}s)")
            await asyncio.sleep(1.0)

    async def free_vram(self) -> None:
        async with httpx.AsyncClient(base_url=self.cfg.base_url, timeout=30) as c:
            (await c.post("/free", json={"unload_models": True, "free_memory": True})).raise_for_status()

    async def interrupt(self) -> None:
        async with httpx.AsyncClient(base_url=self.cfg.base_url, timeout=10) as c:
            (await c.post("/interrupt")).raise_for_status()

    async def model_files(self) -> dict:
        out = {}
        for folder in ("diffusion_models", "text_encoders", "vae", "loras"):
            try:
                out[folder] = await self._get(f"/models/{folder}")
            except Exception:
                out[folder] = None
        return out
