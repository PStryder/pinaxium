"""Project tools over a real stdio session: list_projects, list_drafts paging, finalize_image, list_finals
(search, paging, numbered contact sheet), get_final, relink_final, delete_images, view_image by URI."""

import asyncio
import base64
import io
import json
import os
import shutil
import sys

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image

from pinaxium.config import load
from pinaxium.projects import Projects

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def world(tmp_path, monkeypatch):
    env = {"PINAXIUM_COMFY_DIR": str(tmp_path / "comfy"), "PINAXIUM_STATE_DIR": str(tmp_path / "state"),
           "PINAXIUM_FINALS_DIR": str(tmp_path / "finals"), "PINAXIUM_TRASH_DIR": str(tmp_path / "trash")}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    pj = Projects(load(create=False))
    drafts = []
    for i in range(5):
        d = pj.drafts_dir("art")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"img_{i:05d}_.png")
        Image.new("RGB", (40 + i, 30), (40 * i, 10, 10)).save(p)
        os.utime(p, (1_000_000 + i, 1_000_000 + i))
        pj.record("art", [p], {"kind": "generate", "model": "m", "prompt": f"picture number {i}", "seed": i})
        drafts.append(p)
    return {"env": env, "drafts": drafts, "projects": pj, "tmp": tmp_path}


def call(world, steps):
    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "pinaxium.server"], cwd=PROJECT,
                                       env={**os.environ, **world["env"]})
        async with stdio_client(params) as streams, ClientSession(*streams) as s:
            await s.initialize()
            return await steps(s)
    return asyncio.run(go())


def data(result):
    assert not result.is_error, result.content[0].text
    return result.structured_content


def test_projects_drafts_paging_and_finalize(world):
    async def steps(s):
        projects = data(await s.call_tool("list_projects", {}))
        assert [(p["project"], p["drafts"], p["finals"]) for p in projects["projects"]] == [("art", 5, 0)]
        page1 = data(await s.call_tool("list_drafts", {"limit": 2}))
        page2 = data(await s.call_tool("list_drafts", {"limit": 2, "offset": page1["next_offset"]}))
        last = data(await s.call_tool("list_drafts", {"limit": 2, "offset": 4}))
        assert page1["total"] == 5 and page1["next_offset"] == 2 and last["next_offset"] is None
        assert [d["prompt"] for d in page1["drafts"] + page2["drafts"] + last["drafts"]] == \
            [f"picture number {i}" for i in (4, 3, 2, 1, 0)]                        # newest first, no overlap
        first = data(await s.call_tool("finalize_image", {"image": page1["drafts"][0]["uri"], "final_id": "Cover Art",
                                                          "title": "Cover", "tags": ["book"]}))
        again = data(await s.call_tool("finalize_image", {"image": page1["drafts"][0]["uri"]}))
        assert first["uri"] == "pinaxium://projects/art/finals/cover-art" and not first["already_final"]
        assert again["uri"] == first["uri"] and again["already_final"]
        auto = data(await s.call_tool("finalize_image", {"image": world["drafts"][0]}))      # by path works too
        assert auto["id"] == "final-002"
        bad = await s.call_tool("finalize_image", {"image": page1["drafts"][1]["uri"], "final_id": "cover-art"})
        assert bad.is_error and "already taken" in bad.content[0].text
    call(world, steps)


def test_list_finals_search_paging_and_numbered_contact_sheet(world):
    pj = world["projects"]
    uris = [pj.finalize(p, title=f"Piece {i}")["uri"] for i, p in enumerate(world["drafts"])]
    missing = pj.finals("art")[2]                     # one final whose file disappears: still numbered
    shutil.move(missing["path"], world["tmp"] / "gone.png")

    async def steps(s):
        hit = data(await s.call_tool("list_finals", {"query": "piece 3"}))
        assert [f["title"] for f in hit["finals"]] == ["Piece 3"] and hit["total"] == 1
        r = await s.call_tool("list_finals", {"limit": 4, "thumbnails": True})
        page = data(r)
        assert page["total"] == 5 and page["next_offset"] == 4
        assert [f["sheet_number"] for f in page["finals"]] == [1, 2, 3, 4]
        assert [f["exists"] for f in page["finals"]] == [True, True, False, True]
        images = [c for c in r.content if c.type == "image"]
        assert len(images) == 1, "one contact sheet, not one image per final"
        sheet = Image.open(io.BytesIO(base64.b64decode(images[0].data)))
        assert sheet.width == 4 * 192                                   # four tiles: the missing one kept its slot
        no_thumbs = await s.call_tool("list_finals", {})
        assert [c.type for c in no_thumbs.content] == ["text"]
        assert all(f["sheet_number"] is None for f in data(no_thumbs)["finals"])
        return page

    page = call(world, steps)
    assert {f["uri"] for f in page["finals"]} <= set(uris)


