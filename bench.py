"""Benchmark every usable model: engine boot, cold load + render, warm renders, 2048 render, unload.

    uv run python bench.py [--warm N] [--models id,id]

Uses the same registry, workflow builders and engine as the MCP server. Writes results/bench-<stamp>.json,
results/bench-<stamp>.md and a contact sheet of the cold renders.

Caveat recorded in the report: "cold" means not in VRAM and not in ComfyUI's cache. The model files may
still be in the OS file cache, so a first load after a reboot can be slower.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import time

import httpx
from PIL import Image, ImageDraw

from pinaxium import supervisor as sup
from pinaxium import workflows as wf
from pinaxium.config import load
from pinaxium.engine import Engine
from pinaxium.registry import Registry

PROMPT = ('A rainy neon-lit street market at night in Kowloon, a noodle stall with a hand-painted sign reading '
          '"PINAXIUM NOODLES", steam rising, reflections on wet pavement, people with umbrellas, cinematic photo')
GB = 1024 ** 3
HERE = os.path.dirname(os.path.abspath(__file__))


def gpu_used_gb() -> float:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.strip().splitlines()[0]
    return round(int(out) / 1024, 2)


class Bench:
    def __init__(self):
        self.cfg = load()
        self.eng = Engine(self.cfg)
        self.reg = Registry(self.cfg)
        self.baseline = 0.0          # GPU memory in use with the engine idle; set after boot

    async def stats(self) -> dict:
        async with httpx.AsyncClient(base_url=self.cfg.base_url, timeout=5) as c:
            return (await c.get("/system_stats")).json()["devices"][0]

    async def used_gb(self) -> float:
        """Whole-GPU memory in use. torch's own counters read ~0 under cudaMallocAsync, which ComfyUI uses."""
        s = await self.stats()
        return (s["vram_total"] - s["vram_free"]) / GB

    async def engine_gb(self) -> float:
        return max(0.0, await self.used_gb() - self.baseline)

    async def stop_engine(self) -> None:
        if self.eng.request_stop():
            while sup.lock_is_fresh(self.cfg) or await self.eng.api_up():
                await asyncio.sleep(0.25)

    async def boot(self) -> float:
        await self.stop_engine()
        t0 = time.perf_counter()
        await self.eng.ensure_ready()
        return time.perf_counter() - t0

    async def unload(self) -> float:
        """POST /free (unload + clear node cache) and time until GPU memory is back within 0.5 GB of the
        engine's idle baseline. Returns -1 if that doesn't happen within 30 s."""
        t0 = time.perf_counter()
        await self.eng.free_vram()
        while await self.engine_gb() > 0.5:
            if time.perf_counter() - t0 > 30:
                return -1.0
            await asyncio.sleep(0.1)
        return time.perf_counter() - t0

    async def render(self, m, width: int, height: int, seed: int) -> dict:
        p = wf.resolve(m.defaults, None, seed, None)
        graph = wf.BUILDERS[m.builder].text_to_image(m.files, PROMPT, "", width, height, p)
        peak = {"engine": 0.0}
        done = asyncio.Event()

        async def sample():
            while not done.is_set():
                try:
                    peak["engine"] = max(peak["engine"], await self.engine_gb())
                except Exception:
                    pass
                await asyncio.sleep(0.2)

        sampler = asyncio.create_task(sample())
        t0 = time.perf_counter()
        job = await self.eng.submit(graph)
        j = await self.eng.wait(job, 900)
        elapsed = time.perf_counter() - t0
        done.set()
        await sampler
        if j["state"] != "done":
            raise RuntimeError(f"{m.id}: {j['state']} {j.get('error')}")
        return {"seconds": round(elapsed, 2), "image": j["images"][0], "size": f"{width}x{height}",
                "steps": p.steps, "peak_engine_gb": round(peak["engine"], 2)}


