"""ComfyUI API-format graphs, one builder per model family.

Each builder mirrors Comfy-Org's official template for that family (workflow_templates repo) and takes the
concrete file names from the registry, so any file of a supported architecture works.

qwen_image21: benchmarks/image_qwen_image_2_1_t2i.json, templates/image_qwen_image_2_1_image_edit.json
  UNETLoader -> QwenImage21Cache -> KSampler(euler/simple), CLIPLoader(type=qwen_image) ->
  TextEncodeQwenImage21, VAELoader -> VAEDecode -> SaveImage.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

MIN_SIDE, MAX_SIDE, MULTIPLE = 256, 2048, 16
MAX_REFERENCE_IMAGES = 10
OUTPUT_SUBFOLDER = "pinaxium"


@dataclass(frozen=True)
class Params:
    steps: int
    seed: int
    cfg: float


def snap(side: int) -> int:
    """Clamp a side length to [MIN_SIDE, MAX_SIDE] and round it to a multiple of MULTIPLE."""
    side = max(MIN_SIDE, min(MAX_SIDE, int(side)))
    return max(MIN_SIDE, int(side / MULTIPLE + 0.5) * MULTIPLE)    # half-up, not banker's rounding


def resolve(defaults: dict, steps: int | None, seed: int | None, cfg: float | None) -> Params:
    if steps is not None and not 1 <= steps <= 100:
        raise ValueError("steps must be between 1 and 100")
    if cfg is not None and not 0.0 <= cfg <= 20.0:
        raise ValueError("cfg must be between 0 and 20")
    return Params(steps=int(defaults["steps"] if steps is None else steps),
                  seed=random.randint(0, 2**53) if seed is None else int(seed),
                  cfg=float(defaults["cfg"] if cfg is None else cfg))


def _check_prompt(prompt: str) -> None:
    if not prompt.strip():
        raise ValueError("prompt is empty")


def _save(prefix: str, source: str = "decode") -> dict:
    return {"class_type": "SaveImage", "inputs": {"images": [source, 0], "filename_prefix": f"{OUTPUT_SUBFOLDER}/{prefix}"}}


def into_folder(graph: dict, folder: str) -> dict:
    """Point every SaveImage at `folder` (relative to ComfyUI's output folder), keeping its file name prefix:
    pinaxium/qwen21 -> pinaxium/<project>/qwen21."""
    for node in graph.values():
        if node.get("class_type") == "SaveImage":
            stem = node["inputs"]["filename_prefix"].rsplit("/", 1)[-1]
            node["inputs"]["filename_prefix"] = f"{folder}/{stem}"
    return graph


def _no_transparency(builder: str, transparent: bool) -> None:
    if transparent:
        raise ValueError(f"{builder} models can't produce transparent images")


def _ksampler(model: list, p: Params, latent: list, sampler: str = "euler", scheduler: str = "simple") -> dict:
    return {"class_type": "KSampler", "inputs": {
        "model": model, "positive": ["encode", 0], "negative": ["encode", 1], "latent_image": latent,
        "seed": p.seed, "steps": p.steps, "cfg": p.cfg, "sampler_name": sampler, "scheduler": scheduler, "denoise": 1.0}}


class QwenImage21:
    name = "qwen_image21"
    supports_edit = True

    @staticmethod
    def _common(files: dict, transparent: bool) -> dict:
        """The Qwen-Image 2.1 VAE decodes RGBA. Unless transparency is wanted, the alpha channel (near-opaque
        noise, 251-255, on ordinary images) is dropped so results are plain RGB."""
        g = {
            "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": files["unet"], "weight_dtype": "default"}},
            "cache": {"class_type": "QwenImage21Cache", "inputs": {"model": ["unet", 0], "device": "auto", "dtype": "default"}},
            "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": files["clip"], "type": "qwen_image", "device": "default"}},
            "vae": {"class_type": "VAELoader", "inputs": {"vae_name": files["vae"]}},
            "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sampler", 0], "vae": ["vae", 0]}},
        }
        if transparent:
            g["save"] = _save("qwen21")
        else:
            g["rgb"] = {"class_type": "SplitImageWithAlpha", "inputs": {"image": ["decode", 0]}}
            g["save"] = _save("qwen21", "rgb")
        return g

    @classmethod
    def text_to_image(cls, files: dict, prompt: str, negative_prompt: str, width: int, height: int, p: Params,
                      transparent: bool = False) -> dict:
        _check_prompt(prompt)
        g = cls._common(files, transparent)
        g["encode"] = {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["clip", 0], "prompt": prompt, "negative_prompt": negative_prompt, "resolution": 1024}}
        g["latent"] = {"class_type": "EmptyLatentImage", "inputs": {"width": snap(width), "height": snap(height), "batch_size": 1}}
        g["sampler"] = _ksampler(["cache", 0], p, ["latent", 0])
        return g

    @classmethod
    def edit(cls, files: dict, prompt: str, negative_prompt: str, uploaded_names: list[str], resolution: int,
             p: Params, transparent: bool = False) -> dict:
        """The output takes the first reference's aspect ratio at about resolution x resolution pixels
        (the encoder's own latent output, as in the official template)."""
        _check_prompt(prompt)
        if not 1 <= len(uploaded_names) <= MAX_REFERENCE_IMAGES:
            raise ValueError(f"need 1 to {MAX_REFERENCE_IMAGES} reference images")
        g = cls._common(files, transparent)
        enc = {"clip": ["clip", 0], "vae": ["vae", 0], "prompt": prompt, "negative_prompt": negative_prompt,
               "resolution": snap(resolution)}
        for i, name in enumerate(uploaded_names, start=1):
            g[f"ref{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
            enc[f"images.image_{i}"] = [f"ref{i}", 0]
        g["encode"] = {"class_type": "TextEncodeQwenImage21", "inputs": enc}
        g["sampler"] = _ksampler(["cache", 0], p, ["encode", 2])
        return g


def _negative(clip: list, negative_prompt: str, p: Params) -> dict:
    """Distilled models run at cfg 1 with a zeroed negative (official templates); with cfg > 1 the negative
    prompt is encoded as in the undistilled templates."""
    if p.cfg > 1.0:
        return {"class_type": "CLIPTextEncode", "inputs": {"clip": clip, "text": negative_prompt}}
    return {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["pos", 0]}}


class ZImage:
    """templates/image_z_image_turbo_int8.json, templates/image_z_image.json:
    UNETLoader -> ModelSamplingAuraFlow(shift 3) -> KSampler(res_multistep/simple), CLIPLoader(type=lumina2),
    EmptySD3LatentImage, VAELoader(ae) -> VAEDecode."""
    name = "z_image"
    supports_edit = False

    @staticmethod
    def text_to_image(files: dict, prompt: str, negative_prompt: str, width: int, height: int, p: Params,
                      transparent: bool = False) -> dict:
        _check_prompt(prompt)
        _no_transparency("Z-Image", transparent)
        return {
            "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": files["unet"], "weight_dtype": "default"}},
            "shift": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["unet", 0], "shift": 3.0}},
            "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": files["clip"], "type": "lumina2", "device": "default"}},
            "vae": {"class_type": "VAELoader", "inputs": {"vae_name": files["vae"]}},
            "pos": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
            "neg": _negative(["clip", 0], negative_prompt, p),
            "latent": {"class_type": "EmptySD3LatentImage", "inputs": {"width": snap(width), "height": snap(height), "batch_size": 1}},
            "sampler": {"class_type": "KSampler", "inputs": {
                "model": ["shift", 0], "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["latent", 0],
                "seed": p.seed, "steps": p.steps, "cfg": p.cfg, "sampler_name": "res_multistep", "scheduler": "simple",
                "denoise": 1.0}},
            "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sampler", 0], "vae": ["vae", 0]}},
            "save": _save("zimage"),
        }


