"""Hermetic by default: every load() in the test run (server subprocesses inherit the environment) sees a
throwaway config file, an empty ComfyUI folder, its own state folder and unused ports, so no test touches
the real install, engine or config unless it sets its own values (test_detector_real does, explicitly)."""

import os
import shutil
import socket
import tempfile

_tmp = tempfile.mkdtemp(prefix="pinaxium-test-")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pytest_configure(config):
    comfy = os.path.join(_tmp, "comfy")
    os.makedirs(os.path.join(comfy, "models"), exist_ok=True)
    os.environ.update({
        "PINAXIUM_CONFIG": os.path.join(_tmp, "config.toml"),
        "PINAXIUM_COMFY_DIR": comfy,
        "PINAXIUM_STATE_DIR": os.path.join(_tmp, "state"),
        "PINAXIUM_PORT": str(_free_port()),
        "PINAXIUM_SERVICE_PORT": str(_free_port()),                # never the real always-on service
        "PINAXIUM_FINALS_DIR": os.path.join(_tmp, "finals"),       # never the real Pictures folder
        "PINAXIUM_TRASH_DIR": os.path.join(_tmp, "trash"),         # never the real recycle bin
    })


def pytest_unconfigure(config):
    shutil.rmtree(_tmp, ignore_errors=True)
