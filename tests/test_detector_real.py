"""The real detector (ComfyUI's detection code in ComfyUI's venv) on the real model files.
Skipped when ComfyUI or a file isn't installed. Slow (~15 s ComfyUI import)."""

import os

import pytest

from pinaxium.config import load
from pinaxium.registry import comfy_detector

CFG = load()
M = os.path.join(CFG.comfy_dir, "models")

EXPECTED = [   # (folder, file, kind, arch)
    ("diffusion_models", "qwen_image_2.1_int8_convrot.safetensors", "diffusion", "QwenImage21"),
    ("diffusion_models", "qwen_image_2.1_turbo_int8_convrot.safetensors", "diffusion", "QwenImage21"),
    ("text_encoders", "qwen3vl_8b_int8_convrot.safetensors", "text_encoder", "QWEN3VL_8B"),
    ("vae", "qwen_image_2.1_vae_bf16.safetensors", "vae", "QwenImage21VAE"),
    ("diffusion_models", "z_image_turbo_int8_convrot.safetensors", "diffusion", "ZImage"),
    ("diffusion_models", "flux-2-klein-4b.safetensors", "diffusion", "Flux2"),
    ("text_encoders", "qwen_3_4b.safetensors", "text_encoder", "QWEN3_4B"),
    ("vae", "ae.safetensors", "vae", "VAE_16ch"),
    ("vae", "flux2-vae.safetensors", "vae", "VAE_32ch"),
]
SIZE_B = {"flux-2-klein-4b.safetensors": (3.0, 6.0)}     # must land under the klein-4B family limit
# negative controls: a file must not be detected as something it isn't
CROSS = [
    ("diffusion_models", "qwen_image_2.1_int8_convrot.safetensors", "vae", None),
    ("vae", "qwen_image_2.1_vae_bf16.safetensors", "diffusion", None),
    ("diffusion_models", "qwen_image_2.1_int8_convrot.safetensors", "text_encoder", None),
]


def present(rows):
    return [r for r in rows if os.path.isfile(os.path.join(M, r[0], r[1]))]


@pytest.mark.skipif(not os.path.isfile(CFG.comfy_python), reason="ComfyUI venv not installed")
def test_real_files_detected_as_expected():
    rows = present(EXPECTED + CROSS)
    if not rows:
        pytest.skip("no model files present")
    items = [{"path": os.path.join(M, f, n), "kind": k} for f, n, k, _ in rows]
    results = comfy_detector(CFG)(items)
    for (f, n, k, arch), res in zip(rows, results):
        assert res["error"] is None, f"{n} as {k}: {res['error']}"
        assert res["arch"] == arch, f"{n} as {k}: got {res['arch']}, expected {arch}"
        if n in SIZE_B and k == "diffusion":
            lo, hi = SIZE_B[n]
            assert lo <= res["params_b"] <= hi, f"{n}: params_b {res['params_b']}"
