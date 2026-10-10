"""Stand-in for ComfyUI's HTTP API, for lifecycle and render tests.

    python fake_comfy.py PORT [BUSY_FILE]

- /system_stats, /queue: /queue reports a running job while BUSY_FILE exists.
- POST /prompt: "runs" the graph. Each SaveImage writes a small PNG to <cwd>/output/<filename_prefix>_00001_.png
  (cwd is ComfyUI's folder, as the supervisor starts it); a node {"class_type": "FakeDelay", "inputs":
  {"seconds": s}} makes the job take that long. /history/<id> reports outputs like ComfyUI does.
Records its pid in fake_comfy.pid (cwd).
"""

import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])
BUSY_FILE = sys.argv[2] if len(sys.argv) > 2 else None
JOBS: dict[str, dict] = {}          # prompt_id -> {"done_at": t, "outputs": {...}}
LOCK = threading.Lock()


def run_graph(graph: dict) -> dict:
    from PIL import Image
    outputs = {}
    for node_id, node in graph.items():
        if node.get("class_type") == "SaveImage":
            prefix = node["inputs"]["filename_prefix"]
            subfolder, _, stem = prefix.rpartition("/")
            folder = os.path.join(os.getcwd(), "output", *subfolder.split("/")) if subfolder else os.path.join(os.getcwd(), "output")
            os.makedirs(folder, exist_ok=True)
            n = 1
            while os.path.exists(os.path.join(folder, f"{stem}_{n:05d}_.png")):
                n += 1
            name = f"{stem}_{n:05d}_.png"
            Image.new("RGB", (64, 48), (n * 30 % 255, 80, 160)).save(os.path.join(folder, name))
            outputs[node_id] = {"images": [{"filename": name, "subfolder": subfolder, "type": "output"}]}
    return outputs


class H(BaseHTTPRequestHandler):
    def _json(self, body, code=200):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/system_stats":
            return self._json({"system": {"comfyui_version": "fake"},
                               "devices": [{"name": "fake", "vram_total": 16 << 30, "vram_free": 15 << 30,
                                            "torch_vram_total": 0, "torch_vram_free": 0}]})
        if self.path == "/queue":
            busy = BUSY_FILE is not None and os.path.exists(BUSY_FILE)
            with LOCK:
                running = [[0, pid, {}, {}, []] for pid, j in JOBS.items() if j["done_at"] > time.time()]
            if busy:
                running.append([0, "job", {}, {}, []])
            return self._json({"queue_running": running, "queue_pending": []})
        if self.path.startswith("/history/"):
            pid = self.path.rsplit("/", 1)[-1]
            with LOCK:
                job = JOBS.get(pid)
            if job is None or job["done_at"] > time.time():
                return self._json({})
            return self._json({pid: {"outputs": job["outputs"],
                                     "status": {"status_str": "success", "completed": True, "messages": []}}})
        self._json({}, 404)

    def do_POST(self):
        if self.path == "/prompt":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            graph = body["prompt"]
            delay = sum(float(n["inputs"].get("seconds", 0)) for n in graph.values() if n.get("class_type") == "FakeDelay")
            pid = uuid.uuid4().hex
            with LOCK:
                JOBS[pid] = {"done_at": time.time() + delay, "outputs": run_graph(graph)}
            return self._json({"prompt_id": pid, "number": len(JOBS), "node_errors": {}})
        if self.path in ("/free", "/interrupt"):
            return self._json({})
        self._json({}, 404)

    def log_message(self, *a):
        pass


with open("fake_comfy.pid", "w") as f:
    f.write(str(os.getpid()))
ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