def test_get_final_view_by_uri_and_relink(world):
    pj = world["projects"]
    f = pj.finalize(world["drafts"][1], "hero")
    moved = world["tmp"] / "desktop" / "hero copy.png"
    moved.parent.mkdir()

    async def steps(s):
        r = await s.call_tool("get_final", {"final": f["uri"], "max_side": 128})
        assert [c.type for c in r.content] == ["text", "image"] and json.loads(r.content[0].text)["id"] == "hero"
        v = await s.call_tool("view_image", {"path": f["uri"]})
        assert not v.is_error and v.content[1].type == "image"
        shutil.move(f["path"], moved)
        gone = await s.call_tool("get_final", {"final": f["uri"]})
        assert gone.is_error
        relinked = data(await s.call_tool("relink_final", {"final": f["uri"], "path": str(moved)}))
        assert relinked["path"] == os.path.realpath(moved)
        assert not (await s.call_tool("get_final", {"final": f["uri"]})).is_error
    call(world, steps)


FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_comfy.py")


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_renders_land_in_their_project_recorded_and_linked(world):
    """Through a (fake) engine: outputs go to the project's drafts folder, are recorded with their provenance,
    and come back with a draft resource link; a render that outlives max_wait_s is recorded when get_job
    collects it."""
    port = _free_port()
    world["env"].update({"PINAXIUM_PORT": str(port), "PINAXIUM_IDLE_MINUTES": "1",
                         "PINAXIUM_ENGINE_CMD": json.dumps([sys.executable, FAKE, str(port)])})
    os.makedirs(world["env"]["PINAXIUM_COMFY_DIR"], exist_ok=True)
    graph = {"9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI", "images": ["1", 0]}}}
    slow = {**graph, "5": {"class_type": "FakeDelay", "inputs": {"seconds": 4}}}

    async def steps(s):
        r = await s.call_tool("run_workflow", {"workflow": graph, "project": "Moon Base", "include_preview": False})
        assert not r.is_error, r.content[0].text
        link = next(c for c in r.content if c.type == "resource_link")
        assert str(link.uri) == "pinaxium://projects/Moon%20Base/drafts/ComfyUI_00001_.png"
        drafts = data(await s.call_tool("list_drafts", {"project": "Moon Base"}))["drafts"]
        assert [d["uri"] for d in drafts] == [str(link.uri)]
        assert "project=Moon Base" in r.content[0].text
        pending = await s.call_tool("run_workflow", {"workflow": slow, "project": "Moon Base", "max_wait_s": 1})
        job_id = pending.content[0].text.split("job_id=")[1].split(".")[0]
        await asyncio.sleep(4)
        done = await s.call_tool("get_job", {"job_id": job_id, "include_preview": False})
        assert "project=Moon Base" in done.content[0].text
        await s.call_tool("stop_engine", {})
    try:
        call(world, steps)
    finally:
        open(os.path.join(world["env"]["PINAXIUM_STATE_DIR"], "stop"), "a").close()
    manifest = world["projects"].manifest("Moon Base")["images"]
    assert set(manifest) == {"ComfyUI_00001_.png", "ComfyUI_00002_.png"}
    assert all(m["kind"] == "workflow" and m["job_id"] for m in manifest.values())
    assert not os.listdir(os.path.join(world["env"]["PINAXIUM_STATE_DIR"], "jobs"))      # pending records cleared


def test_delete_images_recycles_drafts_and_refuses_finals(world):
    pj = world["projects"]
    f = pj.finalize(world["drafts"][0], "keep")

    async def steps(s):
        out = data(await s.call_tool("delete_images", {"images": [pj.drafts("art")[0]["uri"], f["uri"]]}))
        assert len(out["deleted"]) == 1 and "never deleted" in out["not_deleted"][0]["reason"]
        both = await s.call_tool("delete_images", {"images": [f["uri"]], "project": "art"})
        assert both.is_error
        rest = data(await s.call_tool("delete_images", {"project": "art"}))
        assert len(rest["deleted"]) == 4
        assert data(await s.call_tool("list_finals", {}))["total"] == 1
    call(world, steps)
    assert len(os.listdir(world["env"]["PINAXIUM_TRASH_DIR"])) == 5 and os.path.isfile(f["path"])
