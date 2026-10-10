"""Probe a running Pinaxium service the way a third-party MCP client would, over raw HTTP (no SDK client):
initialize with every handshake protocol version, tools/list, a structured call, an image call, resources,
Origin/CORS behaviour, and whether anything started the GPU engine.

    uv run python tools_probe.py [http://127.0.0.1:8190/mcp]
"""

import json
import sys

import httpx

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190/mcp"
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc_result(resp: httpx.Response) -> dict:
    """A JSON-RPC response from either a JSON body or an SSE stream."""
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                msg = json.loads(line[5:])
                if "result" in msg or "error" in msg:
                    return msg
        raise AssertionError(f"no JSON-RPC message in SSE: {resp.text[:300]}")
    return resp.json()


def session(version: str, extra: dict | None = None) -> tuple[httpx.Client, dict, dict]:
    c = httpx.Client(timeout=30)
    h = {**ACCEPT, **(extra or {})}
    r = c.post(URL, headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "probe", "version": "1"}}})
    if r.status_code != 200:
        return c, {"status": r.status_code, "body": r.text[:200]}, h
    init = rpc_result(r)
    sid = r.headers.get("mcp-session-id")
    if sid:
        h["Mcp-Session-Id"] = sid
    h["MCP-Protocol-Version"] = init["result"]["protocolVersion"]
    c.post(URL, headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return c, init, h


def call(c, h, i, method, params=None):
    return rpc_result(c.post(URL, headers=h, json={"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}))


def main():
    base = URL.rsplit("/mcp", 1)[0]
    before = httpx.get(base + "/health").json()
    print("health:", before)
    for v in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
        c, init, h = session(v)
        r = init.get("result", {})
        print(f"initialize {v}: negotiated={r.get('protocolVersion')} server={r.get('serverInfo', {}).get('name')} "
              f"session={'yes' if 'Mcp-Session-Id' in h else 'no'} listChanged={r.get('capabilities', {}).get('resources', {}).get('listChanged')}")
        tools = call(c, h, 2, "tools/list")["result"]["tools"]
        print(f"  tools/list: {len(tools)} tools")
        c.delete(URL, headers=h)
    c, init, h = session("2025-06-18")
    drafts = call(c, h, 3, "tools/call", {"name": "list_drafts", "arguments": {"limit": 2}})["result"]
    print("structured list_drafts:", list(drafts.get("structuredContent", {}).keys()),
          "| first entry links:", {k: (drafts["structuredContent"]["drafts"][0][k] if drafts["structuredContent"]["drafts"] else None)
                                   for k in ("file_uri", "local_url")})
    if drafts["structuredContent"]["drafts"]:
        view = call(c, h, 4, "tools/call", {"name": "view_image", "arguments": {"path": drafts["structuredContent"]["drafts"][0]["uri"], "max_side": 256}})["result"]
        print("view_image content types:", [b["type"] for b in view["content"]], "| text:", view["content"][0]["text"].splitlines()[-2:])
    res = call(c, h, 5, "resources/list")["result"]["resources"]
    print("resources/list:", len(res), "resources")
    c.delete(URL, headers=h)
    for label, origin in (("no Origin", None), ("Origin http://127.0.0.1:8190", "http://127.0.0.1:8190"),
                          ("Origin https://chatgpt.com", "https://chatgpt.com"), ("Origin null", "null")):
        _, init, _ = session("2025-06-18", {"Origin": origin} if origin else None)
        print(f"{label}: {'accepted' if 'result' in init else 'refused ' + str(init.get('status'))}")
    pre = httpx.options(URL, headers={"Origin": "https://chatgpt.com", "Access-Control-Request-Method": "POST"})
    print("CORS preflight from a web origin:", pre.status_code, pre.headers.get("access-control-allow-origin"))
    after = httpx.get(base + "/health").json()
    status = httpx.get(before["engine"] + "/system_stats", timeout=2).status_code if False else None
    try:
        engine_up = httpx.get(before["engine"] + "/system_stats", timeout=2).status_code == 200
    except httpx.HTTPError:
        engine_up = False
    print("GPU engine running after all of this:", engine_up)


if __name__ == "__main__":
    main()
