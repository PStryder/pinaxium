"""MCP server: local image generation through a shared, on-demand ComfyUI engine.

Models are discovered by scanning ComfyUI's model folders (registry.py).

    F:/HexyLab/pinaxium/.venv/Scripts/python.exe -m pinaxium.server      (stdio)
"""

# no `from __future__ import annotations`: the SDK reads the Annotated[..., Field(...)] parameter metadata
import asyncio
import inspect
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


INSTRUCTIONS = """\
Pinaxium generates and edits images locally on this machine's GPU through ComfyUI. Nothing leaves the machine \
(except via run_workflow, see below).

Workflow:
1. list_models: pick a model by its capabilities and notes (speed, lettering quality, editing, transparency). \
Omit `model` to use the default.
2. generate_image (text to image) or edit_image (change an image, or compose from several reference images; \
only models with capabilities.edit).
3. Check the result before presenting it: the reply carries the saved path, a pinaxium:// resource link to the \
full-size PNG and a preview. Use view_image with crop to inspect lettering, hands or faces at full resolution.
4. Iterate: the reply reports the seed; reuse it with a changed prompt for controlled variations, or omit it \
for new ones.

Prompting: detailed natural-language descriptions work best (subject, setting, style, lighting, composition). \
Put text that must appear in the image in quotes. negative_prompt only matters when cfg_scale > 1; the \
distilled/turbo models run at 1.

Timing: the engine is stopped while idle. The first request starts it (about 10-20 s) and loads the model; \
switching models reloads. Renders run one at a time, shared with other clients. Time grows with pixel count \
and steps: prefer 1024x1024-class sizes and turbo/distilled models for drafts. If a render outlives \
max_wait_s you get a job_id: collect it with get_job.

When something fails: tool errors say what to fix. engine_status reports setup_problems (and the config \
file) when the engine can't start; the pinaxium://logs/comfyui resource shows ComfyUI's own errors.

Engine control: stop_engine frees all GPU memory now (it also ends other clients' renders); free_vram unloads \
models but keeps the engine; it stops by itself after the configured idle time.

run_workflow runs an arbitrary ComfyUI graph and is open-world: partner API nodes call paid external \
services and custom nodes can run any code. Use it only when the built-in tools can't do the job and the \
user wants it.
"""

mcp = PinaxiumServer(
    name="pinaxium",
    title="Pinaxium: local image generation (ComfyUI)",
    version=__version__,
    lifespan=lifespan,
    middleware=[notifier.middleware],
    instructions=INSTRUCTIONS,
)


def _description(doc: str) -> str:
    """Docstring -> tool description: dedented, wrapped lines joined into paragraphs (clients show the text
    verbatim, source indentation and line breaks included)."""
    paragraphs = inspect.cleandoc(doc or "").split("\n\n")
    return "\n\n".join(" ".join(line.strip() for line in p.splitlines()) for p in paragraphs)


def tool(**kwargs):
    def deco(fn):
        return mcp.tool(description=_description(fn.__doc__), **kwargs)(fn)
    return deco
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

@tool(title="Engine status", annotations=_hints(read_only=True, idempotent=True))
async def engine_status() -> EngineStatus:
    """Report whether the engine is stopped, starting, ready to render, or external (a ComfyUI this server
    didn't start, which it uses but never stops), plus GPU memory, whether models are loaded, the queue,
    idle time, the config file in use and, if the engine can't start, setup_problems saying how to fix them.
    Never starts the engine."""
    return EngineStatus.model_validate(await engine.status())


@tool(title="Start engine", annotations=_hints(read_only=False, idempotent=True))
async def start_engine(ctx: Context) -> EngineStatus:
    """Start the engine now (otherwise it starts on the first render) and wait until it is ready to render.
    Models load into VRAM on the first render, not here. Does nothing if it is already running."""
    await engine.ensure_ready(_progress(ctx))
    return EngineStatus.model_validate(await engine.status())


@tool(title="Stop engine", annotations=_hints(read_only=False, destructive=True, idempotent=True),
          structured_output=False)
async def stop_engine() -> str:
    """Shut the engine down now and release all GPU memory. It restarts on the next request.
    A running render and queued jobs are lost, including other clients' (the engine is shared).
    A ComfyUI this server didn't start is left running."""
    if engine.request_stop():
        return "Stop requested; the engine will exit within a few seconds."
    if await engine.api_up():
        return "The ComfyUI on the port was not started by this service, so it was left running."
    return "The engine is already stopped."


@tool(title="Free GPU memory", annotations=_hints(read_only=False, idempotent=True), structured_output=False)
async def free_vram() -> str:
    """Unload models from GPU and system memory (and clear ComfyUI's node cache) but keep the engine
    running; the next render reloads them."""
    if not await engine.api_up():
        return "The engine is stopped; nothing is loaded."
    await engine.free_vram()
    return "Models unloaded."


# ---------- models and rendering ----------

