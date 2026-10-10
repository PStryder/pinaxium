"""MCP server: local image generation through a shared, on-demand ComfyUI engine.

Models are discovered by scanning ComfyUI's model folders (registry.py).

    F:/HexyLab/pinaxium/.venv/Scripts/python.exe -m pinaxium.server      (stdio)
"""

# no `from __future__ import annotations`: the SDK reads the Annotated[..., Field(...)] parameter metadata
import asyncio
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any

import anyio
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.stdio import stdio_server
from mcp_types import Resource, SubscribeRequestParams, ToolAnnotations, UnsubscribeRequestParams
from PIL import Image as PILImage
from pydantic import Field

from pinaxium import __version__
from pinaxium import resources as res
from pinaxium import workflows as wf
from pinaxium.config import load
from pinaxium.engine import Engine, EngineError
from pinaxium.registry import Registry
from pinaxium.schemas import EngineStatus, ImageEntry, ImageList, ModelList

PREVIEW_MAX_SIDE = 1024
VIEW_MAX_SIDE = 1568            # largest image side a Claude client takes without downscaling
DEFAULT_MAX_WAIT_S = 300

logging.getLogger("httpx").setLevel(logging.WARNING)      # otherwise every poll lands in the client's MCP log

cfg = load()
engine = Engine(cfg)
registry = Registry(cfg)
notifier = res.Notifier(cfg, engine)


class PinaxiumServer(MCPServer):
    """MCPServer lists only resources registered up front; saved images come and go, so they're added here."""

    async def list_resources(self) -> list[Resource]:
        return await super().list_resources() + res.image_resources(cfg)


@asynccontextmanager
async def lifespan(_server):
    watcher = asyncio.create_task(notifier.watch())
    try:
        yield {}
    finally:
        watcher.cancel()


mcp = PinaxiumServer(
    name="pinaxium",
    title="Local image generation (ComfyUI)",
    version=__version__,
    lifespan=lifespan,
    middleware=[notifier.middleware],
    instructions=(
        "Generates and edits images locally on the user's RTX 4080 through ComfyUI. Call list_models to see "
        "the usable models (discovered automatically from the ComfyUI model folders) and pass a model id to "
        "generate_image / edit_image; without one the default is used. The engine is stopped while idle and "
        "starts on the first request (cold start: engine boot plus model load, up to a minute or two). "
        "engine_status says whether it is ready. Tools return the full-size file path, a resource link to the "
        "full-size image (pinaxium://images/...) and a preview; use view_image with crop to inspect details of a "
        "result at full resolution. Resources: saved images, pinaxium://engine/status (subscribable) and the "
        "ComfyUI log at pinaxium://logs/comfyui for diagnosing failed renders."
    ),
)
notifier.bus = mcp._subscriptions          # 2026-07-28 subscriptions/listen fan-out
# legacy resources/subscribe (protocol <= 2025-11-25); MCPServer doesn't register these itself
mcp._lowlevel_server.add_request_handler("resources/subscribe", SubscribeRequestParams, notifier.on_subscribe)
mcp._lowlevel_server.add_request_handler("resources/unsubscribe", UnsubscribeRequestParams, notifier.on_unsubscribe)


def _hints(*, read_only: bool, destructive: bool = False, idempotent: bool, open_world: bool = False) -> ToolAnnotations:
    """All four hints, explicitly: absent ones default to destructive and open-world in the spec. The built-in
    graphs only touch ComfyUI on 127.0.0.1 and files on this machine; run_workflow is the exception (see there)."""
    return ToolAnnotations(readOnlyHint=read_only, destructiveHint=destructive, idempotentHint=idempotent,
                           openWorldHint=open_world)


# ---------- shared parameter types ----------

Prompt = Annotated[str, Field(description="What to draw: a detailed natural-language description. Quoted text "
                                          "(\"...\") is rendered as lettering by models that can.", min_length=1)]
ModelId = Annotated[str | None, Field(description="A model id from list_models; the default model when omitted.")]
NegativePrompt = Annotated[str, Field(description="Things to avoid. Only has an effect when cfg_scale > 1.")]
Steps = Annotated[int | None, Field(description="Sampling steps; the model's default when omitted.", ge=1, le=100)]
Seed = Annotated[int | None, Field(description="Fix for reproducible results; random when omitted (the seed used "
                                               "is reported).", ge=0)]
