"""The always-on Pinaxium service: one process on 127.0.0.1:<service_port> that any local MCP client can use.

  http://127.0.0.1:<service_port>/mcp     MCP (streamable HTTP)
  http://127.0.0.1:<service_port>/files/<project>/drafts/<file>     a draft, for the user's browser
  http://127.0.0.1:<service_port>/files/<project>/finals/<id>       a final, by its permanent id
  http://127.0.0.1:<service_port>/health  liveness

It holds no GPU memory: ComfyUI is started on demand by renders and stopped when idle, as with stdio clients.
Serving MCP metadata and existing images never needs ComfyUI.

    python -m pinaxium.service run         run in the foreground (what the logon task runs)
    python -m pinaxium.service install     Windows: start at logon via Task Scheduler (and start now)
    python -m pinaxium.service uninstall
    python -m pinaxium.service status      task state and whether the service answers
    python -m pinaxium.service restart
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys

import httpx

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASK_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "service_task.ps1")
TASK_NAME = "Pinaxium"


def _port_taken(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def run() -> int:
    from pinaxium.config import load
    cfg = load()
    os.makedirs(cfg.state_dir, exist_ok=True)
    if sys.stdout is None or os.path.basename(sys.executable).lower().startswith("pythonw"):
        log = open(cfg.state_path("service.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
    if _port_taken(cfg.service_port):
        print(f"port {cfg.service_port} is in use (another Pinaxium service?); exiting", flush=True)
        return 1
    from mcp.server.transport_security import TransportSecuritySettings
    from pinaxium import server
    server.SERVICE_MODE = True
    port = cfg.service_port
    local = [f"127.0.0.1:{port}", f"localhost:{port}"]
    # MCP endpoint: only requests addressed to this machine, and only from local pages if from a browser at all
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=local,
                                         allowed_origins=[f"http://{h}" for h in local])
    print(f"pinaxium service on http://127.0.0.1:{port}/mcp", flush=True)
    server.mcp.run("streamable-http", host="127.0.0.1", port=port, transport_security=security)
    return 0


def _task(action: str, **extra) -> str:
    if os.name != "nt":
        raise SystemExit("automatic start is implemented for Windows only; on Linux/macOS run "
                         "`python -m pinaxium.service run` from a systemd user unit or a launchd agent")
    args = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", TASK_SCRIPT,
            "-Action", action, "-TaskName", TASK_NAME]
    for k, v in extra.items():
        args += [f"-{k}", v]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"{action} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def _answering(port: int) -> str:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
        return f"answering on http://127.0.0.1:{port}/mcp (version {r.json().get('version')})"
    except Exception:
        return f"not answering on port {port}"


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "run":
        return run()
    from pinaxium.config import load
    port = load().service_port
    if cmd == "install":
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        print(_task("install", Python=pyw if os.path.isfile(pyw) else sys.executable, WorkDir=PROJECT_DIR))
    elif cmd == "uninstall":
        print(_task("uninstall"))
    elif cmd == "restart":
        _task("stop")
        print(_task("start"))
    elif cmd == "status":
        print(_task("status") if os.name == "nt" else "no task (not Windows)")
    else:
        print(__doc__)
        return 2
    import time
    if cmd in ("install", "restart"):
        for _ in range(40):                     # give it a moment to come up
            if "answering on" in _answering(port):
                break
            time.sleep(0.5)
    print(_answering(port))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