class Flux2Klein:
    """templates/image_flux2_klein_text_to_image.json, templates/image_flux2_klein_image_edit_4b_distilled.json:
    UNETLoader, CLIPLoader(type=flux2), CFGGuider, KSamplerSelect(euler), Flux2Scheduler, RandomNoise,
    EmptyFlux2LatentImage -> SamplerCustomAdvanced -> VAEDecode. Edits: each reference is scaled to ~1 MP,
    VAE-encoded and attached to both conditionings with ReferenceLatent; the first sets the output size."""
    name = "flux2_klein"
    supports_edit = True

    @staticmethod
    def _graph(files: dict, prompt: str, negative_prompt: str, p: Params, width, height) -> dict:
        return {
            "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": files["unet"], "weight_dtype": "default"}},
            "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": files["clip"], "type": "flux2", "device": "default"}},
            "vae": {"class_type": "VAELoader", "inputs": {"vae_name": files["vae"]}},
            "pos": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
            "neg": _negative(["clip", 0], negative_prompt, p),
            "guider": {"class_type": "CFGGuider", "inputs": {"model": ["unet", 0], "positive": ["pos", 0],
                                                           "negative": ["neg", 0], "cfg": p.cfg}},
            "ksampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
            "sigmas": {"class_type": "Flux2Scheduler", "inputs": {"steps": p.steps, "width": width, "height": height}},
            "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": p.seed}},
            "latent": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
            "sampler": {"class_type": "SamplerCustomAdvanced", "inputs": {
                "noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["ksampler", 0], "sigmas": ["sigmas", 0],
                "latent_image": ["latent", 0]}},
            "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sampler", 0], "vae": ["vae", 0]}},
            "save": _save("klein"),
        }

    @classmethod
    def text_to_image(cls, files: dict, prompt: str, negative_prompt: str, width: int, height: int, p: Params,
                      transparent: bool = False) -> dict:
        _check_prompt(prompt)
        _no_transparency("FLUX.2 klein", transparent)
        return cls._graph(files, prompt, negative_prompt, p, snap(width), snap(height))

    @classmethod
    def edit(cls, files: dict, prompt: str, negative_prompt: str, uploaded_names: list[str], resolution: int,
             p: Params, transparent: bool = False) -> dict:
        _check_prompt(prompt)
        _no_transparency("FLUX.2 klein", transparent)
        if not 1 <= len(uploaded_names) <= MAX_REFERENCE_IMAGES:
            raise ValueError(f"need 1 to {MAX_REFERENCE_IMAGES} reference images")
        mp = round(snap(resolution) ** 2 / 1e6, 2)
        g = cls._graph(files, prompt, negative_prompt, p, ["size", 0], ["size", 1])
        pos, neg = ["pos", 0], ["neg", 0]
        for i, name in enumerate(uploaded_names, start=1):
            g[f"ref{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
            g[f"scale{i}"] = {"class_type": "ImageScaleToTotalPixels", "inputs": {
                "image": [f"ref{i}", 0], "upscale_method": "nearest-exact", "megapixels": mp, "resolution_steps": 1}}
            g[f"enc{i}"] = {"class_type": "VAEEncode", "inputs": {"pixels": [f"scale{i}", 0], "vae": ["vae", 0]}}
            g[f"refpos{i}"] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": pos, "latent": [f"enc{i}", 0]}}
            g[f"refneg{i}"] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": neg, "latent": [f"enc{i}", 0]}}
            pos, neg = [f"refpos{i}", 0], [f"refneg{i}", 0]
        g["size"] = {"class_type": "GetImageSize", "inputs": {"image": ["scale1", 0]}}
        g["guider"]["inputs"]["positive"], g["guider"]["inputs"]["negative"] = pos, neg
        return g


BUILDERS = {b.name: b for b in (QwenImage21, ZImage, Flux2Klein)}