CfgScale = Annotated[float | None, Field(description="Guidance scale; the model's default when omitted (1.0 for "
                                                     "distilled/turbo models).", ge=0.0, le=20.0)]
Transparent = Annotated[bool, Field(description="Keep an alpha channel (models with capabilities.transparency "
                                                "only); say 'transparent background' in the prompt. Otherwise "
                                                "results are plain RGB.")]
IncludePreview = Annotated[bool, Field(description="Return a downscaled preview (max 1024 px) of each saved image "
                                                   "as image content, besides its path.")]
MaxWait = Annotated[float, Field(description="Seconds to wait for the render; if it isn't done by then a job_id "
                                             "is returned for get_job.", ge=1, le=3600)]


def _progress(ctx: Context):
    t0 = time.time()

    async def report(msg: str) -> None:
        try:
            await ctx.report_progress(time.time() - t0, None, msg)
        except Exception:
            pass   # clients that didn't ask for progress
    return report


def _has_transparency(im: PILImage.Image) -> bool:
    if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
        return im.convert("RGBA").getchannel("A").getextrema()[0] < 250    # near-opaque alpha noise doesn't count
    return False


def _encode(im: PILImage.Image) -> Image:
    """Transparent images as PNG (keeps the alpha), everything else as JPEG (several times smaller)."""
    buf = io.BytesIO()
    if _has_transparency(im):
        im.convert("RGBA").save(buf, "PNG", optimize=True)
        return Image(data=buf.getvalue(), format="png")
    im.convert("RGB").save(buf, "JPEG", quality=90)
    return Image(data=buf.getvalue(), format="jpeg")


def _preview(path: str, max_side: int = PREVIEW_MAX_SIDE, crop: list[int] | None = None) -> Image:
    with PILImage.open(path) as im:
        im.load()
        if crop is not None:
            if len(crop) != 4:
                raise ValueError("crop is [x, y, width, height] in pixels of the full-size image")
            x, y, w, h = (int(v) for v in crop)
            x, y = max(0, x), max(0, y)
            box = (x, y, min(im.width, x + w), min(im.height, y + h))
            if box[2] <= box[0] or box[3] <= box[1]:
                raise ValueError(f"crop {crop} is outside the {im.width}x{im.height} image")
            im = im.crop(box)
        im.thumbnail((max_side, max_side))
        return _encode(im)


def _describe(path: str) -> str:
    with PILImage.open(path) as im:
        return f"{path} ({im.width}x{im.height}, {im.mode})"


def _check_transparency(m, transparent: bool) -> None:
    if transparent and not m.capabilities.get("transparency"):
        raise ValueError(f"model {m.id} can't produce transparent images; pick one with capabilities.transparency")


async def _run(graph: dict, head: str, max_wait_s: float, ctx: Context, include_preview: bool) -> list:
    report = _progress(ctx)
    origin = await engine.ensure_ready(report)
    t0 = time.time()
    job_id = await engine.submit(graph)
    await report("queued")
    j = await engine.wait(job_id, max_wait_s, report)
    head = f"{head} engine={origin} time={time.time() - t0:.1f}s"
    if j["state"] == "error":
        raise EngineError(f"render failed: {j['error']}")
    if j["state"] != "done":
        return [f"Still {j['state']} after {max_wait_s:.0f}s. job_id={job_id}. "
                f"Call get_job with this id to collect the result. ({head})"]
    if not j["images"]:
        return [f"Finished, but the workflow saved no images. job_id={job_id} ({head})"]
    out: list = [f"Saved: {', '.join(_describe(x) for x in j['images'])}\n{head}\njob_id={job_id}"]
    return out + _results(j["images"], include_preview)


def _results(paths: list[str], include_preview: bool) -> list:
    """Per image: a resource link to the full-size file (when it is one of the exposed images), then the preview."""
    out: list = []
    for x in paths:
        link = res.resource_link(cfg, x)
        if link is not None:
            out.append(link)
        if include_preview:
            out.append(_preview(x))
    return out


