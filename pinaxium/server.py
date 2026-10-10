"""MCP server: local image generation through a shared, on-demand ComfyUI engine.

Models are discovered by scanning ComfyUI's model folders (registry.py).

    F:/HexyLab/pinaxium/.venv/Scripts/python.exe -m pinaxium.server      (stdio)
"""

# no `from __future__ import annotations`: the SDK reads the Annotated[..., Field(...)] parameter metadata
import asyncio
import functools
import inspect
import io
import json
import logging
import os
import pathlib
import time
import urllib.parse
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp_types import (CallToolResult, Resource, SubscribeRequestParams, TextContent, ToolAnnotations,
                       UnsubscribeRequestParams)
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont
from pydantic import Field

from pinaxium import __version__
from pinaxium import resources as res
from pinaxium import workflows as wf
from pinaxium.config import load
from pinaxium.engine import Engine, EngineError
from pinaxium.projects import (DEFAULT_PROJECT, NAME_PATTERN, ProjectError, Projects, draft_uri, final_uri,
                               project_name)
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from pinaxium.registry import Registry
from pinaxium.schemas import (DeleteResult, DraftEntry, DraftList, EngineStatus, FinalEntry, FinalizeResult,
                              FinalList, ModelList, ProjectList)

PREVIEW_MAX_SIDE = 1024
VIEW_MAX_SIDE = 1568            # largest image side a Claude client takes without downscaling
DEFAULT_MAX_WAIT_S = 300

logging.getLogger("httpx").setLevel(logging.WARNING)      # otherwise every poll lands in the client's MCP log

cfg = load()
engine = Engine(cfg)
registry = Registry(cfg)
projects = Projects(cfg)
try:
    projects.migrate_flat_layout()         # images saved before projects existed -> the default project
except OSError:
    pass
notifier = res.Notifier(cfg, engine, projects)


class PinaxiumServer(MCPServer):
    """MCPServer lists only resources registered up front; saved images come and go, so they're added here."""

    async def list_resources(self) -> list[Resource]:
        return await super().list_resources() + res.image_resources(projects)


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
When you show the user an image, give them a link they can click: the `open:` file:/// link (opens the file \
from this machine's disk) and, when present, the `browser:` http://127.0.0.1 link (opens in any browser on this \
machine, also where file links are blocked). Listings and finalize results carry the same as file_uri and \
local_url.
4. Iterate: the reply reports the seed; reuse it with a changed prompt for controlled variations, or omit it \
for new ones.
5. Finish: finalize_image the keeper(s), then delete_images the leftover drafts.

