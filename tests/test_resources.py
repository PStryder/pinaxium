"""Resources over a real stdio session: capabilities, listing, full-size reads, URI safety, and change
notifications (new image -> list_changed; engine state / log change -> updated for subscribers)."""

import asyncio
import base64
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
    images = comfy / "output" / "pinaxium"
    images.mkdir(parents=True)
    state.mkdir()
    port = free_port()
    env = {"PINAXIUM_COMFY_DIR": str(comfy), "PINAXIUM_STATE_DIR": str(state), "PINAXIUM_PORT": str(port)}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    Image.new("RGB", (2048, 2048), (10, 20, 30)).save(images / "old.png")
    os.utime(images / "old.png", (time.time() - 100, time.time() - 100))
    Image.new("RGB", (64, 64), (200, 0, 0)).save(images / "new.png")
    return {"cfg": load(), "images": images, "state": state, "port": port, "env": env, "tmp": tmp_path}


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

@pytest.mark.parametrize("name", ["..%2F..%2Fstate%2Fstate.json", "..\\..\\x.png", "../x.png", "/etc/x.png",
                                  "C:%5Cx.png", "nope.png", "old.jpg", "", ".."])
def test_image_path_refuses_anything_outside_the_images_folder(world, name):
    with pytest.raises(ValueError):
        res.image_path(world["cfg"], name)


def test_image_path_accepts_saved_images(world):
    assert res.image_path(world["cfg"], "old.png") == os.path.realpath(world["images"] / "old.png")


def test_resource_links_only_for_exposed_images(world):
    link = res.resource_link(world["cfg"], str(world["images"] / "new.png"))
    assert link.uri.unicode_string() if hasattr(link.uri, "unicode_string") else str(link.uri)
    assert str(link.uri) == "pinaxium://images/new.png" and link.mime_type == "image/png"
    assert link.size == os.path.getsize(world["images"] / "new.png")
    other = world["tmp"] / "elsewhere.png"
    Image.new("RGB", (8, 8)).save(other)
    assert res.resource_link(world["cfg"], str(other)) is None


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
            assert uris[3:] == ["pinaxium://images/new.png", "pinaxium://images/old.png"]      # newest first
            old = next(r for r in listed if str(r.uri).endswith("old.png"))
            assert old.size == os.path.getsize(world["images"] / "old.png") and old.annotations.last_modified
            templates = (await s.list_resource_templates()).resource_templates
            assert [t.uri_template for t in templates] == [res.IMAGE_TEMPLATE]
            blob = (await s.read_resource("pinaxium://images/old.png")).contents[0]
            assert blob.mime_type == "image/png"
            assert base64.b64decode(blob.blob) == (world["images"] / "old.png").read_bytes()  # full size, unchanged
            status = (await s.read_resource(res.ENGINE_URI)).contents[0].text
            assert '"status": "stopped"' in status
            with pytest.raises(Exception):
                await s.read_resource("pinaxium://images/..%2F..%2Fstate%2Fstate.json")
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
            assert "pinaxium://images/fresh.png" in [str(r.uri) for r in (await s.list_resources()).resources]
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
