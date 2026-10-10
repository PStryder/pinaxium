"""End-to-end smoke test over real MCP stdio, the way Claude Desktop / Claude Code launch the server.

    uv run python smoke_client.py                    # tools, status, model list (engine stays stopped)
    uv run python smoke_client.py render [model ...] # real renders through the whole project workflow:
        generate per model -> finalize -> list_finals (thumbnails) -> get_final -> derive_from_final ->
        edit_image with a final as reference -> view_image crop -> delete the drafts

Renders go to the project "smoke-test". Set PINAXIUM_FINALS_DIR to keep test finals out of your real one.
"""

import asyncio
import base64
import json
import os
import sys
import time

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPT = 'A cozy bookshop window at dusk, warm light, a hand-painted sign reading "PINAXIUM BOOKS", rain on the glass'
PROJECT = "smoke-test"


def show(res) -> str:
    text = ""
    for c in res.content:
        if c.type == "text":
            text += c.text
            print("   ", c.text[:600].replace("\n", "\n    "))
        elif c.type == "image":
            print(f"    [image {c.mime_type}, {len(base64.b64decode(c.data)) // 1024} KB]")
        elif c.type == "resource_link":
            print(f"    [resource link {c.uri}]")
    if res.is_error:
        print("    (tool reported an error)")
    return text


async def progress(p, total, msg):
    print(f"    .. {msg}", flush=True)


async def call(s, name, args):
    t0 = time.time()
    print(f"> {name} {json.dumps({k: v for k, v in args.items() if k not in ('prompt', 'instruction')})}")
    res = await s.call_tool(name, args, read_timeout_seconds=900, progress_callback=progress)
    show(res)
    print(f"    ({time.time() - t0:.1f}s)")
    if res.is_error:
        raise SystemExit(f"{name} failed")
    return res


def link(res) -> str:
    return next(c.uri for c in res.content if c.type == "resource_link")


async def main(render: bool, models: list[str]) -> None:
    # the SDK passes the server only a minimal environment by default: hand over ours (PINAXIUM_* overrides)
    params = StdioServerParameters(command=sys.executable, args=["-m", "pinaxium.server"], cwd=HERE,
                                   env=dict(os.environ))
    async with stdio_client(params) as streams, ClientSession(*streams) as s:
        await s.initialize()
        print("tools:", ", ".join(t.name for t in (await s.list_tools()).tools))
        await call(s, "engine_status", {})
        listing = (await call(s, "list_models", {})).structured_content
        if not render:
            return
        usable = [m for m in listing["models"] if m["available"]]
        editors = [m["id"] for m in usable if m["capabilities"]["edit"]]
        drafts = []
        for mid in models or [m["id"] for m in usable]:
            r = await call(s, "generate_image", {"prompt": PROMPT, "model": mid, "width": 1024, "height": 768,
                                                 "seed": 42, "project": PROJECT})
            drafts.append(link(r))
        final = (await call(s, "finalize_image", {"image": drafts[0], "title": "Bookshop",
                                                  "tags": ["smoke"]})).structured_content
        await call(s, "list_finals", {"project": PROJECT, "thumbnails": True})
        await call(s, "get_final", {"final": final["uri"], "max_side": 512})
        if editors:
            await call(s, "derive_from_final", {"final": final["uri"], "model": editors[0], "seed": 3,
                                                "instruction": "The same shop window in snowy winter"})
            await call(s, "edit_image", {"prompt": "Turn image 1 into a watercolor painting", "model": editors[0],
                                         "image_paths": [final["uri"]], "seed": 4, "project": PROJECT})
        await call(s, "view_image", {"path": final["uri"], "crop": [256, 128, 512, 384]})
        await call(s, "list_drafts", {"project": PROJECT, "limit": 5})
        await call(s, "delete_images", {"project": PROJECT})
        await call(s, "list_projects", {})


if __name__ == "__main__":
    args = sys.argv[1:]
    asyncio.run(main("render" in args, [a for a in args if a != "render"]))
