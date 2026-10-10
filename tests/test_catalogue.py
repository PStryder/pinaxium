"""The tool catalogue as a client sees it (real stdio session): every tool fully described, every parameter
documented, ranges declared, all four annotation hints explicit, JSON tools typed with an outputSchema."""

import asyncio
import os
import sys

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from pinaxium import __version__

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
STRUCTURED = {"engine_status", "start_engine", "list_models", "list_images"}
RANGES = {   # (tool, param) -> (minimum, maximum) the schema must declare
    ("generate_image", "width"): (256, 2048), ("generate_image", "height"): (256, 2048),
    ("generate_image", "steps"): (1, 100), ("generate_image", "cfg_scale"): (0, 20),
    ("edit_image", "resolution"): (256, 2048), ("edit_image", "steps"): (1, 100),
    ("view_image", "max_side"): (64, 1568), ("list_images", "limit"): (1, 200),
}
READ_ONLY = {"engine_status", "list_models", "get_job", "list_images", "view_image"}
DESTRUCTIVE = {"stop_engine", "cancel_render", "run_workflow"}
OPEN_WORLD = {"run_workflow"}       # arbitrary nodes: ComfyUI's partner API nodes and custom nodes reach the network


@pytest.fixture(scope="module")
def catalogue(tmp_path_factory):
    state = tmp_path_factory.mktemp("state")

    async def fetch():
        env = {**os.environ, "PINAXIUM_STATE_DIR": str(state)}
        params = StdioServerParameters(command=sys.executable, args=["-m", "pinaxium.server"], cwd=PROJECT, env=env)
        async with stdio_client(params) as streams, ClientSession(*streams) as s:
            init = await s.initialize()
            tools = await s.list_tools()
            listed = await s.call_tool("list_images", {"limit": 3})
            return init, tools, listed
    return asyncio.run(fetch())


def schema_of(tool) -> dict:
    return tool.model_dump(by_alias=True, exclude_none=True)


def bounds(prop: dict):
    """minimum/maximum of a property, looking inside anyOf for optional (X | None) ones"""
    for p in [prop] + prop.get("anyOf", []):
        if "minimum" in p or "maximum" in p:
            return p.get("minimum"), p.get("maximum")
    return None, None


def test_server_identifies_itself(catalogue):
    init, _, _ = catalogue
    assert init.server_info.name == "pinaxium" and init.server_info.title
    assert init.server_info.version == __version__ and __version__
    assert init.instructions


def test_every_tool_has_title_description_and_all_hints(catalogue):
    _, tools, _ = catalogue
    assert len(tools.tools) == 12
    for t in tools.tools:
        d = schema_of(t)
        assert d.get("title"), f"{t.name}: no title"
        assert len(d.get("description", "")) > 30, f"{t.name}: thin description"
        ann = d.get("annotations") or {}
        for h in HINTS:
            assert h in ann, f"{t.name}: annotation {h} not set"
        assert ann["openWorldHint"] is (t.name in OPEN_WORLD), t.name
        assert ann["readOnlyHint"] == (t.name in READ_ONLY), t.name
        assert ann["destructiveHint"] == (t.name in DESTRUCTIVE), t.name


def test_every_parameter_is_described(catalogue):
    _, tools, _ = catalogue
    for t in tools.tools:
        for name, prop in schema_of(t)["inputSchema"].get("properties", {}).items():
            assert prop.get("description"), f"{t.name}.{name}: no description"


def test_ranges_are_declared(catalogue):
    _, tools, _ = catalogue
    by_name = {t.name: schema_of(t)["inputSchema"]["properties"] for t in tools.tools}
    for (tool, param), (lo, hi) in RANGES.items():
        assert bounds(by_name[tool][param]) == (lo, hi), f"{tool}.{param}: {bounds(by_name[tool][param])}"
    crop = by_name["view_image"]["crop"]
    arr = next(p for p in crop.get("anyOf", [crop]) if p.get("type") == "array")
    assert (arr.get("minItems"), arr.get("maxItems")) == (4, 4)


def test_json_tools_have_output_schemas_and_return_structured_content(catalogue):
    _, tools, listed = catalogue
    for t in tools.tools:
        has = bool(schema_of(t).get("outputSchema"))
        assert has == (t.name in STRUCTURED), f"{t.name}: outputSchema {has}"
    assert listed.structured_content is not None and "images" in listed.structured_content
