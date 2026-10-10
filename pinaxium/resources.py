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

from mcp.shared.subscriptions import ResourcesListChanged, ResourceUpdated
from mcp_types import Annotations, EmptyResult, Resource, ResourceLink, SubscribeRequestParams, UnsubscribeRequestParams

from pinaxium.config import Config
from pinaxium.engine import Engine
from pinaxium.projects import ProjectError, Projects, draft_uri, final_uri, project_uri

log = logging.getLogger(__name__)

POLL_S = 2.0
LIST_LIMIT = 500                    # newest images listed by resources/list
LOG_TAIL_BYTES = 256 * 1024

PROJECT_TEMPLATE = "pinaxium://projects/{project}"                      # manifest: drafts + finals, provenance
DRAFT_TEMPLATE = "pinaxium://projects/{project}/drafts/{filename}"      # a draft, full size
FINAL_TEMPLATE = "pinaxium://projects/{project}/finals/{final_id}"      # a final, by its permanent id
ENGINE_URI = "pinaxium://engine/status"
LOG_URIS = {"pinaxium://logs/comfyui": "comfyui.log", "pinaxium://logs/supervisor": "supervisor.log"}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# No numeric fields (size, priority) in resource entries or links: valid MCP, but the Rust SDK used by Codex /
# ChatGPT desktop (rmcp) fails to parse numbers inside them and drops the whole result ("Unexpected response
# type"). Order already says what matters most; sizes are in the listing tools.

def image_resources(projects: Projects) -> list[Resource]:
    """Finals first (they matter most), then the newest drafts, LIST_LIMIT in all; then one manifest per
    project."""
    out = []
    for f in projects.finals():
        if not f["exists"] or len(out) >= LIST_LIMIT:
            continue
        st = os.stat(f["path"])
        label = f["title"] or f["id"]
        out.append(Resource(uri=f["uri"], name=f["id"], title=f"Final {label} ({f['project']})", mime_type="image/png",
                            description=(f["description"] or "Finished image")[:200],
                            annotations=Annotations(last_modified=_iso(st.st_mtime), audience=["user", "assistant"])))
    for d in projects.drafts():
        if len(out) >= LIST_LIMIT:
            break
        st = os.stat(d["path"])
        name = os.path.basename(d["path"])
        out.append(Resource(uri=d["uri"], name=name, title=f"Draft {name} ({d['project']})", mime_type="image/png",
                            description="Draft" + (f": {d['prompt'][:160]}" if d.get("prompt") else ""),
                            annotations=Annotations(last_modified=_iso(st.st_mtime), audience=["user", "assistant"])))
    for p in projects.list_projects():
        out.append(Resource(uri=p["uri"], name=f"project-{p['project']}", title=f"Project {p['project']}",
                            mime_type="application/json",
                            description=f"{p['drafts']} drafts, {p['finals']} finals, with what made each"))
    return out


def project_document(projects: Projects, project: str) -> str:
    """The project resource: its drafts (with provenance) and finals (with ids, metadata, provenance)."""
    return json.dumps({"project": project, "uri": project_uri(project),
                       "drafts": projects.manifest(project)["images"],
                       "finals": {f["id"]: f for f in projects.finals(project)}}, indent=1)


def resource_link(projects: Projects, path: str) -> ResourceLink | None:
    """A link to the full-size image for tool results, when the file is one of Pinaxium's project images."""
    try:
        kind, project, real, fid = projects.locate(path)
    except ProjectError:
        return None
    name = os.path.basename(real)
    uri = final_uri(project, fid) if kind == "final" else draft_uri(project, name)
    return ResourceLink(uri=uri, name=fid or name, title=f"{kind.title()} {fid or name} ({project})",
                        mime_type="image/png", description="Full-size image (read it as a resource)")


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

    SWEEP_EVERY_S = 3600.0

    def __init__(self, cfg: Config, engine: Engine, projects: Projects):
        self.cfg, self.engine, self.projects = cfg, engine, projects
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
        drafts = [(d["path"], os.stat(d["path"]).st_mtime_ns) for d in self.projects.drafts()[:LIST_LIMIT]]
        finals = [(f["uri"], f["exists"]) for f in self.projects.finals()]
        return tuple(drafts + finals)

    def _manifest_signatures(self) -> dict:
        """Per project: its manifest's and finals index's modification times (the project resource's content)."""
        out = {}
        for name in self.projects.project_names():
            sig = []
            for path in (self.projects._manifest_path(name), self.projects._index_path(name)):
                try:
                    sig.append(os.stat(path).st_mtime_ns)
                except OSError:
                    sig.append(None)
            out[name] = tuple(sig)
        return out

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
        manifests = self._manifest_signatures()
        eng = await self._engine_signature()
        logs = {uri: self._log_signature(f) for uri, f in LOG_URIS.items()}
        last_sweep = 0.0
        while True:
            await asyncio.sleep(POLL_S)
            try:
                if self.cfg.draft_retention_days > 0 and time.time() - last_sweep > self.SWEEP_EVERY_S:
                    last_sweep = time.time()
                    swept = self.projects.sweep(self.cfg.draft_retention_days)
                    if swept:
                        log.info("retention: sent %d old drafts to the recycle bin", len(swept))
                now = self._images_signature()
                if now != images:
                    images = now
                    await self.list_changed()
                now_manifests = self._manifest_signatures()
                for project, sig in now_manifests.items():
                    if manifests.get(project) != sig:
                        await self.updated(project_uri(project))
                manifests = now_manifests
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
