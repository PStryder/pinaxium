"""The generated reference matches what the server advertises, and the hand-written docs only name tools,
resources and config keys that exist."""

import os
import re

from pinaxium.config import FIELDS
from tools_docs import TARGET, generate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HAND_WRITTEN = ["README.md", os.path.join("docs", "GUIDE.md"), os.path.join("docs", "CONFIGURATION.md"),
                os.path.join("docs", "DEVELOPMENT.md")]


def test_reference_is_up_to_date():
    with open(TARGET, encoding="utf-8") as f:
        current = f.read()
    assert current == generate(), "docs/REFERENCE.md is stale: run `uv run python tools_docs.py`"


USER_FACING = ["README.md", os.path.join("docs", "GUIDE.md"), os.path.join("docs", "CONFIGURATION.md")]
# snake_case names the user-facing docs may use that aren't tools, parameters, result fields or settings
OTHER_NAMES = {"python_embeded", "claude_desktop_config", "diffusion_models", "text_encoders", "qwen_3_4b",
               "qwen3vl_8b", "qwen_image_2", "z_image_turbo", "flux2_vae", "state_json", "scan_cache",
               "spawn_env", "engine_lock", "comfyui_log", "supervisor_log"}


def _reference() -> str:
    with open(TARGET, encoding="utf-8") as f:
        return f.read()


def test_user_facing_docs_name_only_real_tools_parameters_fields_and_settings():
    """Catches docs that keep naming a tool, parameter or field after it was renamed or removed."""
    ref = _reference()
    known = set(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", ref)) | set(FIELDS) | OTHER_NAMES
    for rel in USER_FACING:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            text = f.read()
        for name in re.findall(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`", text):
            assert name in known, f"{rel}: `{name}` is not a tool, parameter, result field or setting"


def _uri_patterns(ref: str) -> list[re.Pattern]:
    """The server's resources and templates (from the reference) as patterns; {param} matches one segment."""
    static = re.findall(r"^\| `(pinaxium://[^`{]+)` \|", ref, re.M)
    templates = re.findall(r"^\| `(pinaxium://[^`]*\{[^`]+)` \(template\)", ref, re.M)
    pats = [re.compile(re.escape(u)) for u in static]
    pats += [re.compile(re.sub(r"\\\{\w+\\\}", "[^/]+", re.escape(t))) for t in templates]
    return pats


def test_docs_name_only_real_resources_and_environment_variables():
    ref = _reference()
    patterns = _uri_patterns(ref)
    assert len(patterns) >= 6
    envs = {env for env, _, _ in FIELDS.values()} | {"PINAXIUM_CONFIG", "PINAXIUM_ENGINE_CMD", "PINAXIUM_TRASH_DIR"}
    for rel in HAND_WRITTEN:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            text = f.read()
        for raw in re.findall(r"pinaxium://[^\s`'\")\]|,]+", text):
            if raw.endswith("/..."):                                   # a group: "pinaxium://logs/..."
                group = raw[:-3]
                assert any(p.pattern.startswith(re.escape(group)) for p in patterns), f"{rel}: unknown resource {raw}"
                continue
            uri = raw.rstrip(".;:")
            concrete = re.sub(r"<[^>/]+>", "x", uri).replace("...", "x")   # placeholders stand for one segment
            assert any(p.fullmatch(concrete) for p in patterns), f"{rel}: unknown resource {uri}"
        for env in re.findall(r"PINAXIUM_[A-Z_]+[A-Z]", text):
            assert env in envs, f"{rel}: unknown environment variable {env}"
        if rel.endswith("CONFIGURATION.md"):
            for key in re.findall(r"^\| `(\w+)` \| `PINAXIUM_", text, re.M):
                assert key in FIELDS, f"{rel}: unknown config key {key}"
