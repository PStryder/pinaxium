"""Launch Pinaxium over STDIO exactly as an MCP client configuration would (command, args, working directory,
env), with a deliberately minimal environment, and check initialize, tools/list and a structured call.

    uv run python tools_probe_stdio.py
"""

import asyncio
import os
import sys

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
MINIMAL = {k: os.environ[k] for k in ("SYSTEMROOT", "PATH") if k in os.environ}   # what a strict client might pass


async def probe(label: str, env: dict, cwd: str | None):
    params = StdioServerParameters(command=PYTHON, args=["-m", "pinaxium.server"], cwd=cwd, env=env)
    try:
        async with stdio_client(params) as streams, ClientSession(*streams) as s:
            init = await s.initialize()
            tools = (await s.list_tools()).tools
            st = (await s.call_tool("engine_status", {})).structured_content
            print(f"{label}: OK  server={init.server_info.name} {init.server_info.version}, {len(tools)} tools, "
                  f"config={st.get('config_file')}, engine={st.get('status')}, problems={st.get('setup_problems')}")
    except Exception as e:
        print(f"{label}: FAILED  {type(e).__name__}: {e}")


async def main():
    await probe("cwd=project, minimal env", dict(MINIMAL), ROOT)
    await probe("no cwd, PYTHONPATH only", {**MINIMAL, "PYTHONPATH": ROOT}, None)
    appdata = os.environ.get("APPDATA", "")
    await probe("no cwd, PYTHONPATH + PINAXIUM_CONFIG", {**MINIMAL, "PYTHONPATH": ROOT,
                "PINAXIUM_CONFIG": os.path.join(appdata, "pinaxium", "config.toml")}, None)


asyncio.run(main())
