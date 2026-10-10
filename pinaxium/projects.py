"""Projects: named folders that keep working images (drafts) apart, turn keepers into finals with permanent
identities, and make cleanup easy.

  drafts   <base_dir>/output/pinaxium/<project>/<file>.png    + project.json (what made each draft)
  uploads  <base_dir>/input/pinaxium/<project>/                reference images uploaded for edits
  finals   <finals_dir>/<project>/<final id>.png                + finals.json (the finals index)

URIs (no filesystem paths in them):
  pinaxium://projects/<project>                    the project's manifest (drafts and finals, with provenance)
  pinaxium://projects/<project>/drafts/<file>      a draft
  pinaxium://projects/<project>/finals/<final id>  a final: its id is permanent and never reused

Deleting only ever sends drafts and uploads to the OS recycle bin (send2trash); there is no permanent delete,
and finals are never deleted. If the recycle bin can't take a file (e.g. on a network drive), that file is
reported and left in place instead of being deleted permanently.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.parse
from collections.abc import Callable

from pinaxium.config import Config

SUBFOLDER = "pinaxium"
DEFAULT_PROJECT = "scratch"
MANIFEST = "project.json"
FINALS_INDEX = "finals.json"
IMAGE_EXT = ".png"
NAME_PATTERN = r"^[A-Za-z0-9]([A-Za-z0-9._ -]{0,62}[A-Za-z0-9_-])?$"
FINAL_ID_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$"
_NAME_RE, _FINAL_ID_RE = re.compile(NAME_PATTERN), re.compile(FINAL_ID_PATTERN)
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
LOCK_STALE_S = 10.0
SCHEME = "pinaxium://projects/"


class ProjectError(ValueError):
    pass


# ---------- names and URIs ----------

def project_name(name: str | None) -> str:
    """1-64 letters, digits, spaces, '.', '-', '_', starting with a letter or digit, not ending in a dot or
    space (Windows), not a Windows device name."""
    name = (name or "").strip() or DEFAULT_PROJECT          # surrounding spaces trimmed; blank = default project
    if not _NAME_RE.match(name) or name.lower().split(".")[0] in _WINDOWS_RESERVED:
        raise ProjectError(f"invalid project name {name!r}: use 1-64 letters, digits, spaces, '.', '-' or '_', "
                           "starting with a letter or digit")
    return name


def final_id(name: str) -> str:
    """A permanent final id: lowercase letters, digits and dashes (e.g. 'portrait-001'), 1-64 chars."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:64].strip("-")
    if not slug or not _FINAL_ID_RE.match(slug):
        raise ProjectError(f"can't make a final id from {name!r}: use letters, digits and dashes")
    return slug


def project_uri(project: str) -> str:
    return SCHEME + urllib.parse.quote(project)


def draft_uri(project: str, filename: str) -> str:
    return f"{project_uri(project)}/drafts/{urllib.parse.quote(filename)}"


def final_uri(project: str, fid: str) -> str:
    return f"{project_uri(project)}/finals/{fid}"


def parse_uri(uri: str) -> tuple[str, str, str | None]:
    """-> ("project", project, None) | ("draft", project, filename) | ("final", project, final id)."""
    if not uri.startswith(SCHEME):
        raise ProjectError(f"not a pinaxium project URI: {uri}")
    parts = uri[len(SCHEME):].split("/")
    project = project_name(urllib.parse.unquote(parts[0]))
    if len(parts) == 1:
        return "project", project, None
    if len(parts) == 3 and parts[1] in ("drafts", "finals") and parts[2]:
        leaf = urllib.parse.unquote(parts[2])
        if "/" in leaf or "\\" in leaf or leaf in (".", ".."):
            raise ProjectError(f"bad name in {uri}")
        if parts[1] == "finals" and not _FINAL_ID_RE.match(leaf):
            raise ProjectError(f"bad final id in {uri}")
        return ("draft" if parts[1] == "drafts" else "final"), project, leaf
    raise ProjectError(f"not a pinaxium image URI: {uri} (expected {SCHEME}<project>/drafts/<file> or "
                       f"{SCHEME}<project>/finals/<final id>)")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


