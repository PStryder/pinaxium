"""Typed results for the JSON-returning tools, so they advertise an outputSchema and return structuredContent."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EngineStatus(BaseModel):
    engine_url: str = Field(description="ComfyUI HTTP endpoint")
    idle_shutdown_minutes: float = Field(description="The engine stops after this long without requests or jobs")
    status: Literal["stopped", "starting", "ready", "external"] = Field(
        description="external: a ComfyUI this service didn't start is on the port; it is used but never shut down")
    ready_to_render: bool
    busy: bool | None = Field(None, description="A render is running")
    queued_jobs: int | None = None
    gpu: str | None = None
    vram_used_gb: float | None = Field(None, description="Whole-GPU memory in use, all processes")
    vram_total_gb: float | None = None
    comfyui_version: str | None = None
    models_loaded_estimate: bool | None = Field(
        None, description="GPU memory beyond the engine's idle baseline suggests models are loaded")
    vram_used_by_models_gb: float | None = None
    idle_minutes: float | None = Field(None, description="Minutes since the last request")
    last_stop_reason: str | None = None
    note: str | None = None
    config_file: str | None = Field(None, description="Settings file in use (see `python -m pinaxium.config`)")
    setup_problems: list[str] | None = Field(None, description="What prevents the engine from starting, and how to "
                                                               "fix it")


class ModelDefaults(BaseModel):
    variant: str = Field(description="base, turbo, distilled, ...")
    steps: int
    cfg: float


class ModelCapabilities(BaseModel):
    text_to_image: bool
    edit: bool
    max_side: int = Field(description="Largest supported image side in pixels")
    max_reference_images: int | None = Field(None, description="For edit_image")
    transparency: bool = Field(False, description="Supports transparent=True")
    text_rendering: str | None = Field(None, description="How well it renders text in images")


class ModelFiles(BaseModel):
    unet: str = Field(description="Diffusion model file")
    clip: str | None = Field(description="Text encoder file")
    vae: str | None = Field(description="VAE file")


class ModelInfo(BaseModel):
    id: str = Field(description="Pass as `model` to generate_image / edit_image")
    family: str
    available: bool
    defaults: ModelDefaults
    capabilities: ModelCapabilities
    files: ModelFiles
    license: str
    missing: list[str] = Field(default_factory=list, description="Companion files needed before it can be used")


class OtherFile(BaseModel):
    file: str
    folder: str
    size_gb: float
    note: str = Field(description="Why it isn't offered as a model")


class ModelList(BaseModel):
    models: list[ModelInfo] = Field(description="Default model first")
    other_files: list[OtherFile]


class ImageEntry(BaseModel):
    path: str
    modified: str = Field(description="Local time, YYYY-MM-DD HH:MM:SS")
    bytes: int


class ImageList(BaseModel):
    images: list[ImageEntry] = Field(description="Newest first")
