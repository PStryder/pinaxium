import pytest

from pinaxium import workflows as wf

QWEN_FILES = {"unet": "q_base.safetensors", "clip": "q_te.safetensors", "vae": "q_vae.safetensors"}
QWEN_DEFAULTS = {"steps": 25, "cfg": 1.0}


def by_class(g, cls):
    return [n for n in g.values() if n["class_type"] == cls]


def links_resolve(g):
    for n in g.values():
        for v in n["inputs"].values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[1], int):
                assert v[0] in g, f"dangling link to {v[0]}"


def test_snap_clamps_and_rounds():
    assert wf.snap(1000) == 1008
    assert wf.snap(1024) == 1024
    assert wf.snap(10) == 256
    assert wf.snap(5000) == 2048
    assert wf.snap(1031) == 1024


def test_resolve_uses_model_defaults_unless_overridden():
    p = wf.resolve({"steps": 8, "cfg": 1.0}, None, 5, None)
    assert (p.steps, p.cfg, p.seed) == (8, 1.0, 5)
    p = wf.resolve({"steps": 8, "cfg": 1.0}, 30, 5, 3.5)
    assert (p.steps, p.cfg) == (30, 3.5)


def test_qwen_text_to_image_graph_uses_registry_files():
    p = wf.resolve(QWEN_DEFAULTS, 30, 123, 2.5)
    g = wf.QwenImage21.text_to_image(QWEN_FILES, "a red fox", "blurry", 1000, 600, p)
    assert by_class(g, "UNETLoader")[0]["inputs"]["unet_name"] == "q_base.safetensors"
    assert by_class(g, "CLIPLoader")[0]["inputs"] == {"clip_name": "q_te.safetensors", "type": "qwen_image", "device": "default"}
    assert by_class(g, "VAELoader")[0]["inputs"]["vae_name"] == "q_vae.safetensors"
    lat = by_class(g, "EmptyLatentImage")[0]["inputs"]
    assert (lat["width"], lat["height"]) == (1008, 608)
    ks = by_class(g, "KSampler")[0]["inputs"]
    assert (ks["seed"], ks["steps"], ks["cfg"]) == (123, 30, 2.5)
    assert ks["latent_image"] == ["latent", 0] and ks["negative"] == ["encode", 1]
    enc = by_class(g, "TextEncodeQwenImage21")[0]["inputs"]
    assert (enc["prompt"], enc["negative_prompt"]) == ("a red fox", "blurry")
    assert by_class(g, "SaveImage")[0]["inputs"]["filename_prefix"].startswith(wf.OUTPUT_SUBFOLDER + "/")
    links_resolve(g)


def test_qwen_edit_graph_wires_references_and_uses_encoder_latent():
    p = wf.resolve(QWEN_DEFAULTS, None, 1, None)
    g = wf.QwenImage21.edit(QWEN_FILES, "swap the jacket", "", ["mcp/a.png", "mcp/b.png"], 1500, p)
    enc = by_class(g, "TextEncodeQwenImage21")[0]["inputs"]
    assert enc["images.image_1"] == ["ref1", 0] and enc["images.image_2"] == ["ref2", 0]
    assert g["ref1"]["inputs"]["image"] == "mcp/a.png" and g["ref2"]["inputs"]["image"] == "mcp/b.png"
    assert enc["vae"] == ["vae", 0] and enc["resolution"] == 1504
    assert by_class(g, "KSampler")[0]["inputs"]["latent_image"] == ["encode", 2]
    assert not by_class(g, "EmptyLatentImage")
    links_resolve(g)


@pytest.mark.parametrize("call", [
    lambda: wf.resolve(QWEN_DEFAULTS, 0, None, None),
    lambda: wf.resolve(QWEN_DEFAULTS, None, None, -1.0),
    lambda: wf.QwenImage21.text_to_image(QWEN_FILES, "  ", "", 512, 512, wf.resolve(QWEN_DEFAULTS, None, 1, None)),
    lambda: wf.QwenImage21.edit(QWEN_FILES, "x", "", [], 1024, wf.resolve(QWEN_DEFAULTS, None, 1, None)),
    lambda: wf.QwenImage21.edit(QWEN_FILES, "x", "", ["a"] * 11, 1024, wf.resolve(QWEN_DEFAULTS, None, 1, None)),
])
def test_rejects_bad_input(call):
    with pytest.raises(ValueError):
        call()


Z_FILES = {"unet": "z.safetensors", "clip": "q3.safetensors", "vae": "ae.safetensors"}
K_FILES = {"unet": "k.safetensors", "clip": "q3.safetensors", "vae": "f2vae.safetensors"}