Projects: pass `project` (e.g. "logo-redesign") to keep a piece of work together; without it, work goes to \
"scratch". Every render is a draft (pinaxium://projects/<project>/drafts/<file>) recorded with its prompt, \
model and seed. finalize_image turns a draft into a final with a permanent id and URI \
(pinaxium://projects/<project>/finals/<id>, e.g. .../finals/portrait-001) plus a title, description and tags \
for search. Refer to images by these URIs, not file paths: finals resolve through Pinaxium even after their \
files are renamed or moved. Finals are never deleted or overwritten; delete_images sends drafts to the recycle \
bin (one by one, or a whole project's).

Reuse: list_finals searches finished artwork across projects (paginated metadata; thumbnails=true adds one \
numbered contact sheet whose tile #n is the entry with sheet_number n); get_final shows one at higher \
resolution; derive_from_final starts a new draft from a final, in its project or any other; any draft or final \
URI also works as an edit_image reference. list_projects and list_drafts show the rest.

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


EXPECTED_ERRORS = (ValueError, LookupError, EngineError, OSError, httpx.HTTPError)


def tool(**kwargs):
    """Register a tool. Expected failures become ToolError: the SDK shows the model only "Error executing tool
    <name>" for any other exception, which would hide the message saying what to fix."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kw):
            try:
                return await fn(*args, **kw)
            except ToolError:
                raise
            except EXPECTED_ERRORS as e:
                raise ToolError(str(e) or type(e).__name__) from e
        return mcp.tool(description=_description(fn.__doc__), **kwargs)(wrapper)
    return deco


def resource(*args, **kwargs):
    """Register a resource; a bad or unknown URI is reported as such (ResourceNotFoundError) with its reason."""
    errors = (ValueError, LookupError, OSError)

    def deco(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*a, **kw):
                try:
                    return await fn(*a, **kw)
                except errors as e:
                    raise ResourceNotFoundError(str(e)) from e
        else:
            @functools.wraps(fn)
            def wrapper(*a, **kw):
                try:
                    return fn(*a, **kw)
                except errors as e:
                    raise ResourceNotFoundError(str(e)) from e
        return mcp.resource(*args, **kwargs)(wrapper)
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
ProjectName = Annotated[str, Field(description="Project to keep this work in (created on first use). Drafts of one "
                                               "piece of work belong together so they can be finalized and cleaned "
                                               "up as a set; see list_projects.",
                                   pattern=NAME_PATTERN, max_length=64)]


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


# ---------- links the user can click ----------

SERVICE_MODE = False                 # set by pinaxium.service: this process is the service, so local_url works
_service_seen = {"at": 0.0, "up": False}


def _service_up() -> bool:
    """Is the always-on Pinaxium service answering (it serves the local_url links)? Cached for 10 s."""
    if SERVICE_MODE:
        return True
    if time.time() - _service_seen["at"] > 10:
        try:
            r = httpx.get(cfg.service_url + "/health", timeout=0.5)
            _service_seen["up"] = r.status_code == 200 and r.json().get("service") == "pinaxium"
        except Exception:
            _service_seen["up"] = False
        _service_seen["at"] = time.time()
    return _service_seen["up"]


def file_uri(path: str) -> str:
    return pathlib.Path(path).resolve().as_uri()


def local_url(kind: str, project: str, key: str) -> str | None:
    """http://127.0.0.1:<service_port>/files/<project>/<drafts|finals>/<file or id>, or None if not served."""
    if not _service_up():
        return None
    folder = "finals" if kind == "final" else "drafts"
    return f"{cfg.service_url}/files/{urllib.parse.quote(project)}/{folder}/{urllib.parse.quote(key)}"


def links(path: str) -> dict:
    """file_uri and local_url for a draft or final file (local_url only for Pinaxium's own images)."""
    out = {"file_uri": file_uri(path), "local_url": None}
    try:
        kind, project, real, fid = projects.locate(path)
        out["local_url"] = local_url(kind, project, fid or os.path.basename(real))
    except ProjectError:
        pass
    return out


def _final_links(f: dict) -> dict:
    """Links for a final entry from projects.finals(): its current file, or none if it can't be found."""
    if not f["exists"]:
        return {"file_uri": None, "local_url": None}
    return {"file_uri": file_uri(f["path"]), "local_url": local_url("final", f["project"], f["id"])}


def _link_lines(path: str) -> str:
    lk = links(path)
    return f"\nopen: {lk['file_uri']}" + (f"\nbrowser: {lk['local_url']}" if lk["local_url"] else "")


def _check_transparency(m, transparent: bool) -> None:
    if transparent and not m.capabilities.get("transparency"):
        raise ValueError(f"model {m.id} can't produce transparent images; pick one with capabilities.transparency")


def _pending_path(job_id: str) -> str:
    return cfg.state_path(os.path.join("jobs", f"{job_id}.json"))


def _remember_job(job_id: str, project: str, meta: dict) -> None:
    """What a render was, kept until its images are recorded in the project (it may finish after this tool
    call returns, and be collected by get_job from another client)."""
    os.makedirs(os.path.dirname(_pending_path(job_id)), exist_ok=True)
    with open(_pending_path(job_id), "w", encoding="utf-8") as f:
        json.dump({"project": project, "meta": meta}, f)


def _record_job(job_id: str, images: list[str]) -> str | None:
    """Record a finished job's images in its project; returns the project (None if unknown)."""
    try:
        with open(_pending_path(job_id), encoding="utf-8") as f:
            pending = json.load(f)
    except (OSError, ValueError):
        return None
    projects.record(pending["project"], images, {**pending["meta"], "job_id": job_id})
    try:
        os.remove(_pending_path(job_id))
    except OSError:
        pass
    return pending["project"]


async def _run(graph: dict, head: str, max_wait_s: float, ctx: Context, include_preview: bool,
               project: str, meta: dict) -> list:
    graph = wf.into_folder(graph, projects.output_prefix(project))
    report = _progress(ctx)
    origin = await engine.ensure_ready(report)
    t0 = time.time()
    job_id = await engine.submit(graph)
    _remember_job(job_id, project, meta)
    await report("queued")
    j = await engine.wait(job_id, max_wait_s, report)
    head = f"{head} project={project} engine={origin} time={time.time() - t0:.1f}s"
    if j["state"] == "error":
        raise EngineError(f"render failed: {j['error']}")
    if j["state"] != "done":
        return [f"Still {j['state']} after {max_wait_s:.0f}s. job_id={job_id}. "
                f"Call get_job with this id to collect the result. ({head})"]
    _record_job(job_id, j["images"])
    if not j["images"]:
        return [f"Finished, but the workflow saved no images. job_id={job_id} ({head})"]
    out: list = ["Saved: " + "\n".join(_describe(x) + _link_lines(x) for x in j["images"])
                 + f"\n{head}\njob_id={job_id}"]
    return out + _results(j["images"], include_preview)


def _results(paths: list[str], include_preview: bool) -> list:
    """Per image: a resource link to the full-size file (when it is one of the exposed images), then the preview."""
    out: list = []
    for x in paths:
        link = res.resource_link(projects, x)
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
    project: ProjectName = DEFAULT_PROJECT,
    include_preview: IncludePreview = True,
    max_wait_s: MaxWait = DEFAULT_MAX_WAIT_S,
) -> list:
    """Generate an image from a text prompt. Waits for the render (starting the engine and loading the model
    first if needed) up to max_wait_s. The image is saved as a draft in `project`, recorded with its prompt,
    model and settings.

    Reply, in order: a text block with the saved file's path, size and mode, the settings used (model, steps,
    seed, cfg), the project, the render time and the job_id; a resource link to the full-size PNG
    (pinaxium://images/<project>/<file>); and a preview (max 1024 px) unless include_preview is false. If the
    render is still running at max_wait_s, the reply is just the job_id for get_job."""
    project = project_name(project)
    m = registry.get(model)
    _check_transparency(m, transparent)
    p = wf.resolve(m.defaults, steps, seed, cfg_scale)
    graph = wf.BUILDERS[m.builder].text_to_image(m.files, prompt, negative_prompt, width, height, p, transparent)
    meta = {"kind": "generate", "model": m.id, "prompt": prompt, "negative_prompt": negative_prompt,
            "seed": p.seed, "steps": p.steps, "cfg": p.cfg, "width": wf.snap(width), "height": wf.snap(height),
            "transparent": transparent}
    return await _run(graph, f"model={m.id} steps={p.steps} seed={p.seed} cfg={p.cfg}", max_wait_s, ctx,
                      include_preview, project, meta)


@tool(title="Edit image", annotations=_hints(read_only=False, idempotent=False), structured_output=False)
async def edit_image(
    prompt: Annotated[str, Field(description="The instruction, e.g. \"make it night time\" or \"put the person "
                                             "from image 1 in the jacket from image 2\".", min_length=1)],
    image_paths: Annotated[list[str], Field(description="Reference images, as absolute paths or pinaxium:// image "
                                                        "URIs (1 to the model's max_reference_images); the first "
                                                        "sets the output's aspect ratio.", min_length=1,
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
    project: ProjectName = DEFAULT_PROJECT,
    include_preview: IncludePreview = True,
    max_wait_s: MaxWait = DEFAULT_MAX_WAIT_S,
) -> list:
    """Edit an image, or compose a new one from several reference images, following an instruction (refer to
    them as image 1, image 2, ... in the order given). Only models whose capabilities.edit is true can do
    this, with at most their max_reference_images.

    References can be any image file ComfyUI reads (PNG, JPEG, WebP), or pinaxium:// image URIs from earlier
    results; they're copied into the project's uploads folder. The output has the first reference's aspect
    ratio at about resolution x resolution pixels, is saved as a draft in `project`, and the reply is the same
    as generate_image's."""
    return await _edit(prompt, image_paths, ctx, project_name(project), model, negative_prompt, resolution, steps,
                       seed, cfg_scale, transparent, include_preview, max_wait_s, {"kind": "edit"})


async def _edit(prompt: str, refs: list[str], ctx: Context, project: str, model: str | None, negative_prompt: str,
                resolution: int, steps: int | None, seed: int | None, cfg_scale: float | None, transparent: bool,
                include_preview: bool, max_wait_s: float, extra_meta: dict) -> list:
    """Shared by edit_image and derive_from_final. References are paths or pinaxium:// URIs (drafts, finals);
    the provenance keeps them as given, so finals stay referenced by their permanent URIs."""
    m = registry.get(model)
    builder = wf.BUILDERS[m.builder]
    if not m.capabilities.get("edit"):
        raise ValueError(f"model {m.id} can't edit images; pick one with capabilities.edit in list_models")
    limit = m.capabilities.get("max_reference_images", wf.MAX_REFERENCE_IMAGES)
    if not 1 <= len(refs) <= limit:
        raise ValueError(f"give 1 to {limit} reference images for {m.id}")
    files = [projects.locate(x)[2] if x.startswith("pinaxium://") else x for x in refs]
    for x in files:
        if not os.path.isfile(x):
            raise ValueError(f"no such file: {x}")
    _check_transparency(m, transparent)
    p = wf.resolve(m.defaults, steps, seed, cfg_scale)
    await engine.ensure_ready(_progress(ctx))
    names = [await engine.upload(x, projects.upload_subfolder(project)) for x in files]
    graph = builder.edit(m.files, prompt, negative_prompt, names, resolution, p, transparent)
    meta = {"model": m.id, "prompt": prompt, "negative_prompt": negative_prompt, "seed": p.seed, "steps": p.steps,
            "cfg": p.cfg, "resolution": wf.snap(resolution), "transparent": transparent, "sources": list(refs),
            **extra_meta}
    return await _run(graph, f"model={m.id} steps={p.steps} seed={p.seed} cfg={p.cfg}", max_wait_s, ctx,
                      include_preview, project, meta)


@tool(title="Run ComfyUI workflow",
          annotations=_hints(read_only=False, destructive=True, idempotent=False, open_world=True),
          structured_output=False)
async def run_workflow(
    workflow: Annotated[dict[str, Any], Field(description="ComfyUI API-format graph: {node_id: {\"class_type\": "
                                                          "..., \"inputs\": {...}}}. Model file names must match "
                                                          "those shown by list_models.")],
    ctx: Context,
    project: ProjectName = DEFAULT_PROJECT,
    include_preview: IncludePreview = True,
    max_wait_s: MaxWait = DEFAULT_MAX_WAIT_S,
) -> list:
    """Run an arbitrary ComfyUI workflow, for models or features the built-in tools don't cover (LoRAs,
    ControlNet, unsupported architectures). The graph is ComfyUI's API format, as written by ComfyUI's
    "Export (API)". Its SaveImage nodes are pointed at the project's drafts folder (keeping their file name
    prefixes). Returns every image the workflow saved or previewed, like generate_image.

    Unlike the other tools this is open-world and destructive: ComfyUI's bundled partner API nodes call
    external paid services (OpenAI, Google, Anthropic and others), installed custom nodes can run arbitrary
    code with network access, and nodes can write or overwrite files. The workflow runs exactly as given:
    use it only when the built-in tools can't do the job."""
    if not workflow or not all(isinstance(v, dict) and "class_type" in v for v in workflow.values()):
        raise ValueError('workflow must be API format: {"1": {"class_type": "...", "inputs": {...}}, ...}')
    project = project_name(project)
    return await _run(workflow, "workflow=custom", max_wait_s, ctx, include_preview, project,
                      {"kind": "workflow", "model": None, "prompt": None, "seed": None})


@tool(title="Get render result", annotations=_hints(read_only=False, idempotent=True), structured_output=False)
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
    project = _record_job(job_id, j["images"])
    out: list = ["Saved: " + "\n".join(_describe(x) + _link_lines(x) for x in j["images"])
                 + (f"\nproject={project}" if project else "")]
    return out + _results(j["images"], include_preview)


@tool(title="Cancel render", annotations=_hints(read_only=False, destructive=True, idempotent=True),
          structured_output=False)
async def cancel_render() -> str:
    """Interrupt the render that is currently running; its result is lost. Queued jobs stay queued."""
    if not await engine.api_up():
        return "The engine is stopped; nothing is running."
    await engine.interrupt()
    return "Interrupted the current render."


# ---------- projects, drafts and finals ----------

FinalRef = Annotated[str, Field(description="A final's URI, pinaxium://projects/<project>/finals/<id> (from "
                                            "list_finals or finalize_image).", min_length=1)]
Limit = Annotated[int, Field(description="Page size.", ge=1, le=100)]
Offset = Annotated[int, Field(description="Items to skip: pass the previous page's next_offset.", ge=0)]


def _stamp(path: str) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path)))


@tool(title="List projects", annotations=_hints(read_only=True, idempotent=True))
async def list_projects() -> ProjectList:
    """List all projects with how many drafts and finals each has, the drafts' disk use and last activity,
    plus where finals are kept and the automatic draft cleanup setting. Works while the engine is stopped."""
    return ProjectList(projects=projects.list_projects(), finals_dir=cfg.finals_dir,
                       draft_retention_days=cfg.draft_retention_days)


@tool(title="List drafts", annotations=_hints(read_only=True, idempotent=True))
async def list_drafts(
    project: Annotated[str | None, Field(description="Only this project; all projects when omitted.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> DraftList:
    """List draft images (working renders), newest first, one page at a time: URI, clickable links (file_uri,
    local_url), project, prompt, model, seed and, if it was finalized, its final's URI. Metadata only; look at
    one with view_image. Works while the engine is stopped."""
    items = projects.drafts(project_name(project) if project else None)
    page = items[offset:offset + limit]
    return DraftList(drafts=[DraftEntry(**{k: d[k] for k in ("uri", "path", "project", "final_uri", "model",
                                                                 "prompt", "seed")},
                                        **links(d["path"]),
                                        modified=_stamp(d["path"]), bytes=os.path.getsize(d["path"]))
                             for d in page],
                     total=len(items), next_offset=offset + limit if offset + limit < len(items) else None)


def _wrap(text: str, width: int) -> list[str]:
    return [text[i:i + width] for i in range(0, len(text), width)] or [""]


def _contact_sheet(finals: list[dict], cell: int = 192) -> Image | None:
    """One image of the page's finals: tile #n is finals[n-1], in the same order as the returned list (whose
    entries carry sheet_number), so a thumbnail always maps to exactly one permanent URI. Every final gets a
    tile, a placeholder if its file is missing, so numbering never shifts. Ids are drawn in full (wrapped)."""
    if not finals:
        return None
    font, badge_font = ImageFont.load_default(), ImageFont.load_default(size=18)
    cols = min(5, len(finals))
    rows = (len(finals) + cols - 1) // cols
    chars = max(8, (cell - 8) // 6)

    def label(f):                                   # project on its own line(s), then the id: never cut short
        return _wrap(f"{f['project']}/", chars) + _wrap(f["id"], chars)

    label_h = 6 + 12 * max(len(label(f)) for f in finals)
    sheet = PILImage.new("RGB", (cols * cell, rows * (cell + label_h)), (28, 28, 28))
    draw = ImageDraw.Draw(sheet)
    for i, f in enumerate(finals):
        x, y = (i % cols) * cell, (i // cols) * (cell + label_h)
        if f["exists"]:
            with PILImage.open(f["path"]) as im:
                im = im.convert("RGB")
                im.thumbnail((cell - 6, cell - 6))
            sheet.paste(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2))
        else:
            draw.rectangle((x + 3, y + 3, x + cell - 4, y + cell - 4), outline=(160, 60, 60))
            draw.text((x + 12, y + cell // 2 - 6), "file missing", fill=(220, 120, 120), font=font)
        badge = f"#{i + 1}"
        left, top, right, bottom = draw.textbbox((x + 8, y + 6), badge, font=badge_font)
        draw.rectangle((left - 5, top - 4, right + 5, bottom + 4), fill=(0, 0, 0))
        draw.text((x + 8, y + 6), badge, fill=(255, 220, 0), font=badge_font)
        for j, line in enumerate(label(f)):
            draw.text((x + 4, y + cell + 3 + 12 * j), line, fill=(235, 235, 235), font=font)
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=82)
    return Image(data=buf.getvalue(), format="jpeg")


@tool(title="List finals", annotations=_hints(read_only=True, idempotent=True))
async def list_finals(
    query: Annotated[str | None, Field(description="Words that must all appear in a final's id, title, "
                                                   "description, tags, prompt or project (case-insensitive).")] = None,
    project: Annotated[str | None, Field(description="Only this project; all projects when omitted.")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
    thumbnails: Annotated[bool, Field(description="Also return one contact-sheet image of this page: tile #n is "
                                                  "the entry with sheet_number n (labelled project/id). Small; "
                                                  "use get_final to look closely.")] = False,
) -> Annotated[CallToolResult, FinalList]:
    """Find finished artwork across projects, newest first, one page at a time: each final's permanent URI and
    id, title, description, tags, provenance (model, prompt, seed, what it was derived from). Metadata only
    unless thumbnails is true, which adds a single contact sheet of the page whose numbered tiles map one-to-one
    to the entries' sheet_number. Look at a chosen final with get_final; use it as a source with
    derive_from_final or as a reference in edit_image."""
    items = projects.finals(project_name(project) if project else None, query)
    page = items[offset:offset + limit]
    entries = [FinalEntry(**f, **_final_links(f), sheet_number=(i + 1) if thumbnails else None)
               for i, f in enumerate(page)]
    result = FinalList(finals=entries, total=len(items),
                       next_offset=offset + limit if offset + limit < len(items) else None)
    content = [TextContent(type="text", text=result.model_dump_json(indent=1))]
    if thumbnails:
        sheet = _contact_sheet(page)
        if sheet is not None:
            content.append(sheet.to_image_content())
    return CallToolResult(content=content, structured_content=result.model_dump())


@tool(title="Get final", annotations=_hints(read_only=True, idempotent=True), structured_output=False)
async def get_final(
    final: FinalRef,
    max_side: Annotated[int, Field(description="Longest side of the returned image in pixels.",
                                   ge=64, le=VIEW_MAX_SIDE)] = 1024,
) -> list:
    """Return one final: its metadata (id, URI, title, description, tags, provenance) as text, then the image
    itself downscaled to max_side. The full-size file is the final's URI as a resource."""
    f = projects.get_final(final)
    if not f["exists"]:
        raise ValueError(f"final {f['uri']} is registered but its file is gone: {f['path']}")
    meta = {**{k: v for k, v in f.items() if k != "exists"}, **_final_links(f)}
    return [json.dumps(meta, indent=1), _preview(f["path"], max_side)]


@tool(title="Finalize image", annotations=_hints(read_only=False, idempotent=True), structured_output=True)
async def finalize_image(
    image: Annotated[str, Field(description="The draft to keep: its pinaxium:// URI or file path.", min_length=1)],
    final_id: Annotated[str | None, Field(description="Permanent id within the project, e.g. 'portrait-001' "
                                                      "(lowercased; letters, digits, dashes). The next final-NNN "
                                                      "when omitted. Ids are never reused.")] = None,
    title: Annotated[str | None, Field(description="Short human title.", max_length=200)] = None,
    description: Annotated[str | None, Field(description="What it is, for search; the prompt when omitted.",
                                             max_length=4000)] = None,
    tags: Annotated[list[str] | None, Field(description="Search tags.", max_length=50)] = None,
) -> FinalizeResult:
    """Mark a draft as finished: copy it into its project's finals (finals_dir/<project>/<id>.png) under a
    permanent id and URI (pinaxium://projects/<project>/finals/<id>), with searchable title, description and
    tags and the draft's provenance. Finals are never deleted or overwritten by Pinaxium. Finalizing the same
    draft again returns its existing final."""
    r = projects.finalize(image, final_id, title, description, tags)
    return FinalizeResult(**r, file_uri=file_uri(r["path"]), local_url=local_url("final", r["project"], r["id"]))


@tool(title="Relink final", annotations=_hints(read_only=False, idempotent=True))
async def relink_final(
    final: FinalRef,
    path: Annotated[str, Field(description="Where the final's file is now.", min_length=1)],
) -> FinalizeResult:
    """Tell Pinaxium where a final's file went after it was moved outside the finals folder (moves and renames
    inside it are followed automatically). The file must be that final: it carries the final's stamp, or its
    content is unchanged. The final keeps its id and URI."""
    r = projects.relink_final(final, path)
    return FinalizeResult(**r, file_uri=file_uri(r["path"]), local_url=local_url("final", r["project"], r["id"]))


@tool(title="Derive from final", annotations=_hints(read_only=False, idempotent=False), structured_output=False)
async def derive_from_final(
    final: FinalRef,
    instruction: Annotated[str, Field(description="What to change or make from it, e.g. \"same character, "
                                                  "now in winter clothes\". Refer to the final as image 1 and any "
                                                  "extra references as image 2, 3, ...", min_length=1)],
    ctx: Context,
    project: Annotated[str | None, Field(description="Project for the new draft (any project: start a new "
                                                     "branch elsewhere); the final's own project when omitted.",
                                         pattern=NAME_PATTERN, max_length=64)] = None,
    extra_references: Annotated[list[str] | None, Field(description="More reference images (URIs or paths) "
                                                                    "after the final.", max_length=9)] = None,
    model: Annotated[str | None, Field(description="A model id with capabilities.edit; the default model when "
                                                   "omitted.")] = None,
    resolution: Annotated[int, Field(description="Output pixel budget as one side of a square, at the final's "
                                                 "aspect ratio.", ge=wf.MIN_SIDE, le=wf.MAX_SIDE)] = 1024,
    steps: Steps = None,
    seed: Seed = None,
    cfg_scale: CfgScale = None,
    include_preview: IncludePreview = True,
    max_wait_s: MaxWait = DEFAULT_MAX_WAIT_S,
) -> list:
    """Start a new branch from an existing final: an edit with the final as the source image. The result is a
    new draft (in the final's project, or any other); the final itself is never changed. Its provenance records
    the final's URI as derived_from, so lineage survives finalizing the new image too. The reply is the same as
    generate_image's."""
    f = projects.get_final(final)
    target = project_name(project or f["project"])
    return await _edit(instruction, [f["uri"], *(extra_references or [])], ctx, target, model, "", resolution,
                       steps, seed, cfg_scale, False, include_preview, max_wait_s,
                       {"kind": "derive", "derived_from": f["uri"]})


@tool(title="Delete drafts", annotations=_hints(read_only=False, destructive=True, idempotent=True))
async def delete_images(
    images: Annotated[list[str] | None, Field(description="Drafts to delete (URIs or paths).", max_length=500)] = None,
    project: Annotated[str | None, Field(description="Delete every draft and uploaded reference of this project "
                                                     "instead (its finals stay).")] = None,
) -> DeleteResult:
    """Send drafts to the OS recycle bin (restorable from there): the listed images, or all of a project's
    drafts and uploads. Give exactly one of images or project. Finals are never deleted and are reported as not
    deleted; so is anything outside Pinaxium's own folders, and any file the recycle bin refuses (there is no
    permanent delete). Finalize what you want to keep first."""
    return DeleteResult(**projects.delete(images or None, project_name(project) if project else None))


# ---------- viewing ----------

@tool(title="View image", annotations=_hints(read_only=True, idempotent=True), structured_output=False)
async def view_image(
    path: Annotated[str, Field(description="A local image file path, or a pinaxium:// draft or final URI.",
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
    if path.startswith("pinaxium://"):
        path = projects.locate(path)[2]
    if not os.path.isfile(path):
        raise ValueError(f"no such file: {path}")
    shown = _preview(path, max_side, crop)
    note = _describe(path) + (f", showing crop {crop}" if crop else "") + _link_lines(path)
    return [note, shown]


# ---------- resources ----------

def _image_bytes(uri: str) -> bytes:
    with open(projects.locate(uri)[2], "rb") as f:
        return f.read()


@resource(res.DRAFT_TEMPLATE, name="draft", title="Draft (full size)", mime_type="image/png",
              description="A draft image, full size, by project and file name (see list_drafts).")
def draft_resource(project: str, filename: str) -> bytes:
    return _image_bytes(draft_uri(project, filename))


@resource(res.FINAL_TEMPLATE, name="final", title="Final (full size)", mime_type="image/png",
              description="A final image, full size, by project and permanent id (see list_finals).")
def final_resource(project: str, final_id: str) -> bytes:
    return _image_bytes(final_uri(project, final_id))


@resource(res.PROJECT_TEMPLATE, name="project", title="Project", mime_type="application/json",
              description="A project's drafts (with what made each) and finals (ids, metadata, provenance). "
                          "Subscribe to hear when it changes.")
def project_resource(project: str) -> str:
    return res.project_document(projects, project_name(project))


@resource(res.ENGINE_URI, name="engine-status", title="Engine status", mime_type="application/json",
              description="Same as the engine_status tool. Subscribe to be notified when the engine starts, "
                          "stops, gets busy or loads models.")
async def engine_status_resource() -> str:
    return res.engine_status_json(await engine.status())


@resource("pinaxium://logs/comfyui", name="comfyui-log", title="ComfyUI log", mime_type="text/plain",
              description="Tail of the engine's log (last 256 KB): node errors, model loading, timings.")
def comfyui_log() -> str:
    return res.log_tail(cfg, res.LOG_URIS["pinaxium://logs/comfyui"])


@resource("pinaxium://logs/supervisor", name="supervisor-log", title="Supervisor log", mime_type="text/plain",
              description="Tail of the engine supervisor's log: start, idle shutdown and spawn problems.")
def supervisor_log() -> str:
    return res.log_tail(cfg, res.LOG_URIS["pinaxium://logs/supervisor"])


# ---------- initialization options (both transports) ----------
# The SDK builds a session's initialization options with no notification options, which advertises
# resources.listChanged = false to pre-2026 clients (stdio and every HTTP session alike). Default them here.

_create_init_options = mcp._lowlevel_server.create_initialization_options


def _init_options(*args, **kwargs):
    if not args and kwargs.get("notification_options") is None:
        kwargs["notification_options"] = NotificationOptions(resources_changed=True)
    return _create_init_options(*args, **kwargs)


mcp._lowlevel_server.create_initialization_options = _init_options


# ---------- HTTP routes (served by the always-on service: python -m pinaxium.service) ----------

def _local_host_ok(request: Request) -> bool:
    """Only requests addressed to this machine by its loopback name: a page on another site that rebinds its
    DNS to 127.0.0.1 still sends its own Host header and is refused."""
    host = (request.headers.get("host") or "").lower()
    return host in {f"127.0.0.1:{cfg.service_port}", f"localhost:{cfg.service_port}"}


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def http_health(request: Request):
    return JSONResponse({"service": "pinaxium", "version": __version__, "engine": cfg.base_url})


async def _serve_image(request: Request, kind: str):
    if not _local_host_ok(request):
        return PlainTextResponse("forbidden host", status_code=403)
    project = request.path_params["project"]
    key = request.path_params["key"]
    try:
        uri = draft_uri(project, key) if kind == "draft" else final_uri(project, key)
        found_kind, _, path, _ = projects.locate(uri)          # finals resolve wherever their file now is
    except ProjectError as e:
        return PlainTextResponse(str(e), status_code=404)
    if found_kind != kind:
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})


@mcp.custom_route("/files/{project}/drafts/{key}", methods=["GET"], include_in_schema=False)
async def http_draft(request: Request):
    return await _serve_image(request, "draft")


@mcp.custom_route("/files/{project}/finals/{key}", methods=["GET"], include_in_schema=False)
async def http_final(request: Request):
    return await _serve_image(request, "final")


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
