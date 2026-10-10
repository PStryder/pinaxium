"""MCP resources: saved images (full size), engine status, ComfyUI / supervisor logs, plus change notifications.

Notifications go out both ways the SDK speaks:
- legacy clients (protocol <= 2025-11-25): `notifications/resources/list_changed` to every session seen, and
  `notifications/resources/updated` to sessions that sent `resources/subscribe` for the URI;
- 2026-07-28 clients: the same events published on the server's subscription bus (`subscriptions/listen`).
A background watcher (every POLL_S) notices new or removed images, engine state changes and log growth.
"""

import asyncio
import json
import logging
import os
import time
import urllib.parse

from mcp.shared.subscriptions import ResourcesListChanged, ResourceUpdated
from mcp_types import Annotations, EmptyResult, Resource, ResourceLink, SubscribeRequestParams, UnsubscribeRequestParams

from pinaxium import workflows as wf
from pinaxium.config import Config
from pinaxium.engine import Engine

log = logging.getLogger(__name__)

POLL_S = 2.0
LIST_LIMIT = 500                    # newest images listed by resources/list
LOG_TAIL_BYTES = 256 * 1024

IMAGE_PREFIX = "pinaxium://images/"
IMAGE_TEMPLATE = IMAGE_PREFIX + "{filename}"
ENGINE_URI = "pinaxium://engine/status"
LOG_URIS = {"pinaxium://logs/comfyui": "comfyui.log", "pinaxium://logs/supervisor": "supervisor.log"}


def images_dir(cfg: Config) -> str:
    return os.path.join(cfg.output_dir, wf.OUTPUT_SUBFOLDER)


def image_uri(filename: str) -> str:
    return IMAGE_PREFIX + urllib.parse.quote(filename)


def image_path(cfg: Config, filename: str) -> str:
    """Resolve a filename from an image URI to a file in the images folder, refusing anything else."""
    name = urllib.parse.unquote(filename)
    if not name or name != os.path.basename(name) or name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError(f"not an image name: {filename!r}")
    if not name.lower().endswith(".png"):
        raise ValueError("only the PNG images saved by this service are exposed")
    root = os.path.realpath(images_dir(cfg))
    path = os.path.realpath(os.path.join(root, name))
    if os.path.dirname(path) != root or not os.path.isfile(path):
        raise ValueError(f"no such image: {name}")
    return path


def saved_images(cfg: Config) -> list[os.DirEntry]:
    d = images_dir(cfg)
    if not os.path.isdir(d):
        return []
    files = [e for e in os.scandir(d) if e.is_file() and e.name.lower().endswith(".png")]
    files.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    return files


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def image_resources(cfg: Config) -> list[Resource]:
    out = []
    for e in saved_images(cfg)[:LIST_LIMIT]:
        st = e.stat()
        out.append(Resource(uri=image_uri(e.name), name=e.name, title=f"Image {e.name}", mime_type="image/png",
                            size=st.st_size, description="Full-size image saved by pinaxium",
                            annotations=Annotations(last_modified=_iso(st.st_mtime), audience=["user", "assistant"])))
    return out


def resource_link(cfg: Config, path: str) -> ResourceLink | None:
    """A link to the full-size image for tool results, when the file is one of the exposed images."""
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(images_dir(cfg)) or not path.lower().endswith(".png"):
        return None
    name = os.path.basename(path)
    return ResourceLink(uri=image_uri(name), name=name, title=f"Image {name}", mime_type="image/png",
                        size=os.path.getsize(path), description="Full-size image (read it as a resource)")


def log_tail(cfg: Config, filename: str) -> str:
    path = cfg.state_path(filename)
    if not os.path.isfile(path):
        return f"({filename} does not exist yet; it is written once the engine has started)"
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - LOG_TAIL_BYTES))
        text = f.read().decode("utf-8", errors="replace")
    return (f"(last {LOG_TAIL_BYTES // 1024} KB of {size // 1024} KB)\n" if size > LOG_TAIL_BYTES else "") + text


def _client_key(session):
    """The SDK builds a new ServerSession for every request, all wrapping one connection: clients are told
    apart by that connection, not by the session object."""
    return id(getattr(session, "_connection", session))


class Notifier:
    """Tracks clients and their subscriptions; the watcher turns state changes into notifications."""

    def __init__(self, cfg: Config, engine: Engine):
        self.cfg, self.engine = cfg, engine
        self.bus = None                                  # set by the server: its subscription bus
        self.clients: dict[int, object] = {}             # client key -> latest session (sends on the connection)
        self.subscriptions: dict[str, set[int]] = {}     # uri -> client keys

    # --- wiring into the SDK ---

    async def middleware(self, ctx, call_next):
        session = getattr(ctx, "session", None)
        if session is not None:
            self.clients[_client_key(session)] = session
        return await call_next(ctx)

    async def on_subscribe(self, ctx, params: SubscribeRequestParams) -> EmptyResult:
        self.clients[_client_key(ctx.session)] = ctx.session
        self.subscriptions.setdefault(str(params.uri), set()).add(_client_key(ctx.session))
        return EmptyResult()

    async def on_unsubscribe(self, ctx, params: UnsubscribeRequestParams) -> EmptyResult:
        self.subscriptions.get(str(params.uri), set()).discard(_client_key(ctx.session))
        return EmptyResult()

    # --- sending ---

    async def _each(self, keys, send) -> None:
        for k in list(keys):
            session = self.clients.get(k)
            if session is None:
                continue
            try:
                await send(session)
            except Exception:                            # closed connection: forget the client
                self.clients.pop(k, None)
                for subs in self.subscriptions.values():
                    subs.discard(k)

    async def list_changed(self) -> None:
        await self._each(self.clients, lambda s: s.send_resource_list_changed())
        if self.bus is not None:
            await self.bus.publish(ResourcesListChanged())

    async def updated(self, uri: str) -> None:
        await self._each(self.subscriptions.get(uri, set()), lambda s: s.send_resource_updated(uri))
        if self.bus is not None:
            await self.bus.publish(ResourceUpdated(uri=uri))

    # --- watching ---

    def _images_signature(self) -> tuple:
        return tuple((e.name, e.stat().st_mtime_ns) for e in saved_images(self.cfg)[:LIST_LIMIT])

    async def _engine_signature(self) -> tuple:
        s = await self.engine.status()
        return (s.get("status"), s.get("busy"), s.get("queued_jobs"), s.get("models_loaded_estimate"))

    def _log_signature(self, filename: str) -> tuple:
        try:
            st = os.stat(self.cfg.state_path(filename))
            return (st.st_size, st.st_mtime_ns)
        except OSError:
            return (None, None)

    async def watch(self) -> None:
        images = self._images_signature()
        eng = await self._engine_signature()
        logs = {uri: self._log_signature(f) for uri, f in LOG_URIS.items()}
        while True:
            await asyncio.sleep(POLL_S)
            try:
                now = self._images_signature()
                if now != images:
                    images = now
                    await self.list_changed()
                now_eng = await self._engine_signature()
                if now_eng != eng:
                    eng = now_eng
                    await self.updated(ENGINE_URI)
                for uri, f in LOG_URIS.items():
                    sig = self._log_signature(f)
                    if sig != logs[uri]:
                        logs[uri] = sig
                        await self.updated(uri)
            except Exception:
                log.exception("resource watcher iteration failed")


def engine_status_json(status: dict) -> str:
    return json.dumps(status, indent=2)
