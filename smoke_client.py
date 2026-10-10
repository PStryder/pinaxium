"""End-to-end smoke test over real MCP stdio, the way Claude Desktop / Claude Code launch the server.

    uv run python smoke_client.py                    # tools, status, model list (engine stays stopped)
    uv run python smoke_client.py render [model ...] # also renders: t2i per model, an edit, a crop view
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


def show(res) -> str:
    text = ""
    for c in res.content:
        if c.type == "text":
            text += c.text
            print("   ", c.text[:700].replace("\n", "\n    "))
        elif c.type == "image":
            print(f"    [image {c.mime_type}, {len(base64.b64decode(c.data)) // 1024} KB]")
    if res.is_error:
        print("    (tool reported an error)")
    return text


async def progress(p, total, msg):
    print(f"    .. {msg}", flush=True)


async def call(s, name, args, **kw):
    t0 = time.time()
    print(f"> {name} {json.dumps({k: v for k, v in args.items() if k != 'prompt'})}")
    res = await s.call_tool(name, args, read_timeout_seconds=900, progress_callback=progress, **kw)
    text = show(res)
    print(f"    ({time.time() - t0:.1f}s)")
    return res, text


async def main(render: bool, models: list[str]) -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "pinaxium.server"], cwd=HERE)
    async with stdio_client(params) as streams, ClientSession(*streams) as s:
        await s.initialize()
        tools = await s.list_tools()
        print("tools:", ", ".join(t.name for t in tools.tools))
        await call(s, "engine_status", {})
        _, listing = await call(s, "list_models", {})
        if not render:
            return
        available = [m["id"] for m in json.loads(listing)["models"] if m["available"]]
        models = models or available
        first = None
        for mid in models:
            res, text = await call(s, "generate_image", {"prompt": PROMPT, "model": mid, "width": 1024, "height": 768, "seed": 42})
            if not res.is_error and first is None:
                first = text.split("Saved: ")[1].split(" (")[0]
        if first:
            editors = [m["id"] for m in json.loads(listing)["models"] if m["available"] and m["capabilities"].get("edit")]
            for mid in editors:
                await call(s, "edit_image", {"prompt": "Turn this into a watercolor painting", "image_paths": [first],
                                             "model": mid, "seed": 3})
            await call(s, "view_image", {"path": first, "crop": [256, 128, 512, 384]})
        await call(s, "engine_status", {})


if __name__ == "__main__":
    args = sys.argv[1:]
    asyncio.run(main("render" in args, [a for a in args if a != "render"]))
