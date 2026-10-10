"""Classify model files with ComfyUI's own detection code, without loading any weights.

Run with ComfyUI's interpreter, cwd = the ComfyUI folder:
    <comfy>/.venv/Scripts/python.exe <this file>  < request.json  > result.json
request: [{"path": ..., "kind": "diffusion"|"checkpoint"|"text_encoder"|"vae"|"lora"}, ...]
result:  after the line marker "@@RESULT@@", a JSON list aligned with the request:
         [{"arch": str|null, "params_b": float|null, "error": str|null}, ...]
         params_b = stored elements in billions (a size hint; packed 4-bit formats count half)

Only the safetensors header is read; tensors are stand-ins on the "meta" device (shape and dtype, no data),
which is all ComfyUI's detectors look at.
"""

import json
import os
import struct
import sys

sys.argv = [sys.argv[0], "--cpu"]          # ComfyUI parses argv on import; keep it off the GPU
sys.path.insert(0, os.getcwd())

import torch  # noqa: E402

DTYPES = {"F64": torch.float64, "F32": torch.float32, "F16": torch.float16, "BF16": torch.bfloat16,
          "I64": torch.int64, "I32": torch.int32, "I16": torch.int16, "I8": torch.int8, "U8": torch.uint8,
          "BOOL": torch.bool, "F8_E4M3": torch.float8_e4m3fn, "F8_E5M2": torch.float8_e5m2}


def meta_state_dict(path):
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        if n > 100 * 2**20:
            raise ValueError("implausible safetensors header")
        header = json.loads(f.read(n))
    metadata = header.pop("__metadata__", None)
    sd = {k: torch.empty(v["shape"], dtype=DTYPES.get(v["dtype"], torch.uint8), device="meta")
          for k, v in header.items()}
    return sd, metadata


def qwen_image21_vae(sd):
    # mirrors comfy/sd.py VAE.__init__: Wan 2.2 layout, temporal kernel 1, no patchify (Qwen-Image 2.1 VAE)
    head = sd.get("decoder.head.2.weight")
    return ("decoder.middle.0.residual.0.gamma" in sd
            and "decoder.upsamples.0.upsamples.0.residual.2.weight" in sd
            and head is not None and head.ndim == 5 and head.shape[2] == 1)


def vae_arch(sd):
    if qwen_image21_vae(sd):
        return "QwenImage21VAE"
    # mirrors comfy/sd.py's generic AutoencoderKL branch: latent channels = decoder.conv_in input channels
    # (16: FLUX.1 / Z-Image "ae", 32: FLUX.2). ComfyUI itself tells same-shaped VAEs apart only by the user's pick.
    w = sd.get("decoder.conv_in.weight")
    if w is not None and w.ndim == 4:
        return f"VAE_{w.shape[1]}ch"
    return None


def params_b(sd):
    return round(sum(t.numel() for t in sd.values()) / 1e9, 2)


def classify(path, kind, model_detection, sd_mod):
    """-> (arch, size in billions of stored elements)"""
    if not path.lower().endswith((".safetensors", ".sft")):
        return None, None
    sd, metadata = meta_state_dict(path)
    if kind in ("diffusion", "checkpoint"):
        prefixes = ["model.diffusion_model.", ""] if kind == "checkpoint" else ["", "model.diffusion_model."]
        for prefix in prefixes:
            cfg = model_detection.model_config_from_unet(sd, prefix, metadata=metadata)
            if cfg is not None:
                return type(cfg).__name__, params_b(sd)
        return None, params_b(sd)
    if kind == "text_encoder":
        te = sd_mod.detect_te_model(sd)
        return (te.name if te is not None else None), params_b(sd)
    if kind == "vae":
        return vae_arch(sd), None
    return None, None


def main():
    request = json.load(sys.stdin)
    out = []
    try:
        import comfy.model_detection as model_detection
        import comfy.sd as sd_mod
    except Exception as e:  # ComfyUI itself broken: report per file rather than crash
        out = [{"arch": None, "params_b": None, "error": f"ComfyUI import failed: {e}"} for _ in request]
        sys.stdout.write("\n@@RESULT@@" + json.dumps(out))
        return
    for item in request:
        try:
            arch, size = classify(item["path"], item["kind"], model_detection, sd_mod)
            out.append({"arch": arch, "params_b": size, "error": None})
        except Exception as e:
            out.append({"arch": None, "params_b": None, "error": f"{type(e).__name__}: {e}"})
    sys.stdout.write("\n@@RESULT@@" + json.dumps(out))


if __name__ == "__main__":
    main()