# ---------- engine ----------

@mcp.tool(title="Engine status", annotations=_hints(read_only=True, idempotent=True))
async def engine_status() -> EngineStatus:
    """Report whether the engine is stopped, starting or ready to render, plus GPU memory, whether models
    are loaded, queue and idle time. Does not start the engine."""
    return EngineStatus.model_validate(await engine.status())


@mcp.tool(title="Start engine", annotations=_hints(read_only=False, idempotent=True))
async def start_engine(ctx: Context) -> EngineStatus:
    """Start the engine now (otherwise it starts on the first render) and wait until it is ready to render.
    Models load into VRAM on the first render, not here. Does nothing if it is already running."""
    await engine.ensure_ready(_progress(ctx))
    return EngineStatus.model_validate(await engine.status())


@mcp.tool(title="Stop engine", annotations=_hints(read_only=False, destructive=True, idempotent=True),
          structured_output=False)
async def stop_engine() -> str:
    """Shut the engine down now and release all GPU memory. It restarts on the next request.
    A running render and queued jobs are lost."""
    if engine.request_stop():
        return "Stop requested; the engine will exit within a few seconds."
    if await engine.api_up():
        return "The ComfyUI on the port was not started by this service, so it was left running."
    return "The engine is already stopped."


@mcp.tool(title="Free GPU memory", annotations=_hints(read_only=False, idempotent=True), structured_output=False)
async def free_vram() -> str:
    """Unload models from GPU and system memory (and clear ComfyUI's node cache) but keep the engine
    running; the next render reloads them."""
    if not await engine.api_up():
        return "The engine is stopped; nothing is loaded."
    await engine.free_vram()
    return "Models unloaded."


# ---------- models and rendering ----------

@mcp.tool(title="List image models", annotations=_hints(read_only=True, idempotent=True))
async def list_models() -> ModelList:
    """List the image models found in ComfyUI's model folders: each usable model's id (pass it as `model`),
    family, capabilities, default steps/cfg and files; models that can't be used yet and why; and other
    files found (LoRAs, unsupported architectures). New files are detected automatically."""
    return ModelList.model_validate(registry.summary())


@mcp.tool(title="Generate image", annotations=_hints(read_only=False, idempotent=False), structured_output=False)
async def generate_image(
    prompt: Prompt,
    ctx: Context,
    model: ModelId = None,
    negative_prompt: NegativePrompt = "",
    width: Annotated[int, Field(description="Output width in pixels (rounded to a multiple of 16). Render time "
                                            "grows with pixel count.", ge=wf.MIN_SIDE, le=wf.MAX_SIDE)] = 1024,
    height: Annotated[int, Field(description="Output height in pixels (rounded to a multiple of 16).",
                                 ge=wf.MIN_SIDE, le=wf.MAX_SIDE)] = 1024,
    steps: Steps = None,
    seed: Seed = None,
    cfg_scale: CfgScale = None,
    transparent: Transparent = False,
    include_preview: IncludePreview = True,
    max_wait_s: MaxWait = DEFAULT_MAX_WAIT_S,
) -> list:
    """Generate an image from a text prompt. Saves the full-size image and returns its path, the settings
    used (model, steps, seed, cfg) and, by default, a preview as image content."""
    m = registry.get(model)
    _check_transparency(m, transparent)
    p = wf.resolve(m.defaults, steps, seed, cfg_scale)
    graph = wf.BUILDERS[m.builder].text_to_image(m.files, prompt, negative_prompt, width, height, p, transparent)
    return await _run(graph, f"model={m.id} steps={p.steps} seed={p.seed} cfg={p.cfg}", max_wait_s, ctx, include_preview)


