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
TOOLS = {"engine_status", "start_engine", "stop_engine", "free_vram", "list_models", "generate_image", "edit_image",
         "run_workflow", "get_job", "cancel_render", "list_projects", "list_drafts", "list_finals", "get_final",
         "finalize_image", "relink_final", "derive_from_final", "delete_images", "view_image"}
STRUCTURED = {"engine_status", "start_engine", "list_models", "list_projects", "list_drafts", "list_finals",
              "finalize_image", "relink_final", "delete_images"}
RANGES = {   # (tool, param) -> (minimum, maximum) the schema must declare
    ("generate_image", "width"): (256, 2048), ("generate_image", "height"): (256, 2048),
    ("generate_image", "steps"): (1, 100), ("generate_image", "cfg_scale"): (0, 20),
    ("edit_image", "resolution"): (256, 2048), ("edit_image", "steps"): (1, 100),
    ("view_image", "max_side"): (64, 1568), ("list_drafts", "limit"): (1, 100), ("list_finals", "limit"): (1, 100),
    ("get_final", "max_side"): (64, 1568), ("derive_from_final", "resolution"): (256, 2048),
}
READ_ONLY = {"engine_status", "list_models", "list_projects", "list_drafts", "list_finals", "get_final", "view_image"}
DESTRUCTIVE = {"stop_engine", "cancel_render", "run_workflow", "delete_images"}
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
            listed = await s.call_tool("list_drafts", {"limit": 3})
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
    assert init.server_info.name == "pinaxium" and init.server_info.title.startswith("Pinaxium")
    assert init.server_info.version == __version__ and __version__
    assert init.instructions


def test_every_tool_has_title_description_and_all_hints(catalogue):
    _, tools, _ = catalogue
    assert {t.name for t in tools.tools} == TOOLS
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


def test_expected_errors_reach_the_model_with_their_message(tmp_path):
    """The SDK replaces any exception but ToolError with "Error executing tool <name>"; ours must get through."""
    async def go():
        env = {**os.environ, "PINAXIUM_STATE_DIR": str(tmp_path / "s")}
        params = StdioServerParameters(command=sys.executable, args=["-m", "pinaxium.server"], cwd=PROJECT, env=env)
        async with stdio_client(params) as streams, ClientSession(*streams) as s:
            await s.initialize()
            r = await s.call_tool("generate_image", {"prompt": "x", "model": "no-such-model"})
            assert r.is_error and "unknown model 'no-such-model'" in r.content[0].text
            r = await s.call_tool("get_final", {"final": "pinaxium://projects/none/finals/nope"})
            assert r.is_error and "no such final" in r.content[0].text
            with pytest.raises(Exception, match="no such draft"):
                await s.read_resource("pinaxium://projects/none/drafts/x.png")
    asyncio.run(go())


def test_every_result_field_is_described(catalogue):
    _, tools, _ = catalogue
    for t in tools.tools:
        schema = schema_of(t).get("outputSchema")
        if not schema:
            continue
        for owner, sub in [(t.name, schema)] + list(schema.get("$defs", {}).items()):
            for name, prop in sub.get("properties", {}).items():
                assert prop.get("description"), f"{owner}.{name}: no description"


def test_descriptions_have_no_source_indentation(catalogue):
    _, tools, _ = catalogue
    for t in tools.tools:
        assert "\n " not in (t.description or ""), f"{t.name}: indented line in description"


def test_json_tools_have_output_schemas_and_return_structured_content(catalogue):
    _, tools, listed = catalogue
    for t in tools.tools:
        has = bool(schema_of(t).get("outputSchema"))
        assert has == (t.name in STRUCTURED), f"{t.name}: outputSchema {has}"
    assert listed.structured_content is not None and "drafts" in listed.structured_content