# ---------- final identity inside the file ----------
# A final carries its permanent URI in a PNG tEXt chunk, so it identifies itself wherever it is moved or however
# it is renamed. The chunk is spliced in before IEND: pixel data and ComfyUI's own metadata stay byte-identical.

STAMP_KEY = b"pinaxium:final"
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, data: bytes) -> bytes:
    import struct
    import zlib
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def stamp_png(data: bytes, uri: str) -> bytes:
    if not data.startswith(PNG_SIG):
        raise ProjectError("not a PNG file")
    end = data.rfind(b"IEND")
    if end < 4:
        raise ProjectError("PNG without an IEND chunk")
    return data[:end - 4] + _chunk(b"tEXt", STAMP_KEY + b"\x00" + uri.encode("latin-1")) + data[end - 4:]


def read_stamp(path: str) -> str | None:
    """The final URI stamped into a PNG, if any (reads only the chunk headers, skipping image data)."""
    import struct
    try:
        with open(path, "rb") as f:
            if f.read(8) != PNG_SIG:
                return None
            while True:
                head = f.read(8)
                if len(head) < 8:
                    return None
                length, kind = struct.unpack(">I", head[:4])[0], head[4:]
                if kind == b"tEXt" and length < 4096:
                    body = f.read(length)
                    key, _, value = body.partition(b"\x00")
                    if key == STAMP_KEY:
                        return value.decode("latin-1")
                    f.seek(4, 1)
                elif kind == b"IEND":
                    return None
                else:
                    f.seek(length + 4, 1)
    except OSError:
        return None


def image_size(path: str) -> tuple[int, int] | None:
    from PIL import Image
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def pixel_hash(path: str) -> str:
    """SHA-256 of the decoded image (size, mode, pixels): unchanged by renames, stripped metadata or lossless
    re-encoding, unlike a hash of the file's bytes."""
    import hashlib
    from PIL import Image
    with Image.open(path) as im:
        im.load()
        h = hashlib.sha256(f"{im.size[0]}x{im.size[1]}:{im.mode}:".encode())
        h.update(im.tobytes())
    return h.hexdigest()


def _trash(path: str) -> None:
    test_dir = os.environ.get("PINAXIUM_TRASH_DIR")     # tests: a folder standing in for the recycle bin
    if test_dir:
        os.makedirs(test_dir, exist_ok=True)
        os.replace(path, os.path.join(test_dir, f"{time.time_ns()}-{os.path.basename(path)}"))
        return
    from send2trash import send2trash
    send2trash(path)


