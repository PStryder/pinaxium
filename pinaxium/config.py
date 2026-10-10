"""Settings, from environment variables with defaults for this machine."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    comfy_dir: str
    host: str
    port: int
    idle_minutes: float
    state_dir: str
    start_timeout_s: float

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def comfy_python(self) -> str:
        return os.path.join(self.comfy_dir, ".venv", "Scripts", "python.exe")

    @property
    def output_dir(self) -> str:
        return os.path.join(self.comfy_dir, "output")

    def state_path(self, name: str) -> str:
        return os.path.join(self.state_dir, name)


def load() -> Config:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return Config(
        comfy_dir=_env("PINAXIUM_COMFY_DIR", os.path.join("F:" + os.sep, "ComfyUI")),
        host="127.0.0.1",
        port=int(_env("PINAXIUM_PORT", "8188")),
        idle_minutes=float(_env("PINAXIUM_IDLE_MINUTES", "10")),
        state_dir=_env("PINAXIUM_STATE_DIR", os.path.join(here, "state")),
        start_timeout_s=float(_env("PINAXIUM_START_TIMEOUT_S", "180")),
    )
