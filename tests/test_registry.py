"""Registry: folder scan, detection cache, family matching, companion choice, availability, defaults."""

import os

import pytest

from pinaxium.config import load
from pinaxium.registry import Registry

ARCH_BY_NAME = {   # what the fake detector "detects", by file name
    "qwen_image_2.1_int8_convrot.safetensors": "QwenImage21",
    "qwen_image_2.1_turbo_int8_convrot.safetensors": "QwenImage21",
    "qwen3vl_8b_bf16.safetensors": "QWEN3VL_8B",
    "qwen3vl_8b_int8_convrot.safetensors": "QWEN3VL_8B",
    "qwen3.5_9b_pe.safetensors": "QWEN35_9B",
    "qwen_image_2.1_vae_bf16.safetensors": "QwenImage21VAE",
    "some_flux_thing.safetensors": "Flux",
    "renamed_model.safetensors": "QwenImage21",
    "z_image_turbo_int8_convrot.safetensors": "ZImage",
    "z_image_bf16.safetensors": "ZImage",
    "qwen_3_4b.safetensors": "QWEN3_4B",
    "ae.safetensors": "VAE_16ch",
    "sd3_vae_fp16.safetensors": "VAE_16ch",   # ranks above "ae" on precision and name; only the hint picks ae
    "flux-2-klein-4b.safetensors": "Flux2",
    "flux-2-klein-base-4b.safetensors": "Flux2",
    "flux-2-klein-9b.safetensors": "Flux2",
    "flux2-vae.safetensors": "VAE_32ch",
}
PARAMS_B = {"flux-2-klein-4b.safetensors": 3.88, "flux-2-klein-base-4b.safetensors": 3.88,
            "flux-2-klein-9b.safetensors": 9.1}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("PINAXIUM_COMFY_DIR", str(tmp_path / "comfy"))
    monkeypatch.setenv("PINAXIUM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("PINAXIUM_DEFAULT_MODEL", raising=False)
    models = tmp_path / "comfy" / "models"
    calls = []

    def detector(items):
        calls.append([os.path.basename(i["path"]) for i in items])
        return [{"arch": ARCH_BY_NAME.get(os.path.basename(i["path"])),
                 "params_b": PARAMS_B.get(os.path.basename(i["path"])), "error": None} for i in items]

    def put(folder, name, size=10):
        d = models / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(b"x" * size)

    return Registry(load(), detector), put, calls


def full_qwen(put):
    put("diffusion_models", "qwen_image_2.1_int8_convrot.safetensors")
    put("diffusion_models", "qwen_image_2.1_turbo_int8_convrot.safetensors")
    put("text_encoders", "qwen3vl_8b_bf16.safetensors")
    put("text_encoders", "qwen3vl_8b_int8_convrot.safetensors")
    put("text_encoders", "qwen3.5_9b_pe.safetensors")
    put("vae", "qwen_image_2.1_vae_bf16.safetensors")


def test_discovers_qwen_models_with_variant_defaults(setup):
    reg, put, _ = setup
    full_qwen(put)
    ms = {m.id: m for m in reg.models()}
    assert set(ms) == {"qwen_image_2.1_int8_convrot", "qwen_image_2.1_turbo_int8_convrot"}
    base, turbo = ms["qwen_image_2.1_int8_convrot"], ms["qwen_image_2.1_turbo_int8_convrot"]
    assert base.available and turbo.available
    assert (base.defaults["steps"], turbo.defaults["steps"]) == (25, 8)
    assert base.builder == "qwen_image21" and base.capabilities["edit"]


def test_prefers_int8_text_encoder_and_ignores_other_architectures(setup):
    reg, put, _ = setup
    full_qwen(put)
    m = reg.get("qwen_image_2.1_int8_convrot")
    assert m.files == {"unet": "qwen_image_2.1_int8_convrot.safetensors",
                       "clip": "qwen3vl_8b_int8_convrot.safetensors",
                       "vae": "qwen_image_2.1_vae_bf16.safetensors"}


def test_missing_companions_make_model_unavailable_with_reason(setup):
    reg, put, _ = setup
    put("diffusion_models", "qwen_image_2.1_int8_convrot.safetensors")
    put("vae", "qwen_image_2.1_vae_bf16.safetensors")
    (m,) = reg.models()
    assert not m.available and m.missing == ["text encoder (QWEN3VL_8B)"]
    with pytest.raises(LookupError, match="missing text encoder"):
        reg.get(m.id)
    with pytest.raises(LookupError, match="no usable model"):
        reg.get(None)


def test_detection_by_content_not_name(setup):
    reg, put, _ = setup
    full_qwen(put)
    put("diffusion_models", "renamed_model.safetensors")
    assert reg.get("renamed_model").available


def test_default_is_base_before_turbo_and_env_override(setup, monkeypatch):
    reg, put, _ = setup
    full_qwen(put)
    assert reg.get(None).id == "qwen_image_2.1_int8_convrot"
    monkeypatch.setenv("PINAXIUM_DEFAULT_MODEL", "qwen_image_2.1_turbo_int8_convrot")
    assert reg.get(None).id == "qwen_image_2.1_turbo_int8_convrot"


def test_unknown_model_id(setup):
    reg, put, _ = setup
    full_qwen(put)
    with pytest.raises(LookupError, match="unknown model"):
        reg.get("nope")


def test_unsupported_architecture_and_loras_are_listed_not_offered(setup):
    reg, put, _ = setup
    full_qwen(put)
    put("diffusion_models", "some_flux_thing.safetensors")
    put("loras", "style.safetensors")
    s = reg.summary()
    assert "some_flux_thing" not in {m["id"] for m in s["models"]}
    notes = {o["file"]: o["note"] for o in s["other_files"]}
    assert "Flux" in notes["some_flux_thing.safetensors"] and "run_workflow" in notes["some_flux_thing.safetensors"]
    assert "LoRA" in notes["style.safetensors"]
    assert "qwen3.5_9b_pe.safetensors" in notes and "qwen3vl_8b_bf16.safetensors" in notes


def test_cache_only_detects_new_or_changed_files(setup):
    reg, put, calls = setup
    full_qwen(put)
    reg.models()
    assert len(calls) == 1 and len(calls[0]) == 6
    reg.models()
    assert len(calls) == 1, "rescanned unchanged files"
    put("text_encoders", "qwen3vl_8b_bf16.safetensors", size=20)      # changed size
    reg.models()
    assert calls[-1] == ["qwen3vl_8b_bf16.safetensors"]


def test_file_growing_during_detection_keeps_its_result(tmp_path, monkeypatch):
    """A download in progress changes size between scan and detection; the result must still be used."""
    monkeypatch.setenv("PINAXIUM_COMFY_DIR", str(tmp_path / "comfy"))
    monkeypatch.setenv("PINAXIUM_STATE_DIR", str(tmp_path / "state"))
    d = tmp_path / "comfy" / "models" / "diffusion_models"
    d.mkdir(parents=True)
    f = d / "z_image_turbo_int8_convrot.safetensors"
    f.write_bytes(b"x" * 10)

    def detector(items):
        with open(f, "ab") as fh:
            fh.write(b"more")                         # the download continues while ComfyUI inspects it
        return [{"arch": "ZImage", "params_b": 6.2, "error": None} for _ in items]

    (info,) = Registry(load(), detector).scan_files()
    assert info.arch == "ZImage"


def test_loras_are_not_sent_to_the_detector(setup):
    reg, put, calls = setup
    put("loras", "style.safetensors")
    reg.scan_files()
    assert calls == []


def zimage_and_klein(put):
    put("diffusion_models", "z_image_turbo_int8_convrot.safetensors")
    put("diffusion_models", "flux-2-klein-4b.safetensors")
    put("text_encoders", "qwen_3_4b.safetensors")
    put("vae", "sd3_vae_fp16.safetensors")
    put("vae", "ae.safetensors")
    put("vae", "flux2-vae.safetensors")


def test_zimage_and_klein_share_text_encoder_and_pick_their_vaes(setup):
    reg, put, _ = setup
    zimage_and_klein(put)
    z = reg.get("z_image_turbo_int8_convrot")
    k = reg.get("flux-2-klein-4b")
    assert z.files == {"unet": "z_image_turbo_int8_convrot.safetensors", "clip": "qwen_3_4b.safetensors",
                       "vae": "ae.safetensors"}                      # preferred over the same-shaped sd3_vae
    assert k.files == {"unet": "flux-2-klein-4b.safetensors", "clip": "qwen_3_4b.safetensors",
                       "vae": "flux2-vae.safetensors"}
    assert (z.builder, k.builder) == ("z_image", "flux2_klein")
    assert (z.defaults["steps"], z.defaults["cfg"]) == (8, 1.0)
    assert (k.defaults["steps"], k.defaults["cfg"]) == (4, 1.0)
    assert not z.capabilities["edit"] and k.capabilities["edit"]


def test_variant_defaults_follow_official_templates(setup):
    reg, put, _ = setup
    zimage_and_klein(put)
    put("diffusion_models", "z_image_bf16.safetensors")
    put("diffusion_models", "flux-2-klein-base-4b.safetensors")
    assert (reg.get("z_image_bf16").defaults["steps"], reg.get("z_image_bf16").defaults["cfg"]) == (25, 4.0)
    kb = reg.get("flux-2-klein-base-4b").defaults
    assert (kb["steps"], kb["cfg"], kb["variant"]) == (20, 5.0, "base")


def test_same_class_larger_model_is_not_offered_as_klein_4b(setup):
    reg, put, _ = setup
    zimage_and_klein(put)
    put("diffusion_models", "flux-2-klein-9b.safetensors")
    assert "flux-2-klein-9b" not in {m.id for m in reg.models()}
    notes = {o["file"]: o["note"] for o in reg.summary()["other_files"]}
    assert "Flux2 (9.1B)" in notes["flux-2-klein-9b.safetensors"]


def test_default_model_order_is_family_order(setup):
    reg, put, _ = setup
    zimage_and_klein(put)
    full_qwen(put)
    assert reg.get(None).id == "qwen_image_2.1_int8_convrot"


def test_cache_from_an_older_version_is_ignored(setup):
    import json
    from pinaxium.registry import CACHE_VERSION
    reg, put, calls = setup
    full_qwen(put)
    planted = {}
    for f in os.scandir(os.path.join(reg.cfg.comfy_dir, "models", "diffusion_models")):
        st = os.stat(f.path)
        planted[f"{f.path}|diffusion|{st.st_size}|{st.st_mtime_ns}"] = {"arch": "Bogus", "params_b": 1, "error": None}
    os.makedirs(os.path.dirname(reg.cache_path), exist_ok=True)
    with open(reg.cache_path, "w") as fh:
        json.dump({"version": CACHE_VERSION - 1, "entries": planted}, fh)
    assert reg.get(None).id == "qwen_image_2.1_int8_convrot", "stale cache entries were trusted"
    assert len(calls) == 1 and len(calls[0]) == 6
