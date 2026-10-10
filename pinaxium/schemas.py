"""Typed results for the JSON-returning tools, so they advertise an outputSchema and return structuredContent.
Every field carries a description: docs/REFERENCE.md is generated from these, and tests/test_catalogue.py checks
that none is missing."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EngineStatus(BaseModel):
    engine_url: str = Field(description="ComfyUI HTTP endpoint the engine uses")
    idle_shutdown_minutes: float = Field(description="The engine stops after this long without requests or jobs")
    status: Literal["stopped", "starting", "ready", "external"] = Field(
        description="stopped: not running (starts on the next request); starting: booting; ready: running and "
                    "started by this server; external: a ComfyUI this server didn't start is on the port, used "
                    "but never shut down")
    ready_to_render: bool = Field(description="True when renders can be submitted now (status ready or external)")
    busy: bool | None = Field(None, description="A render is running (only while the engine is up)")
    queued_jobs: int | None = Field(None, description="Jobs waiting behind the running one")
    gpu: str | None = Field(None, description="GPU name as ComfyUI reports it")
    vram_used_gb: float | None = Field(None, description="Whole-GPU memory in use by all processes, GB")
    vram_total_gb: float | None = Field(None, description="Total GPU memory, GB")
    comfyui_version: str | None = Field(None, description="Version of the running ComfyUI")
    models_loaded_estimate: bool | None = Field(
        None, description="True when GPU memory beyond the engine's idle baseline suggests models are loaded "
                          "(an estimate: other programs' GPU memory counts too)")
    vram_used_by_models_gb: float | None = Field(None, description="GPU memory above the idle engine's baseline, GB")
    idle_minutes: float | None = Field(None, description="Minutes since the last request (managed engine only)")
    last_stop_reason: str | None = Field(None, description="Why the engine last stopped, e.g. idle, stop requested, "
                                                           "or a startup failure")
    note: str | None = Field(None, description="Extra information about the engine's state")
    config_file: str | None = Field(None, description="Settings file in use (see `python -m pinaxium.config`)")
    setup_problems: list[str] | None = Field(None, description="What prevents the engine from starting, and how to "
                                                               "fix it")


class ModelDefaults(BaseModel):
    variant: str = Field(description="Which version of the family this file is: base, turbo or distilled")
    steps: int = Field(description="Sampling steps used when a request doesn't set steps")
    cfg: float = Field(description="Guidance scale used when a request doesn't set cfg_scale")


class ModelCapabilities(BaseModel):
    text_to_image: bool = Field(description="Works with generate_image")
    edit: bool = Field(description="Works with edit_image")
    max_side: int = Field(description="Largest supported image side in pixels")
    max_reference_images: int | None = Field(None, description="Most reference images edit_image accepts")
    transparency: bool = Field(False, description="Supports transparent=True")
    text_rendering: str | None = Field(None, description="How reliably it renders lettering: strong, good or weak")


class ModelFiles(BaseModel):
    unet: str = Field(description="Diffusion model file")
    clip: str | None = Field(description="Text encoder file (null if missing)")
    vae: str | None = Field(description="VAE file (null if missing)")


class ModelInfo(BaseModel):
    id: str = Field(description="Pass as `model` to generate_image / edit_image")
    family: str = Field(description="Model family, e.g. Qwen-Image 2.1")
    available: bool = Field(description="All files present; usable now")
    defaults: ModelDefaults = Field(description="Settings used unless a request overrides them")
    capabilities: ModelCapabilities = Field(description="What the model can do")
    files: ModelFiles = Field(description="Files the model runs with")
    license: str = Field(description="The model's license (Pinaxium itself is MIT)")
    notes: str = Field("", description="How it compares: strengths, limits and relative speed, to choose a model")
    missing: list[str] = Field(default_factory=list, description="Companion files needed before it can be used")


class OtherFile(BaseModel):
    file: str = Field(description="File name")
    folder: str = Field(description="ComfyUI models subfolder it is in")
    size_gb: float = Field(description="File size, GB")
    note: str = Field(description="Why it isn't offered as a model")


class ModelList(BaseModel):
    models: list[ModelInfo] = Field(description="Usable and not-yet-usable models, default model first")
    other_files: list[OtherFile] = Field(description="Model files that aren't part of a usable model")


class ImageEntry(BaseModel):
    path: str = Field(description="Full path of the saved PNG")
    modified: str = Field(description="Local time, YYYY-MM-DD HH:MM:SS")
    bytes: int = Field(description="File size in bytes")


class ImageList(BaseModel):
    images: list[ImageEntry] = Field(description="Newest first")
