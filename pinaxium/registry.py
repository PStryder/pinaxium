"""Discover models by scanning ComfyUI's model folders.

Every file is classified by ComfyUI's own detection code (comfy_detect.py, run in ComfyUI's venv on the
safetensors header only). Results are cached by path, size and mtime, so a rescan costs only new or
changed files.

A diffusion model becomes a usable *model* when its architecture has a family here (a verified workflow
builder) and the family's companion files (text encoder, VAE) are present. Everything else is still
listed, with the reason it can't be used, and stays reachable through run_workflow.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from pinaxium.config import Config

FOLDERS = {"diffusion_models": "diffusion", "unet": "diffusion", "checkpoints": "checkpoint",
           "text_encoders": "text_encoder", "clip": "text_encoder", "vae": "vae", "loras": "lora"}
EXTENSIONS = (".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin", ".gguf")
DETECTOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "comfy_detect.py")


@dataclass(frozen=True)
class Family:
    name: str
    arch: str                        # ComfyUI supported_models class name of the diffusion model
    builder: str                     # key into workflows.BUILDERS
    text_encoder_arch: str
    vae_arch: str
    capabilities: dict
    license: str
    variants: Callable[[str], dict]  # filename -> defaults (variant, steps, cfg, note) for that variant
    notes: str = ""                  # for choosing between models: strengths, limits, relative speed
    max_params_b: float | None = None   # tells same-class models of different sizes apart
    vae_prefer: tuple = ()           # name hints when several VAEs share a shape (e.g. 16-channel ones)

    def matches(self, f: "FileInfo") -> bool:
        if f.arch != self.arch:
            return False
        return self.max_params_b is None or (f.params_b is not None and f.params_b <= self.max_params_b)


def _turbo_variant(turbo: dict, base: dict):
    def pick(filename: str) -> dict:
        return dict(turbo, variant="turbo") if "turbo" in filename.lower() else dict(base, variant="base")
    return pick


def _klein_variant(filename: str) -> dict:
    # BFL ships klein as a step-distilled model and an undistilled "base" one
    if "base" in filename.lower():
        return {"variant": "base", "steps": 20, "cfg": 5.0,
                "note": "Undistilled base: much slower, uses real guidance and negative prompts; mainly for fine-tuning."}
    return {"variant": "distilled", "steps": 4, "cfg": 1.0, "note": "Step-distilled: 4 steps."}


_TURBO_NOTE = "Distilled turbo variant: several times faster than base, with very similar images for the same seed."

# Each family's builder and defaults come from Comfy-Org's official template, and each was verified with a
# real render before being listed here. Notes are relative comparisons from those renders (bench.py).
FAMILIES = [
    Family(name="Qwen-Image 2.1", arch="QwenImage21", builder="qwen_image21",
           text_encoder_arch="QWEN3VL_8B", vae_arch="QwenImage21VAE",
           capabilities={"text_to_image": True, "edit": True, "max_reference_images": 10, "max_side": 2048,
                         "transparency": True, "text_rendering": "strong"},
           license="Qwen Research License (non-commercial)",
           notes="Highest quality and the most reliable lettering; edits and composes from up to 10 reference "
                 "images; can output transparency. The base variant is the slowest model here.",
           variants=_turbo_variant({"steps": 8, "cfg": 1.0, "note": _TURBO_NOTE},
                                   {"steps": 25, "cfg": 1.0, "note": "Base variant: 25 steps."})),
    Family(name="Z-Image", arch="ZImage", builder="z_image",
           text_encoder_arch="QWEN3_4B", vae_arch="VAE_16ch", vae_prefer=("ae.", "flux"),
           capabilities={"text_to_image": True, "edit": False, "max_side": 2048, "text_rendering": "good"},
           license="Apache-2.0",
           notes="Fast text-to-image with clean lettering; no editing.",
           variants=_turbo_variant({"steps": 8, "cfg": 1.0, "note": "Turbo: 8 steps."},
                                   {"steps": 25, "cfg": 4.0, "note": "Base: 25 steps with real guidance; slower."})),
    Family(name="FLUX.2 [klein] 4B", arch="Flux2", builder="flux2_klein", max_params_b=6.0,
           text_encoder_arch="QWEN3_4B", vae_arch="VAE_32ch",
           capabilities={"text_to_image": True, "edit": True, "max_reference_images": 4, "max_side": 2048,
                         "text_rendering": "weak"},
           license="Apache-2.0",
           notes="The fastest model here; edits and composes from up to 4 reference images. Lettering is "
                 "unreliable (misspellings), so prefer another model when the image must contain text.",
           variants=_klein_variant),
]

# companion preference when several files share an architecture: smaller formats first (they fit more GPUs)
_PRECISION_RANK = ("int8", "fp8", "w4a8", "nf4", "bf16", "fp16", "fp32")


def _rank(filename: str) -> tuple:
    low = filename.lower()
    for i, tag in enumerate(_PRECISION_RANK):
        if tag in low:
            return (i, low)
    return (len(_PRECISION_RANK), low)


CACHE_VERSION = 2


def family_for(f: "FileInfo") -> Family | None:
    return next((fam for fam in FAMILIES if fam.matches(f)), None)


@dataclass
class FileInfo:
    path: str
    folder: str
    kind: str
    arch: str | None
    size_gb: float
    error: str | None = None
    params_b: float | None = None

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


@dataclass
class Model:
    id: str
    family: str
    builder: str
    files: dict                      # {"unet": name, "clip": name, "vae": name}
    defaults: dict
    capabilities: dict
    license: str
    available: bool
    missing: list = field(default_factory=list)
    notes: str = ""

    def public(self) -> dict:
        d = {"id": self.id, "family": self.family, "available": self.available, "defaults": self.defaults,
             "capabilities": self.capabilities, "files": self.files, "license": self.license, "notes": self.notes}
        if self.missing:
            d["missing"] = self.missing
        return d


Detector = Callable[[list[dict]], list[dict]]


def comfy_detector(cfg: Config) -> Detector:
    """Results with "transient": True describe a broken setup, not the file, and are never cached."""
    def detect(items: list[dict]) -> list[dict]:
        problems = cfg.problems()
        if problems:
            return [{"arch": None, "error": "; ".join(problems), "transient": True} for _ in items]
        try:
            r = subprocess.run([cfg.comfy_python, DETECTOR], input=json.dumps(items), capture_output=True,
                               text=True, cwd=cfg.comfy_dir, timeout=300,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as e:
            return [{"arch": None, "error": f"detector failed: {e}", "transient": True} for _ in items]
        marker = r.stdout.rfind("@@RESULT@@")
        if marker < 0:
            tail = (r.stderr or r.stdout)[-500:]
            return [{"arch": None, "error": f"detector failed: {tail}", "transient": True} for _ in items]
        return json.loads(r.stdout[marker + len("@@RESULT@@"):])
    return detect


class Registry:
    def __init__(self, cfg: Config, detector: Detector | None = None):
        self.cfg = cfg
        self.detect = detector or comfy_detector(cfg)
        self.cache_path = cfg.state_path("scan_cache.json")

    def _load_cache(self) -> dict:
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("entries", {}) if data.get("version") == CACHE_VERSION else {}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_cache(self, cache: dict) -> None:
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": CACHE_VERSION, "entries": cache}, f, indent=1)
        os.replace(tmp, self.cache_path)

    def scan_files(self) -> list[FileInfo]:
        root = self.cfg.models_dir
        found = []
        for folder, kind in FOLDERS.items():
            d = os.path.join(root, folder)
            if not os.path.isdir(d):
                continue
            for dirpath, _, names in os.walk(d):
                for n in names:
                    if n.lower().endswith(EXTENSIONS):
                        p = os.path.join(dirpath, n)
                        st = os.stat(p)
                        found.append((p, folder, kind, st.st_size, st.st_mtime_ns))
        cache = self._load_cache()
        key = lambda p, kind, size, mt: f"{p}|{kind}|{size}|{mt}"          # noqa: E731
        todo = [(key(p, kind, size, mt), {"path": p, "kind": kind}) for p, _, kind, size, mt in found
                if kind != "lora" and key(p, kind, size, mt) not in cache]
        if todo:
            # stored under the scan-time key: a file that changes meanwhile (still downloading) is simply
            # detected again on the next scan
            for (k, _), res in zip(todo, self.detect([item for _, item in todo])):
                cache[k] = res
            live = {key(p, kind, size, mt) for p, _, kind, size, mt in found}
            # a broken setup (transient) is reported now but not remembered: fixing it fixes the listing
            self._save_cache({k: v for k, v in cache.items() if k in live and not v.get("transient")})
        out = []
        for p, folder, kind, size, mt in found:
            res = cache.get(key(p, kind, size, mt), {"arch": None, "error": None})
            out.append(FileInfo(path=p, folder=folder, kind=kind, arch=res.get("arch"),
                                size_gb=round(size / 1e9, 2), error=res.get("error"), params_b=res.get("params_b")))
        return out

    def models(self, files: list[FileInfo] | None = None) -> list[Model]:
        files = self.scan_files() if files is None else files
        out = []
        for f in files:
            fam = family_for(f) if f.kind == "diffusion" else None
            if fam is None:
                continue
            tes = sorted((x for x in files if x.kind == "text_encoder" and x.arch == fam.text_encoder_arch),
                         key=lambda x: _rank(x.name))
            vae_key = lambda x: (not any(h in x.name.lower() for h in fam.vae_prefer), _rank(x.name))  # noqa: B023,E731
            vaes = sorted((x for x in files if x.kind == "vae" and x.arch == fam.vae_arch), key=vae_key)
            missing = []
            if not tes:
                missing.append(f"text encoder ({fam.text_encoder_arch})")
            if not vaes:
                missing.append(f"VAE ({fam.vae_arch})")
            defaults = fam.variants(f.name)
            variant_note = defaults.pop("note", "")
            out.append(Model(
                id=os.path.splitext(f.name)[0], family=fam.name, builder=fam.builder,
                files={"unet": f.name, "clip": tes[0].name if tes else None, "vae": vaes[0].name if vaes else None},
                defaults=defaults, capabilities=fam.capabilities, license=fam.license,
                available=not missing, missing=missing, notes=f"{fam.notes} {variant_note}".strip()))
        order = {fam.name: i for i, fam in enumerate(FAMILIES)}      # default: first family, base before turbo
        out.sort(key=lambda m: (not m.available, order[m.family], m.defaults.get("variant") == "turbo", m.id))
        return out

    def get(self, model_id: str | None) -> Model:
        models = self.models()
        usable = [m for m in models if m.available]
        if model_id is None:
            wanted = self.cfg.default_model
            if wanted:
                model_id = wanted
            elif usable:
                return usable[0]
            else:
                raise LookupError("no usable model found; call list_models to see what is missing")
        for m in models:
            if m.id == model_id:
                if not m.available:
                    raise LookupError(f"model {model_id!r} is missing {', '.join(m.missing)}")
                return m
        raise LookupError(f"unknown model {model_id!r}; available: {[m.id for m in usable]}")

    def summary(self) -> dict:
        files = self.scan_files()
        models = self.models(files)
        used = {n for m in models for n in m.files.values() if n}
        other = []
        for f in files:
            if f.name in used:
                continue
            if f.kind == "diffusion" and f.arch:
                size = f" ({f.params_b}B)" if f.params_b else ""
                why = (f"architecture {f.arch}{size} detected, but this service has no verified workflow for it "
                       f"(use run_workflow)")
            elif f.kind == "lora":
                why = "LoRA (not applied by the built-in workflows; usable through run_workflow)"
            elif f.error:
                why = f"could not classify: {f.error}"
            elif f.arch:
                why = f"{f.kind} ({f.arch}), not needed by any usable model"
            else:
                why = f"{f.kind}, architecture not recognised"
            other.append({"file": f.name, "folder": f.folder, "size_gb": f.size_gb, "note": why})
        return {"models": [m.public() for m in models], "other_files": other}
