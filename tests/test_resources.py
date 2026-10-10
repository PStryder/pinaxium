"""Resources over a real stdio session: capabilities, listing, full-size reads, URI safety, and change
notifications (new image -> list_changed; engine state / log change -> updated for subscribers)."""

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import time

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image

from pinaxium import resources as res
from pinaxium.config import load
from pinaxium.projects import ProjectError, Projects

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
FAKE = os.path.join(HERE, "fake_comfy.py")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def world(tmp_path, monkeypatch):
    comfy, state = tmp_path / "comfy", tmp_path / "state"
    images = comfy / "output" / "pinaxium" / "art"            # project "art"
    images.mkdir(parents=True)
    state.mkdir()
    port = free_port()
    env = {"PINAXIUM_COMFY_DIR": str(comfy), "PINAXIUM_STATE_DIR": str(state), "PINAXIUM_PORT": str(port),
           "PINAXIUM_FINALS_DIR": str(tmp_path / "finals")}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Image.new("RGB", (2048, 2048), (10, 20, 30)).save(images / "old.png")
    os.utime(images / "old.png", (time.time() - 100, time.time() - 100))
    Image.new("RGB", (64, 64), (200, 0, 0)).save(images / "new.png")
    Image.new("RGB", (32, 32), (0, 200, 0)).save(images / "keeper.png")
    os.utime(images / "keeper.png", (time.time() - 200, time.time() - 200))
    cfg = load()
    projects = Projects(cfg)
    projects.record("art", [str(images / "keeper.png")], {"kind": "generate", "prompt": "a green square", "seed": 1})
    final = projects.finalize(str(images / "keeper.png"), "hero-001", title="Hero")
    return {"cfg": cfg, "projects": projects, "images": images, "final": final, "state": state, "port": port,
            "env": env, "tmp": tmp_path}


class Recorder:
    def __init__(self):
        self.events: list[tuple[str, str | None]] = []

    async def __call__(self, msg):
        root = getattr(msg, "root", msg)
        method = getattr(root, "method", None)
        if method:
            params = getattr(root, "params", None)
            self.events.append((method, str(getattr(params, "uri", "")) or None))

    async def wait_for(self, method: str, uri: str | None = None, timeout: float = 10.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if any(m == method and (uri is None or u == uri) for m, u in self.events):
                return True
            await asyncio.sleep(0.1)
        return False


def session(world, rec: Recorder | None = None):
    params = StdioServerParameters(command=sys.executable, args=["-m", "pinaxium.server"], cwd=PROJECT,
                                   env={**os.environ, **world["env"]})
    return stdio_client(params), (lambda streams: ClientSession(*streams, message_handler=rec) if rec else ClientSession(*streams))


def run(coro):
    return asyncio.run(coro)


# ---------- unit: URI safety and links ----------

@pytest.mark.parametrize("uri", ["pinaxium://projects/art/drafts/..%2F..%2Fstate%2Fstate.json",
                                 "pinaxium://projects/art/drafts/..", "pinaxium://projects/art/drafts/nope.png",
                                 "pinaxium://projects/art/drafts/old.jpg", "pinaxium://projects/../drafts/old.png",
                                 "pinaxium://projects/art/finals/missing-id", "pinaxium://projects/art"])
def test_image_uris_resolve_only_to_pinaxium_images(world, uri):
    with pytest.raises(ProjectError):
        world["projects"].locate(uri)


def test_resource_links_for_drafts_and_finals_only(world):
    pj = world["projects"]
    link = res.resource_link(pj, str(world["images"] / "new.png"))
    assert str(link.uri) == "pinaxium://projects/art/drafts/new.png" and link.mime_type == "image/png"
    assert link.size == os.path.getsize(world["images"] / "new.png")
    final_link = res.resource_link(pj, world["final"]["path"])
    assert str(final_link.uri) == "pinaxium://projects/art/finals/hero-001"
    other = world["tmp"] / "elsewhere.png"
    Image.new("RGB", (8, 8)).save(other)
    assert res.resource_link(pj, str(other)) is None


# ---------- over stdio ----------

def test_capabilities_listing_and_full_size_reads(world):
    async def go():
        client, make = session(world)
        async with client as streams, make(streams) as s:
            init = await s.initialize()
            caps = init.capabilities.resources
            assert caps.subscribe is True and caps.list_changed is True
            listed = (await s.list_resources()).resources
            uris = [str(r.uri) for r in listed]
            assert uris[:3] == [res.ENGINE_URI, "pinaxium://logs/comfyui", "pinaxium://logs/supervisor"]
            assert uris[3:] == ["pinaxium://projects/art/finals/hero-001",                 # finals first
                                "pinaxium://projects/art/drafts/new.png",                  # then drafts, newest first
                                "pinaxium://projects/art/drafts/old.png",
                                "pinaxium://projects/art/drafts/keeper.png",
                                "pinaxium://projects/art"]                                 # then project manifests
            old = next(r for r in listed if str(r.uri).endswith("old.png"))
            assert old.size == os.path.getsize(world["images"] / "old.png") and old.annotations.last_modified
            templates = (await s.list_resource_templates()).resource_templates
            assert {t.uri_template for t in templates} == {res.DRAFT_TEMPLATE, res.FINAL_TEMPLATE, res.PROJECT_TEMPLATE}
            blob = (await s.read_resource("pinaxium://projects/art/drafts/old.png")).contents[0]
            assert blob.mime_type == "image/png"
            assert base64.b64decode(blob.blob) == (world["images"] / "old.png").read_bytes()  # full size, unchanged
            final = (await s.read_resource("pinaxium://projects/art/finals/hero-001")).contents[0]
            with open(world["final"]["path"], "rb") as f:
                assert base64.b64decode(final.blob) == f.read()
            doc = json.loads((await s.read_resource("pinaxium://projects/art")).contents[0].text)
            assert doc["finals"]["hero-001"]["title"] == "Hero" and doc["drafts"]["keeper.png"]["prompt"]
            status = (await s.read_resource(res.ENGINE_URI)).contents[0].text
            assert '"status": "stopped"' in status
            for bad in ["pinaxium://projects/art/drafts/..%2F..%2Fstate%2Fstate.json",
                        "pinaxium://projects/art/finals/nope"]:
                with pytest.raises(Exception):
                    await s.read_resource(bad)
    run(go())


def test_new_image_sends_list_changed(world):
    async def go():
        rec = Recorder()
        client, make = session(world, rec)
        async with client as streams, make(streams) as s:
            await s.initialize()
            for _ in range(3):                     # several requests: still one client, one notification per change
                await s.list_resources()
            await asyncio.sleep(res.POLL_S + 0.5)
            assert not any(m == "notifications/resources/list_changed" for m, _ in rec.events)
            Image.new("RGB", (32, 32)).save(world["images"] / "fresh.png")
            assert await rec.wait_for("notifications/resources/list_changed")
            await asyncio.sleep(res.POLL_S * 2)
            assert sum(m == "notifications/resources/list_changed" for m, _ in rec.events) == 1, rec.events
            assert "pinaxium://projects/art/drafts/fresh.png" in [str(r.uri) for r in (await s.list_resources()).resources]
    run(go())


def test_engine_status_and_log_updates_reach_subscribers_only(world):
    async def go():
        rec = Recorder()
        client, make = session(world, rec)
        async with client as streams, make(streams) as s:
            await s.initialize()
            await s.subscribe_resource(res.ENGINE_URI)
            await s.subscribe_resource("pinaxium://logs/comfyui")
            await asyncio.sleep(res.POLL_S + 1)    # updates are for changes after the watcher's first look
            fake = subprocess.Popen([sys.executable, FAKE, str(world["port"])], cwd=str(world["tmp"]))
            try:
                assert await rec.wait_for("notifications/resources/updated", res.ENGINE_URI, 15), rec.events
                status = (await s.read_resource(res.ENGINE_URI)).contents[0].text
                assert '"status": "external"' in status
                with open(world["state"] / "comfyui.log", "a") as f:
                    f.write("got prompt\n")
                assert await rec.wait_for("notifications/resources/updated", "pinaxium://logs/comfyui")
                assert not any(u == "pinaxium://logs/supervisor" for _, u in rec.events)       # not subscribed
                await s.unsubscribe_resource("pinaxium://logs/comfyui")
                n = len(rec.events)
                with open(world["state"] / "comfyui.log", "a") as f:
                    f.write("more\n")
                await asyncio.sleep(res.POLL_S * 2 + 0.5)
                assert not any(u == "pinaxium://logs/comfyui" for _, u in rec.events[n:]), "updated after unsubscribe"
            finally:
                fake.kill()
    run(go())
