"""The always-on HTTP service: MCP over streamable HTTP for any local client, clickable image links, the local
Host/Origin boundary, and the firm rule that serving MCP metadata and existing images never needs ComfyUI."""

import asyncio
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from PIL import Image

from pinaxium.config import load
from pinaxium.projects import Projects

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def service(tmp_path, monkeypatch):
    port, dead_engine_port = free_port(), free_port()           # nothing will ever listen on the engine port
    env = {"PINAXIUM_COMFY_DIR": str(tmp_path / "comfy"), "PINAXIUM_STATE_DIR": str(tmp_path / "state"),
           "PINAXIUM_FINALS_DIR": str(tmp_path / "finals"), "PINAXIUM_SERVICE_PORT": str(port),
           "PINAXIUM_PORT": str(dead_engine_port)}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    pj = Projects(load(create=False))
    os.makedirs(pj.drafts_dir("art"))
    draft = os.path.join(pj.drafts_dir("art"), "img_00001_.png")
    Image.new("RGB", (40, 30), (200, 10, 10)).save(draft)
    pj.record("art", [draft], {"kind": "generate", "prompt": "red", "seed": 1})
    other = os.path.join(pj.drafts_dir("art"), "img_00002_.png")
    Image.new("RGB", (40, 30), (10, 200, 10)).save(other)
    pj.record("art", [other], {"kind": "generate", "prompt": "green", "seed": 2})
    final = pj.finalize(other, "keeper")
    proc = subprocess.Popen([sys.executable, "-m", "pinaxium.service", "run"], cwd=PROJECT, env={**os.environ, **env},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        proc.kill()
        raise AssertionError("service didn't start: " + proc.stdout.read().decode(errors="replace")[-2000:])
    yield {"base": base, "port": port, "draft": draft, "final": final, "projects": pj, "env": env, "tmp": tmp_path,
           "engine_port": dead_engine_port}
    proc.kill()
    proc.wait(10)


def test_any_local_client_can_use_mcp_over_http_and_gets_clickable_links(service):
    async def go():
        async with streamable_http_client(service["base"] + "/mcp") as streams, ClientSession(*streams) as s:
            init = await s.initialize()
            assert init.server_info.name == "pinaxium"
            assert init.capabilities.resources.list_changed is True          # also over HTTP
            names = {t.name for t in (await s.list_tools()).tools}
            assert {"generate_image", "list_finals", "finalize_image"} <= names
            drafts = (await s.call_tool("list_drafts", {})).structured_content["drafts"]
            d = next(x for x in drafts if x["path"].endswith("img_00001_.png"))
            assert d["file_uri"].startswith("file:///") and d["file_uri"].endswith("/art/img_00001_.png")
            assert d["local_url"] == f"{service['base']}/files/art/drafts/img_00001_.png"
            finals = (await s.call_tool("list_finals", {})).structured_content["finals"]
            assert finals[0]["local_url"] == f"{service['base']}/files/art/finals/keeper"
            return d, finals[0]
    d, f = asyncio.run(go())
    with open(service["draft"], "rb") as fh:
        assert httpx.get(d["local_url"]).content == fh.read()                 # the link opens the image
    assert httpx.get(f["local_url"]).headers["content-type"] == "image/png"


def test_images_are_served_with_the_gpu_engine_completely_down(service):
    """The firm boundary: the archive never depends on ComfyUI. Nothing listens on the engine port, and the
    service never tries to start the engine to serve images."""
    pj, base = service["projects"], service["base"]
    moved = os.path.join(pj.finals_root, "renamed elsewhere.png")
    os.replace(service["final"]["path"], moved)                             # finals still resolve after a rename
    assert httpx.get(f"{base}/files/art/finals/keeper").status_code == 200
    assert httpx.get(f"{base}/files/art/drafts/img_00001_.png").status_code == 200
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", service["engine_port"])) != 0       # the engine is not running
    state = service["env"]["PINAXIUM_STATE_DIR"]
    assert not os.path.exists(os.path.join(state, "spawn_env.json")), "the service tried to start the engine"


def test_only_this_machines_requests_and_only_pinaxium_images(service):
    base = service["base"]
    evil_host = httpx.get(f"{base}/files/art/drafts/img_00001_.png", headers={"Host": "evil.example"})
    assert evil_host.status_code == 403                                       # DNS-rebinding page
    for bad in ["/files/art/drafts/..%2F..%2Fstate%2Fstate.json", "/files/art/drafts/nope.png",
                "/files/art/finals/nope", "/files/..%2F/drafts/x.png"]:
        assert httpx.get(base + bad).status_code == 404, bad
    rebinding = httpx.post(f"{base}/mcp", headers={"Host": "evil.example", "Content-Type": "application/json"},
                           json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert rebinding.status_code in (403, 421)
    foreign_page = httpx.post(f"{base}/mcp", headers={"Origin": "https://evil.example",
                                                       "Content-Type": "application/json"},
                              json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert foreign_page.status_code in (403, 421)


def test_a_second_service_on_the_same_port_exits(service):
    r = subprocess.run([sys.executable, "-m", "pinaxium.service", "run"], cwd=PROJECT,
                       env={**os.environ, **service["env"]}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "in use" in (r.stdout + r.stderr)