def test_zimage_turbo_graph_matches_template():
    p = wf.resolve({"steps": 8, "cfg": 1.0}, None, 9, None)
    g = wf.ZImage.text_to_image(Z_FILES, "a lighthouse", "ignored at cfg 1", 1280, 720, p)
    assert by_class(g, "CLIPLoader")[0]["inputs"]["type"] == "lumina2"
    assert by_class(g, "ModelSamplingAuraFlow")[0]["inputs"]["shift"] == 3.0
    ks = by_class(g, "KSampler")[0]["inputs"]
    assert (ks["steps"], ks["cfg"], ks["sampler_name"], ks["scheduler"]) == (8, 1.0, "res_multistep", "simple")
    assert ks["model"] == ["shift", 0]
    assert g["neg"]["class_type"] == "ConditioningZeroOut"
    lat = by_class(g, "EmptySD3LatentImage")[0]["inputs"]
    assert (lat["width"], lat["height"]) == (1280, 720)
    links_resolve(g)


def test_negative_prompt_is_encoded_only_above_cfg_1():
    p = wf.resolve({"steps": 25, "cfg": 4.0}, None, 9, None)
    g = wf.ZImage.text_to_image(Z_FILES, "a lighthouse", "fog", 1024, 1024, p)
    assert g["neg"] == {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": "fog"}}
    g = wf.Flux2Klein.text_to_image(K_FILES, "x", "fog", 1024, 1024, wf.resolve({"steps": 20, "cfg": 5.0}, None, 1, None))
    assert g["neg"]["class_type"] == "CLIPTextEncode" and g["guider"]["inputs"]["cfg"] == 5.0


def test_klein_text_to_image_graph_matches_template():
    p = wf.resolve({"steps": 4, "cfg": 1.0}, None, 77, None)
    g = wf.Flux2Klein.text_to_image(K_FILES, "a hedgehog", "", 1024, 768, p)
    assert by_class(g, "CLIPLoader")[0]["inputs"]["type"] == "flux2"
    assert g["sigmas"]["inputs"] == {"steps": 4, "width": 1024, "height": 768}
    assert g["latent"]["inputs"] == {"width": 1024, "height": 768, "batch_size": 1}
    assert g["noise"]["inputs"]["noise_seed"] == 77 and g["ksampler"]["inputs"]["sampler_name"] == "euler"
    assert g["guider"]["inputs"]["negative"] == ["neg", 0] and g["neg"]["class_type"] == "ConditioningZeroOut"
    links_resolve(g)


def test_klein_edit_chains_reference_latents_into_both_conditionings():
    p = wf.resolve({"steps": 4, "cfg": 1.0}, None, 1, None)
    g = wf.Flux2Klein.edit(K_FILES, "make it blue", "", ["mcp/a.png", "mcp/b.png"], 1024, p)
    assert g["refpos1"]["inputs"] == {"conditioning": ["pos", 0], "latent": ["enc1", 0]}
    assert g["refpos2"]["inputs"]["conditioning"] == ["refpos1", 0]
    assert g["refneg2"]["inputs"]["conditioning"] == ["refneg1", 0] and g["refneg1"]["inputs"]["conditioning"] == ["neg", 0]
    assert g["guider"]["inputs"]["positive"] == ["refpos2", 0] and g["guider"]["inputs"]["negative"] == ["refneg2", 0]
    assert g["size"]["inputs"]["image"] == ["scale1", 0]
    assert g["latent"]["inputs"]["width"] == ["size", 0] and g["sigmas"]["inputs"]["height"] == ["size", 1]
    assert g["scale2"]["inputs"]["megapixels"] == 1.05
    links_resolve(g)


def test_qwen_drops_alpha_unless_transparency_is_asked_for():
    p = wf.resolve(QWEN_DEFAULTS, None, 1, None)
    for g in (wf.QwenImage21.text_to_image(QWEN_FILES, "x", "", 512, 512, p),
              wf.QwenImage21.edit(QWEN_FILES, "x", "", ["a.png"], 512, p)):
        assert g["rgb"] == {"class_type": "SplitImageWithAlpha", "inputs": {"image": ["decode", 0]}}
        assert g["save"]["inputs"]["images"] == ["rgb", 0]
    for g in (wf.QwenImage21.text_to_image(QWEN_FILES, "x", "", 512, 512, p, transparent=True),
              wf.QwenImage21.edit(QWEN_FILES, "x", "", ["a.png"], 512, p, transparent=True)):
        assert "rgb" not in g and g["save"]["inputs"]["images"] == ["decode", 0]


@pytest.mark.parametrize("call", [
    lambda p: wf.ZImage.text_to_image(Z_FILES, "x", "", 512, 512, p, transparent=True),
    lambda p: wf.Flux2Klein.text_to_image(K_FILES, "x", "", 512, 512, p, transparent=True),
    lambda p: wf.Flux2Klein.edit(K_FILES, "x", "", ["a.png"], 512, p, transparent=True),
])
def test_transparency_refused_by_rgb_only_families(call):
    with pytest.raises(ValueError, match="transparent"):
        call(wf.resolve({"steps": 4, "cfg": 1.0}, None, 1, None))


def test_every_family_has_a_builder():
    from pinaxium.registry import FAMILIES
    for fam in FAMILIES:
        b = wf.BUILDERS[fam.builder]
        assert b.supports_edit == bool(fam.capabilities.get("edit")), fam.name


def test_random_seed_when_omitted():
    assert len({wf.resolve(QWEN_DEFAULTS, None, None, None).seed for _ in range(5)}) > 1