@mcp.tool(title="Edit image", annotations=_hints(read_only=False, idempotent=False), structured_output=False)
async def edit_image(
    prompt: Annotated[str, Field(description="The instruction, e.g. \"make it night time\" or \"put the person "
                                             "from image 1 in the jacket from image 2\".", min_length=1)],
    image_paths: Annotated[list[str], Field(description="Absolute paths of local reference images (1 to the "
                                                        "model's max_reference_images); the first sets the "
                                                        "output's aspect ratio.", min_length=1,
                                            max_length=wf.MAX_REFERENCE_IMAGES)],
    ctx: Context,
    model: Annotated[str | None, Field(description="A model id from list_models whose capabilities.edit is "
                                                   "true; the default model when omitted.")] = None,
    negative_prompt: NegativePrompt = "",
    resolution: Annotated[int, Field(description="Output pixel budget as one side of a square (1024 means about "
                                                 "1 megapixel), at the first reference's aspect ratio.",
                                     ge=wf.MIN_SIDE, le=wf.MAX_SIDE)] = 1024,
    steps: Steps = None,
    seed: Seed = None,
    cfg_scale: CfgScale = None,
    transparent: Transparent = False,
    include_preview: IncludePreview = True,
    max_wait_s: MaxWait = DEFAULT_MAX_WAIT_S,
) -> list:
    """Edit an image, or compose a new one from several reference images, following an instruction.
    Only models whose capabilities include edit can do this. Returns like generate_image."""
    m = registry.get(model)
    builder = wf.BUILDERS[m.builder]
    if not m.capabilities.get("edit"):
        raise ValueError(f"model {m.id} can't edit images; pick one with capabilities.edit in list_models")
    limit = m.capabilities.get("max_reference_images", wf.MAX_REFERENCE_IMAGES)
    if not 1 <= len(image_paths) <= limit:
        raise ValueError(f"give 1 to {limit} image paths for {m.id}")
    for x in image_paths:
        if not os.path.isfile(x):
            raise ValueError(f"no such file: {x}")
    _check_transparency(m, transparent)
    p = wf.resolve(m.defaults, steps, seed, cfg_scale)
    await engine.ensure_ready(_progress(ctx))
    names = [await engine.upload(x) for x in image_paths]
    graph = builder.edit(m.files, prompt, negative_prompt, names, resolution, p, transparent)
    return await _run(graph, f"model={m.id} steps={p.steps} seed={p.seed} cfg={p.cfg}", max_wait_s, ctx, include_preview)


@mcp.tool(title="Run ComfyUI workflow",
          annotations=_hints(read_only=False, destructive=True, idempotent=False, open_world=True),
          structured_output=False)
async def run_workflow(
    workflow: Annotated[dict[str, Any], Field(description="ComfyUI API-format graph: {node_id: {\"class_type\": "
                                                          "..., \"inputs\": {...}}}. Model file names must match "
                                                          "those shown by list_models.")],
    ctx: Context,
    include_preview: IncludePreview = True,
    max_wait_s: MaxWait = DEFAULT_MAX_WAIT_S,
) -> list:
    """Run an arbitrary ComfyUI workflow, for models or features the built-in tools don't cover (LoRAs,
    ControlNet, unsupported architectures). Returns every image the workflow saved or previewed.
    Unlike the other tools this is open-world and destructive: ComfyUI's bundled partner API nodes call
    external paid services (OpenAI, Google, Anthropic and others), installed custom nodes can run arbitrary
    code with network access, and nodes can write or overwrite files. The workflow runs exactly as given."""
    if not workflow or not all(isinstance(v, dict) and "class_type" in v for v in workflow.values()):
        raise ValueError('workflow must be API format: {"1": {"class_type": "...", "inputs": {...}}, ...}')
    return await _run(workflow, "workflow=custom", max_wait_s, ctx, include_preview)


@mcp.tool(title="Get render result", annotations=_hints(read_only=True, idempotent=True), structured_output=False)
async def get_job(
    job_id: Annotated[str, Field(description="The job_id returned by a render that outlived max_wait_s.",
                                 min_length=1)],
    include_preview: IncludePreview = True,
) -> list:
    """Check a render that outlived max_wait_s: returns its state, and the image once done."""
    if not await engine.api_up():
        return ["The engine is stopped, so the job's state is gone. Check list_images for its output."]
    engine.touch_activity()
    j = await engine.job(job_id)
    if j["state"] == "error":
        return [f"Job {job_id} failed: {j['error']}"]
    if j["state"] != "done":
        return [f"Job {job_id} is {j['state']}."]
    out: list = [f"Saved: {', '.join(_describe(x) for x in j['images'])}"]
    return out + _results(j["images"], include_preview)


