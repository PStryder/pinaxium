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


LINK_DESCRIPTIONS = {
    "file_uri": "file:/// link to the image on this machine's disk: give it to the user as a clickable link",
    "local_url": "http://127.0.0.1 link served by the Pinaxium service, opens in any browser on this machine; "
                 "null when the service isn't running",
}


class DraftEntry(BaseModel):
    uri: str = Field(description="The draft's URI, pinaxium://projects/<project>/drafts/<file>: readable as a "
                                 "resource, usable wherever an image is accepted")
    file_uri: str = Field(description=LINK_DESCRIPTIONS["file_uri"])
    local_url: str | None = Field(None, description=LINK_DESCRIPTIONS["local_url"])
    path: str = Field(description="Full path of the PNG")
    project: str = Field(description="Project the draft belongs to")
    final_uri: str | None = Field(None, description="If this draft was finalized: its final's URI")
    model: str | None = Field(None, description="Model that made it, when known")
    prompt: str | None = Field(None, description="Prompt or instruction that made it, when known")
    seed: int | None = Field(None, description="Seed used, when known")
    modified: str = Field(description="Local time, YYYY-MM-DD HH:MM:SS")
    bytes: int = Field(description="File size in bytes")


class DraftList(BaseModel):
    drafts: list[DraftEntry] = Field(description="This page, newest first")
    total: int = Field(description="How many drafts match in all")
    next_offset: int | None = Field(None, description="Pass as offset for the next page; null on the last page")


class FinalEntry(BaseModel):
    id: str = Field(description="The final's permanent id within its project, e.g. portrait-001")
    uri: str = Field(description="Permanent URI, pinaxium://projects/<project>/finals/<id>: readable as a resource, "
                                 "usable wherever an image is accepted")
    file_uri: str | None = Field(None, description=LINK_DESCRIPTIONS["file_uri"] + " (null if the file can't be found)")
    local_url: str | None = Field(None, description=LINK_DESCRIPTIONS["local_url"])
    project: str = Field(description="Project the final belongs to")
    title: str | None = Field(None, description="Title given at finalize_image")
    description: str | None = Field(None, description="Description given at finalize_image (by default the prompt)")
    tags: list[str] = Field(description="Tags given at finalize_image")
    created: str | None = Field(None, description="When it was finalized, local time")
    model: str | None = Field(None, description="Model that made it")
    prompt: str | None = Field(None, description="Prompt or instruction that made it")
    seed: int | None = Field(None, description="Seed used")
    derived_from: str | None = Field(None, description="URI of the final it was derived from, if any")
    source_draft: str | None = Field(None, description="URI of the draft it was finalized from")
    path: str = Field(description="Where the file is now (renames and moves inside the finals folder are "
                                  "followed); informational, refer to the final by its URI")
    exists: bool = Field(description="False if the file can't be found (moved outside the finals folder or "
                                     "deleted): relink_final it")
    sheet_number: int | None = Field(None, description="With thumbnails: the number of this final's tile on the "
                                                       "contact sheet")


class FinalList(BaseModel):
    finals: list[FinalEntry] = Field(description="This page, newest first")
    total: int = Field(description="How many finals match in all")
    next_offset: int | None = Field(None, description="Pass as offset for the next page; null on the last page")


class ProjectEntry(BaseModel):
    project: str = Field(description="Project name (pass as `project`)")
    uri: str = Field(description="The project's resource: its drafts and finals with provenance")
    drafts: int = Field(description="Working images in the project")
    finals: int = Field(description="Finals registered in the project")
    draft_bytes: int = Field(description="Disk space used by the drafts, bytes")
    last_activity: str | None = Field(None, description="Newest image's time, local, YYYY-MM-DD HH:MM:SS")


class ProjectList(BaseModel):
    projects: list[ProjectEntry] = Field(description="All projects, by name")
    finals_dir: str = Field(description="Folder holding every project's finals (setting finals_dir)")
    draft_retention_days: float = Field(description="Unfinalized drafts older than this go to the recycle bin "
                                                    "automatically; 0 = never")


class FinalizeResult(BaseModel):
    project: str = Field(description="Project of the final")
    id: str = Field(description="The final's permanent id")
    uri: str = Field(description="The final's permanent URI")
    file_uri: str = Field(description=LINK_DESCRIPTIONS["file_uri"])
    local_url: str | None = Field(None, description=LINK_DESCRIPTIONS["local_url"])
    path: str = Field(description="Where the final's file is")
    already_final: bool = Field(description="True if this draft had already been finalized (nothing copied; "
                                            "the existing final is returned)")


class NotDeleted(BaseModel):
    image: str = Field(description="The image or file that was not deleted")
    reason: str = Field(description="Why")


class DeleteResult(BaseModel):
    deleted: list[str] = Field(description="Files sent to the recycle bin")
    not_deleted: list[NotDeleted] = Field(description="Files left in place, with the reason")
    note: str = Field(description="Where deleted files went")
