"""The real detector (ComfyUI's detection code in ComfyUI's venv) on the real model files, with the ComfyUI
install found by auto-detection (nothing is written). Skipped when no ComfyUI or no model file is found.
Slow (~15 s ComfyUI import)."""

import os

import pytest

from pinaxium.config import detect, load
from pinaxium.registry import comfy_detector

INSTALLS = [i for i in detect() if i.comfy_python]
REAL = INSTALLS[0] if INSTALLS else None
M = os.path.join(REAL.base_dir, "models") if REAL else ""

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


@pytest.mark.skipif(REAL is None, reason="no ComfyUI install with a Python interpreter found")
def test_real_files_detected_as_expected(monkeypatch):
    rows = present(EXPECTED + CROSS)
    if not rows:
        pytest.skip("no model files present")
    monkeypatch.setenv("PINAXIUM_COMFY_DIR", REAL.comfy_dir)
    monkeypatch.setenv("PINAXIUM_COMFY_PYTHON", REAL.comfy_python)
    monkeypatch.setenv("PINAXIUM_BASE_DIR", REAL.base_dir)
    cfg = load(create=False)
    assert not cfg.problems()
    items = [{"path": os.path.join(M, f, n), "kind": k} for f, n, k, _ in rows]
    results = comfy_detector(cfg)(items)
    for (f, n, k, arch), res in zip(rows, results):
        assert res["error"] is None, f"{n} as {k}: {res['error']}"
        assert res["arch"] == arch, f"{n} as {k}: got {res['arch']}, expected {arch}"
        if n in SIZE_B and k == "diffusion":
            lo, hi = SIZE_B[n]
            assert lo <= res["params_b"] <= hi, f"{n}: params_b {res['params_b']}"