@mcp.tool(title="Cancel render", annotations=_hints(read_only=False, destructive=True, idempotent=True),
          structured_output=False)
async def cancel_render() -> str:
    """Interrupt the render that is currently running; its result is lost. Queued jobs stay queued."""
    if not await engine.api_up():
        return "The engine is stopped; nothing is running."
    await engine.interrupt()
    return "Interrupted the current render."


# ---------- images ----------

@mcp.tool(title="List saved images", annotations=_hints(read_only=True, idempotent=True))
async def list_images(
    limit: Annotated[int, Field(description="How many to return.", ge=1, le=200)] = 20,
) -> ImageList:
    """List the most recent images saved by this service, newest first (works while the engine is stopped)."""
    root = os.path.join(cfg.output_dir, wf.OUTPUT_SUBFOLDER)
    files = []
    if os.path.isdir(root):
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) and os.path.isfile(path):
                files.append(path)
    files.sort(key=os.path.getmtime, reverse=True)
    return ImageList(images=[ImageEntry(path=f, bytes=os.path.getsize(f),
                                        modified=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(f))))
                             for f in files[:limit]])


@mcp.tool(title="View image", annotations=_hints(read_only=True, idempotent=True), structured_output=False)
async def view_image(
    path: Annotated[str, Field(description="Path of a local image file, e.g. from a render or list_images.",
                               min_length=1)],
    crop: Annotated[list[int] | None, Field(description="Optional [x, y, width, height] in pixels of the "
                                                        "full-size image, to inspect a detail (text, hands, faces) "
                                                        "at full resolution.", min_length=4, max_length=4)] = None,
    max_side: Annotated[int, Field(description="Longest side of the returned image in pixels.",
                                   ge=64, le=VIEW_MAX_SIDE)] = VIEW_MAX_SIDE,
) -> list:
    """Look at a local image (e.g. a result, to check it before showing the user). Returns its path, size and
    mode, and the image itself as image content, downscaled to max_side."""
    if not os.path.isfile(path):
        raise ValueError(f"no such file: {path}")
    shown = _preview(path, max_side, crop)
    note = _describe(path) + (f", showing crop {crop}" if crop else "")
    return [note, shown]


# ---------- resources ----------

@mcp.resource(res.IMAGE_TEMPLATE, name="image", title="Saved image (full size)", mime_type="image/png",
              description="A full-size PNG saved by this service, by file name (see resources/list or list_images).")
def image_resource(filename: str) -> bytes:
    with open(res.image_path(cfg, filename), "rb") as f:
        return f.read()


@mcp.resource(res.ENGINE_URI, name="engine-status", title="Engine status", mime_type="application/json",
              description="Same as the engine_status tool. Subscribe to be notified when the engine starts, "
                          "stops, gets busy or loads models.")
async def engine_status_resource() -> str:
    return res.engine_status_json(await engine.status())


@mcp.resource("pinaxium://logs/comfyui", name="comfyui-log", title="ComfyUI log", mime_type="text/plain",
              description="Tail of the engine's log (last 256 KB): node errors, model loading, timings.")
def comfyui_log() -> str:
    return res.log_tail(cfg, res.LOG_URIS["pinaxium://logs/comfyui"])


@mcp.resource("pinaxium://logs/supervisor", name="supervisor-log", title="Supervisor log", mime_type="text/plain",
              description="Tail of the engine supervisor's log: start, idle shutdown and spawn problems.")
def supervisor_log() -> str:
    return res.log_tail(cfg, res.LOG_URIS["pinaxium://logs/supervisor"])


async def _serve_stdio() -> None:
    """MCPServer.run("stdio") advertises resources.listChanged = false to pre-2026 clients, so the
    initialization options are built here with it switched on."""
    ll = mcp._lowlevel_server
    async with stdio_server() as (read_stream, write_stream):
        await ll.run(read_stream, write_stream,
                     ll.create_initialization_options(notification_options=NotificationOptions(resources_changed=True)))


def main() -> None:
    anyio.run(_serve_stdio)


if __name__ == "__main__":
    main()