async def main(warm_n: int, only: list[str] | None) -> None:
    b = Bench()
    models = [m for m in b.reg.models() if m.available and (not only or m.id in only)]
    print("models:", [m.id for m in models], flush=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = {"stamp": stamp, "prompt": PROMPT, "gpu": None, "idle_gpu_used_gb_before": None, "boots_s": [],
              "models": []}

    await b.stop_engine()
    report["idle_gpu_used_gb_before"] = gpu_used_gb()
    for _ in range(2):
        report["boots_s"].append(round(await b.boot(), 2))
        print("boot", report["boots_s"][-1], flush=True)
    report["gpu"] = (await b.stats())["name"]
    report["engine_idle_gpu_used_gb"] = gpu_used_gb()
    b.baseline = await b.used_gb()

    for m in models:
        r = {"id": m.id, "family": m.family, "variant": m.defaults.get("variant"), "files": m.files}
        r["unload_before_s"] = round(await b.unload(), 2)
        r["cold"] = await b.render(m, 1024, 1024, 1)
        print(m.id, "cold", r["cold"]["seconds"], flush=True)
        r["warm"] = [await b.render(m, 1024, 1024, 2 + i) for i in range(warm_n)]
        warm_s = [w["seconds"] for w in r["warm"]]
        r["warm_median_s"] = round(statistics.median(warm_s), 2)
        r["load_estimate_s"] = round(r["cold"]["seconds"] - r["warm_median_s"], 2)
        print(m.id, "warm", warm_s, flush=True)
        if m.capabilities.get("max_side", 1024) >= 2048:
            r["warm_2048"] = await b.render(m, 2048, 2048, 99)
            print(m.id, "2048", r["warm_2048"]["seconds"], flush=True)
        r["resident_after_gb"] = round(await b.engine_gb(), 2)
        r["unload_s"] = round(await b.unload(), 2)
        print(m.id, "unload", r["unload_s"], flush=True)
        report["models"].append(r)

    await b.stop_engine()
    report["gpu_used_gb_after_stop"] = gpu_used_gb()

    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    base = os.path.join(HERE, "results", f"bench-{stamp}")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    sheet(report, base + "-sheet.jpg")
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(markdown(report))
    print(markdown(report))
    print("wrote", base + ".json / .md / -sheet.jpg")


def sheet(report: dict, path: str) -> None:
    tiles = []
    for r in report["models"]:
        with Image.open(r["cold"]["image"]) as im:
            t = im.convert("RGB").resize((512, 512))
        ImageDraw.Draw(t).rectangle((0, 0, 512, 22), fill=(0, 0, 0))
        ImageDraw.Draw(t).text((6, 5), r["id"], fill=(255, 255, 255))
        tiles.append(t)
    if not tiles:
        return
    out = Image.new("RGB", (512 * len(tiles), 512))
    for i, t in enumerate(tiles):
        out.paste(t, (512 * i, 0))
    out.save(path, quality=90)


def markdown(r: dict) -> str:
    lines = [f"# Image model benchmark ({r['stamp']})", "",
             f"GPU: {r['gpu']}. Engine boot (ComfyUI start to API ready): {', '.join(f'{x:.1f}' for x in r['boots_s'])} s. "
             f"GPU memory in use with the engine stopped: {r['idle_gpu_used_gb_before']} GB before, "
             f"{r['gpu_used_gb_after_stop']} GB after; with the engine idle: {r['engine_idle_gpu_used_gb']} GB.", "",
             "All renders: same prompt, 1024x1024 unless noted, each model's default steps.", "",
             "| model | steps | cold (load + render) | load (est.) | warm 1024² median | warm 2048² | peak VRAM (engine) | resident after | unload |",
             "|---|---|---|---|---|---|---|---|---|"]
    for m in r["models"]:
        big = f"{m['warm_2048']['seconds']:.1f} s" if "warm_2048" in m else "—"
        peak = max([m["cold"]["peak_engine_gb"]] + [w["peak_engine_gb"] for w in m["warm"]]
                   + ([m["warm_2048"]["peak_engine_gb"]] if "warm_2048" in m else []))
        unload = f"{m['unload_s']:.1f} s" if m["unload_s"] >= 0 else "> 30 s"
        lines.append(f"| {m['id']} | {m['cold']['steps']} | {m['cold']['seconds']:.1f} s | {m['load_estimate_s']:.1f} s | "
                     f"{m['warm_median_s']:.1f} s | {big} | {peak:.1f} GB | {m['resident_after_gb']:.1f} GB | {unload} |")
    lines += ["", "Cold = not in VRAM and not in ComfyUI's cache; the files may still be in the OS file cache, "
              "so a first load after a reboot can take longer. Load (est.) = cold minus warm median. "
              "VRAM figures are whole-GPU memory above the idle engine's baseline, sampled every 0.2 s. "
              "Unload = POST /free until GPU memory is back within 0.5 GB of that baseline."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--warm", type=int, default=3)
    ap.add_argument("--models", default="")
    a = ap.parse_args()
    asyncio.run(main(a.warm, [x for x in a.models.split(",") if x] or None))
