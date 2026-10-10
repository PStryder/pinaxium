"""Stand-in for ComfyUI's HTTP API, for lifecycle tests.

    python fake_comfy.py PORT [BUSY_FILE]

/queue reports a running job while BUSY_FILE exists. Records its pid in fake_comfy.pid (cwd).
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])
BUSY_FILE = sys.argv[2] if len(sys.argv) > 2 else None


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/system_stats":
            body = {"system": {"comfyui_version": "fake"},
                    "devices": [{"name": "fake", "vram_total": 16 << 30, "vram_free": 15 << 30,
                                 "torch_vram_total": 0, "torch_vram_free": 0}]}
        elif self.path == "/queue":
            busy = BUSY_FILE is not None and os.path.exists(BUSY_FILE)
            body = {"queue_running": [[0, "job", {}, {}, []]] if busy else [], "queue_pending": []}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


with open("fake_comfy.pid", "w") as f:
    f.write(str(os.getpid()))
HTTPServer(("127.0.0.1", PORT), H).serve_forever()