@tool(title="List image models", annotations=_hints(read_only=True, idempotent=True))
async def list_models() -> ModelList:
    """List the image models found in ComfyUI's model folders, default model first: each one's id (pass it
    as `model`), family, capabilities (editing, reference-image limit, transparency, lettering quality,
    largest size), default steps/cfg, notes comparing it with the others, files and license; models that
    can't be used yet and what they are missing; and other files found (LoRAs, unsupported architectures).
    New model files are picked up automatically. Works while the engine is stopped."""
    return ModelList.model_validate(registry.summary())


@tool(title="Generate image", annotations=_hints(read_only=False, idempotent=False), structured_output=False)
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
    """Generate an image from a text prompt. Waits for the render (starting the engine and loading the model
    first if needed) up to max_wait_s.

    Reply, in order: a text block with the saved file's path, size and mode, the settings used (model, steps,
    seed, cfg), the render time and the job_id; a resource link to the full-size PNG (pinaxium://images/...);
    and a preview (max 1024 px) unless include_preview is false. If the render is still running at
    max_wait_s, the reply is just the job_id for get_job."""
    m = registry.get(model)
    _check_transparency(m, transparent)
    p = wf.resolve(m.defaults, steps, seed, cfg_scale)
    graph = wf.BUILDERS[m.builder].text_to_image(m.files, prompt, negative_prompt, width, height, p, transparent)
    return await _run(graph, f"model={m.id} steps={p.steps} seed={p.seed} cfg={p.cfg}", max_wait_s, ctx, include_preview)


@tool(title="Edit image", annotations=_hints(read_only=False, idempotent=False), structured_output=False)
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
    """Edit an image, or compose a new one from several reference images, following an instruction (refer to
    them as image 1, image 2, ... in the order given). Only models whose capabilities.edit is true can do
    this, with at most their max_reference_images.

    References can be any image file ComfyUI reads (PNG, JPEG, WebP) and are copied into ComfyUI's input
    folder. The output has the first reference's aspect ratio at about resolution x resolution pixels. The
    reply is the same as generate_image's."""
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


@tool(title="Run ComfyUI workflow",
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
    ControlNet, unsupported architectures). The graph is ComfyUI's API format, as written by ComfyUI's
    "Export (API)". Returns every image the workflow saved or previewed, like generate_image.

    Unlike the other tools this is open-world and destructive: ComfyUI's bundled partner API nodes call
    external paid services (OpenAI, Google, Anthropic and others), installed custom nodes can run arbitrary
    code with network access, and nodes can write or overwrite files. The workflow runs exactly as given:
    use it only when the built-in tools can't do the job."""
    if not workflow or not all(isinstance(v, dict) and "class_type" in v for v in workflow.values()):
        raise ValueError('workflow must be API format: {"1": {"class_type": "...", "inputs": {...}}, ...}')
    return await _run(workflow, "workflow=custom", max_wait_s, ctx, include_preview)


@tool(title="Get render result", annotations=_hints(read_only=True, idempotent=True), structured_output=False)
async def get_job(
    job_id: Annotated[str, Field(description="The job_id returned by a render that outlived max_wait_s.",
                                 min_length=1)],
    include_preview: IncludePreview = True,
) -> list:
    """Check a render that outlived max_wait_s. Its state is queued, running, done (the reply then is the same
    as generate_image's), error (with ComfyUI's message) or unknown. Job state lives in the engine: once the
    engine has stopped, find the result with list_images instead."""
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


@tool(title="Cancel render", annotations=_hints(read_only=False, destructive=True, idempotent=True),
          structured_output=False)
async def cancel_render() -> str:
    """Interrupt the render that is currently running; its result is lost. Queued jobs stay queued."""
    if not await engine.api_up():
        return "The engine is stopped; nothing is running."
    await engine.interrupt()
    return "Interrupted the current render."


# ---------- images ----------

@tool(title="List saved images", annotations=_hints(read_only=True, idempotent=True))
async def list_images(
    limit: Annotated[int, Field(description="How many to return.", ge=1, le=200)] = 20,
) -> ImageList:
    """List the most recent images saved by this server (its output folder, `<base_dir>/output/pinaxium`),
    newest first, with path, modification time and size. Works while the engine is stopped; each image is
    also readable at full size as the resource pinaxium://images/<file name>."""
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


@tool(title="View image", annotations=_hints(read_only=True, idempotent=True), structured_output=False)
async def view_image(
    path: Annotated[str, Field(description="Path of a local image file, e.g. from a render or list_images.",
                               min_length=1)],
    crop: Annotated[list[int] | None, Field(description="Optional [x, y, width, height] in pixels of the "
                                                        "full-size image, to inspect a detail (text, hands, faces) "
                                                        "at full resolution.", min_length=4, max_length=4)] = None,
    max_side: Annotated[int, Field(description="Longest side of the returned image in pixels.",
                                   ge=64, le=VIEW_MAX_SIDE)] = VIEW_MAX_SIDE,
) -> list:
    """Look at a local image, e.g. a result, to check it before showing the user. Returns a text block with its
    path, full size and mode, then the image itself (JPEG, or PNG if it has transparency), downscaled to
    max_side. With crop, only that region is returned, at full resolution when it fits in max_side. Reads any
    image file this server's process can read, not only its own results."""
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