class Projects:
    def __init__(self, cfg: Config, trash: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.trash = trash or _trash
        self.drafts_root = os.path.join(cfg.output_dir, SUBFOLDER)
        self.uploads_root = os.path.join(cfg.input_dir, SUBFOLDER)
        self.finals_root = cfg.finals_dir

    # ---------- folders ----------

    def drafts_dir(self, project: str) -> str:
        return os.path.join(self.drafts_root, project)

    def uploads_dir(self, project: str) -> str:
        return os.path.join(self.uploads_root, project)

    def finals_dir(self, project: str) -> str:
        return os.path.join(self.finals_root, project)

    def output_prefix(self, project: str) -> str:
        """Folder for ComfyUI SaveImage's filename_prefix (relative to ComfyUI's output folder)."""
        return f"{SUBFOLDER}/{project}"

    def upload_subfolder(self, project: str) -> str:
        return f"{SUBFOLDER}/{project}"

    def migrate_flat_layout(self) -> int:
        """Images saved before projects existed sit directly in output/pinaxium: move them into the default
        project. Returns how many moved."""
        if not os.path.isdir(self.drafts_root):
            return 0
        loose = [e for e in os.scandir(self.drafts_root) if e.is_file() and e.name.lower().endswith(IMAGE_EXT)]
        if not loose:
            return 0
        dest = self.drafts_dir(DEFAULT_PROJECT)
        os.makedirs(dest, exist_ok=True)
        for e in loose:
            if not os.path.exists(os.path.join(dest, e.name)):
                os.replace(e.path, os.path.join(dest, e.name))
        return len(loose)

    # ---------- JSON files (manifest, finals index) ----------

    def _manifest_path(self, project: str) -> str:
        return os.path.join(self.drafts_dir(project), MANIFEST)

    def _index_path(self, project: str) -> str:
        return os.path.join(self.finals_dir(project), FINALS_INDEX)

    def _lock(self, project: str):
        return _FileLock(os.path.join(self.drafts_dir(project), ".project.lock"))

    @staticmethod
    def _read(path: str) -> dict:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write(path: str, data: dict) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)

    def manifest(self, project: str) -> dict:
        data = self._read(self._manifest_path(project))
        data.setdefault("project", project)
        data.setdefault("images", {})
        return data

    def index(self, project: str) -> dict:
        data = self._read(self._index_path(project))
        data.setdefault("project", project)
        data.setdefault("finals", {})
        return data

    def record(self, project: str, paths: list[str], meta: dict) -> None:
        """Remember what made these drafts (prompt, model, seed, settings, sources)."""
        os.makedirs(self.drafts_dir(project), exist_ok=True)
        with self._lock(project):
            data = self.manifest(project)
            data.setdefault("created", _now())
            here = os.path.realpath(self.drafts_dir(project))
            for p in paths:
                if os.path.dirname(os.path.realpath(p)) == here:
                    data["images"][os.path.basename(p)] = {**meta, "created": _now(), "final": None}
            self._write(self._manifest_path(project), data)

    # ---------- lookups ----------

    def project_names(self) -> list[str]:
        names = set()
        for root in (self.drafts_root, self.finals_root):
            if os.path.isdir(root):
                names |= {e.name for e in os.scandir(root) if e.is_dir() and _NAME_RE.match(e.name)}
        return sorted(names)

    @staticmethod
    def _pngs(d: str) -> list[str]:
        if not os.path.isdir(d):
            return []
        return [e.path for e in os.scandir(d) if e.is_file() and e.name.lower().endswith(IMAGE_EXT)]

    def list_projects(self) -> list[dict]:
        out = []
        for name in self.project_names():
            drafts = self._pngs(self.drafts_dir(name))
            finals = self.index(name)["finals"]
            mtimes = [os.path.getmtime(p) for p in drafts + self._pngs(self.finals_dir(name))]
            out.append({"project": name, "uri": project_uri(name), "drafts": len(drafts), "finals": len(finals),
                        "draft_bytes": sum(os.path.getsize(p) for p in drafts),
                        "last_activity": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(max(mtimes)))
                        if mtimes else None})
        return out

    def drafts(self, project: str | None = None) -> list[dict]:
        """Drafts with what's known about them, newest first."""
        out = []
        for name in [project] if project else self.project_names():
            info = self.manifest(name)["images"]
            for p in self._pngs(self.drafts_dir(name)):
                meta = info.get(os.path.basename(p)) or {}
                final = meta.get("final") or {}
                out.append({"path": p, "uri": draft_uri(name, os.path.basename(p)), "project": name, "kind": "draft",
                            "final_uri": final.get("uri"), **{k: meta.get(k) for k in ("model", "prompt", "seed")}})
        out.sort(key=lambda e: os.path.getmtime(e["path"]), reverse=True)
        return out

    # ---------- resolving finals (the file may have been renamed or moved) ----------

    def _matches(self, path: str, uri: str, entry: dict) -> bool:
        """Is this file the final `uri`? Its stamp says so; or, if the stamp was stripped, the image itself is
        identical (same dimensions, same pixel hash)."""
        try:
            if not os.path.isfile(path):
                return False
            stamp = read_stamp(path)
            if stamp is not None:
                return stamp == uri
            size = image_size(path)
            return (size is not None and list(size) == entry.get("size")
                    and pixel_hash(path) == entry.get("pixels_sha256"))
        except OSError:
            return False

    def _find_moved(self, uri: str, entry: dict) -> str | None:
        """Search the finals tree for a final whose file isn't where it was last seen: by stamp first, then by
        identical image content (only unstamped files of the same dimensions are decoded)."""
        if not os.path.isdir(self.finals_root):
            return None
        unstamped = []
        for dirpath, _, names in os.walk(self.finals_root):
            for n in names:
                if not n.lower().endswith(IMAGE_EXT):
                    continue
                p = os.path.join(dirpath, n)
                stamp = read_stamp(p)
                if stamp == uri:
                    return p
                if stamp is None:
                    unstamped.append(p)
        return next((p for p in unstamped if self._matches(p, uri, entry)), None)

    def resolve_final(self, project: str, fid: str) -> tuple[dict, str | None]:
        """-> (index entry, current path or None). Follows renames and moves inside the finals tree and
        remembers the new location."""
        entry = self.index(project)["finals"].get(fid)
        if entry is None:
            raise ProjectError(f"no such final: {final_uri(project, fid)}")
        uri = final_uri(project, fid)
        last = entry.get("path") or os.path.join(self.finals_dir(project), entry.get("file", ""))
        if self._matches(last, uri, entry):
            return entry, os.path.realpath(last)
        found = self._find_moved(uri, entry)
        if found:
            self._set_final_path(project, fid, found)
        return entry, (os.path.realpath(found) if found else None)

    def _set_final_path(self, project: str, fid: str, path: str) -> None:
        os.makedirs(self.drafts_dir(project), exist_ok=True)
        with self._lock(project):
            index = self.index(project)
            if fid in index["finals"]:
                index["finals"][fid]["path"] = os.path.realpath(path)
                self._write(self._index_path(project), index)

    def relink_final(self, ref: str, path: str) -> dict:
        """Point a final at its file after it was moved somewhere Pinaxium doesn't search. The file must carry
        the final's stamp or have its exact content."""
        kind, project, fid = parse_uri(ref)
        if kind != "final":
            raise ProjectError(f"{ref} is not a final URI")
        entry = self.index(project)["finals"].get(fid)
        if entry is None:
            raise ProjectError(f"no such final: {ref}")
        if not self._matches(path, ref, entry):
            raise ProjectError(f"{path} is not {ref}: neither its stamp nor its content matches")
        self._set_final_path(project, fid, path)
        return {"project": project, "id": fid, "uri": ref, "path": os.path.realpath(path), "already_final": True}

    def finals(self, project: str | None = None, query: str | None = None) -> list[dict]:
        """Finals across projects (or one), newest first. `query`: every word of it must start a word of the
        final's id, title, description, tags, prompt or project (case-insensitive; "port" finds "portrait",
        "3" doesn't find "final-003")."""
        words = re.findall(r"[a-z0-9]+", (query or "").lower())
        out = []
        for name in [project] if project else self.project_names():
            for fid, e in self.index(name)["finals"].items():
                src = e.get("source") or {}
                text = " ".join(str(x) for x in (fid, e.get("title"), e.get("description"),
                                                 " ".join(e.get("tags") or []), src.get("prompt"), name)).lower()
                tokens = re.findall(r"[a-z0-9]+", text)
                if all(any(t.startswith(w) for t in tokens) for w in words):
                    _, path = self.resolve_final(name, fid)
                    out.append({"id": fid, "uri": final_uri(name, fid), "project": name,
                                "path": path or e.get("path") or "", "exists": path is not None,
                                "title": e.get("title"),
                                "description": e.get("description"), "tags": e.get("tags") or [],
                                "created": e.get("created"), "model": src.get("model"), "prompt": src.get("prompt"),
                                "seed": src.get("seed"), "derived_from": src.get("derived_from"),
                                "source_draft": src.get("draft")})
        out.sort(key=lambda e: e.get("created") or "", reverse=True)
        return out

    def get_final(self, ref: str) -> dict:
        """A final by URI (pinaxium://projects/<p>/finals/<id>), or by the path of its (stamped) file."""
        if ref.startswith("pinaxium://"):
            kind, project, fid = parse_uri(ref)
            if kind != "final":
                raise ProjectError(f"{ref} is not a final URI (finalize_image a draft first)")
        else:
            kind, project, _, fid = self.locate(ref)
            if kind != "final":
                raise ProjectError(f"{ref} is a draft, not a final (finalize_image it first)")
        matches = [f for f in self.finals(project) if f["id"] == fid]
        if not matches:
            raise ProjectError(f"no such final: {final_uri(project, fid)}")
        return matches[0]

    def locate(self, ref: str) -> tuple[str, str, str, str | None]:
        """A path or pinaxium:// URI -> (kind 'draft'|'final', project, absolute path, final id or None).
        Finals resolve through Pinaxium (index, stamp, hash), wherever their file now is; a path is a final if
        the file is one (by its stamp), else a draft if it is in a project's drafts folder."""
        if ref.startswith("pinaxium://"):
            kind, project, leaf = parse_uri(ref)
            if kind == "project":
                raise ProjectError(f"{ref} is a project, not an image")
            if kind == "draft":
                path = os.path.join(self.drafts_dir(project), leaf)
                if not leaf.lower().endswith(IMAGE_EXT) or not os.path.isfile(path):
                    raise ProjectError(f"no such draft: {ref}")
                return "draft", project, os.path.realpath(path), None
            _, path = self.resolve_final(project, leaf)
            if path is None:
                raise ProjectError(f"the file of final {ref} can't be found (moved outside {self.finals_root}?): "
                                   "use relink_final to point Pinaxium at it")
            return "final", project, path, leaf
        real = os.path.realpath(ref)
        if not (real.lower().endswith(IMAGE_EXT) and os.path.isfile(real)):
            raise ProjectError(f"{ref} is not one of Pinaxium's project images")
        stamp = read_stamp(real)
        if stamp:
            try:
                kind, project, fid = parse_uri(stamp)
                entry = self.index(project)["finals"].get(fid) if kind == "final" else None
                if entry is not None and self._matches(real, stamp, entry):
                    return "final", project, real, fid
            except ProjectError:
                pass
        parent = os.path.dirname(real)
        if os.path.dirname(parent) == os.path.realpath(self.drafts_root):
            return "draft", os.path.basename(parent), real, None
        raise ProjectError(f"{ref} is not one of Pinaxium's project images")

    # ---------- finishing ----------

    def finalize(self, ref: str, fid: str | None = None, title: str | None = None, description: str | None = None,
                 tags: list[str] | None = None) -> dict:
        """Copy a draft into its project's finals under a permanent id (given, or the next final-NNN), with
        searchable title/description/tags and the draft's provenance. Finalizing the same draft again returns
        its existing final; an id already used by another final is refused (ids are never reused)."""
        kind, project, path, existing = self.locate(ref)
        if kind == "final":
            return {"project": project, "id": existing, "uri": final_uri(project, existing), "path": path,
                    "already_final": True}
        filename = os.path.basename(path)
        with self._lock(project):
            manifest = self.manifest(project)
            draft = manifest["images"].setdefault(filename, {})
            prior = (draft.get("final") or {}).get("id")
            index = self.index(project)
            if prior and prior in index["finals"]:
                e = index["finals"][prior]
                return {"project": project, "id": prior, "uri": final_uri(project, prior),
                        "path": e.get("path", ""), "already_final": True}
            if fid:
                fid = final_id(fid)
                if fid in index["finals"] or fid in index.get("retired", []):
                    raise ProjectError(f"final id {fid!r} is already taken in project {project}")
            else:
                n = len(index["finals"]) + len(index.get("retired", [])) + 1
                while f"final-{n:03d}" in index["finals"]:
                    n += 1
                fid = f"final-{n:03d}"
            dest = os.path.join(self.finals_dir(project), fid + IMAGE_EXT)
            if os.path.exists(dest):
                raise ProjectError(f"{dest} already exists but isn't a registered final; move it away first")
            os.makedirs(self.finals_dir(project), exist_ok=True)
            with open(path, "rb") as f:
                stamped = stamp_png(f.read(), final_uri(project, fid))     # the file names its own identity
            tmp = dest + ".tmp"
            with open(tmp, "wb") as f:
                f.write(stamped)
            shutil.copystat(path, tmp)
            os.replace(tmp, dest)
            source = {k: v for k, v in draft.items() if k not in ("final", "created")}
            index["finals"][fid] = {
                "path": os.path.realpath(dest), "size": list(image_size(dest) or ()), "pixels_sha256": pixel_hash(dest),
                "title": title or None,
                "description": description or draft.get("prompt"), "tags": [t.strip() for t in tags or [] if t.strip()],
                "created": _now(), "source": {**source, "draft": draft_uri(project, filename)}}
            self._write(self._index_path(project), index)
            draft["final"] = {"id": fid, "uri": final_uri(project, fid)}
            self._write(self._manifest_path(project), manifest)
        return {"project": project, "id": fid, "uri": final_uri(project, fid), "path": dest, "already_final": False}

    # ---------- cleanup ----------

    def delete(self, refs: list[str] | None = None, project: str | None = None) -> dict:
        """Send drafts to the recycle bin: the given images, or every draft and upload of a project. Finals are
        never deleted. Returns what was deleted and what wasn't, with reasons."""
        if bool(refs) == bool(project):
            raise ProjectError("give either images or a project, not both")
        deleted, refused, targets, uploads = [], [], [], []
        if project:
            project = project_name(project)
            targets = self._pngs(self.drafts_dir(project))
            if os.path.isdir(self.uploads_dir(project)):
                uploads = [e.path for e in os.scandir(self.uploads_dir(project)) if e.is_file()]
        else:
            for ref in refs:
                try:
                    kind, _, path, _ = self.locate(ref)
                except ProjectError as e:
                    refused.append({"image": ref, "reason": str(e)})
                    continue
                if kind == "final":
                    refused.append({"image": ref, "reason": "finals are never deleted by Pinaxium; remove the "
                                                            "file yourself if you want it gone"})
                    continue
                targets.append(path)
        for path in targets + uploads:
            try:
                self.trash(path)
                if path in targets:
                    deleted.append(path)
            except Exception as e:                   # never fall back to a permanent delete
                refused.append({"image": path, "reason": f"the recycle bin refused it: {e}"})
        self._forget(deleted)
        return {"deleted": deleted, "not_deleted": refused,
                "note": "Sent to the recycle bin; restore from there if needed."}

    def _forget(self, paths: list[str]) -> None:
        by_project: dict[str, list[str]] = {}
        for p in paths:
            by_project.setdefault(os.path.basename(os.path.dirname(p)), []).append(os.path.basename(p))
        for project, names in by_project.items():
            if not os.path.isdir(self.drafts_dir(project)):
                continue
            with self._lock(project):
                data = self.manifest(project)
                for n in names:
                    data["images"].pop(n, None)      # finals keep their provenance in the finals index
                self._write(self._manifest_path(project), data)

    def sweep(self, days: float) -> list[str]:
        """Recycle drafts older than `days` that were never finalized (draft_retention_days)."""
        if days <= 0:
            return []
        cutoff = time.time() - days * 86400
        old = [d["path"] for d in self.drafts() if os.path.getmtime(d["path"]) < cutoff and not d["final_uri"]]
        return self.delete(refs=old)["deleted"] if old else []


class _FileLock:
    """Cross-process lock for a project's JSON files (several MCP clients may write them)."""

    def __init__(self, path: str):
        self.path = path

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.time() + 15
        while True:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > LOCK_STALE_S:
                        os.remove(self.path)
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise ProjectError("project is busy (lock held); try again")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass
