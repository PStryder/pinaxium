"""Mutation check: each mutation removes one guarantee; the test suite must fail for every one.
Originals are restored afterwards, even on error. Writes mutation_check.json."""

import json
import subprocess
import sys

MUTATIONS = [
    ("supervisor.py", "never shuts down when idle",
     "if time.time() - idle_since > cfg.idle_minutes * 60:", "if False:"),
    ("supervisor.py", "client activity ignored for idle time",
     "max(last_busy, mtime(cfg.state_path(\"activity\")), started)", "max(last_busy, started)"),
    ("supervisor.py", "running job ignored for idle time",
     "            if queue_busy(cfg):\n                last_busy = time.time()\n", ""),
    ("supervisor.py", "stop flag ignored after startup",
     "            if os.path.exists(stop_flag):\n                reason = \"stop requested\"\n                return 0\n            if proc.poll() is not None:\n                reason = f\"engine exited with code",
     "            if proc.poll() is not None:\n                reason = f\"engine exited with code"),
    ("supervisor.py", "engine process not terminated on exit",
     "        if proc is not None:\n            stop_process(proc)", "        pass"),
    ("supervisor.py", "stale lock never taken over",
     "            if lock_is_fresh(cfg):\n                return False", "            return False"),
    ("supervisor.py", "fresh lock ignored", "            if lock_is_fresh(cfg):\n                return False\n", ""),
    ("supervisor.py", "no check for a ComfyUI already on the port",
     "        if api_get(cfg, \"/system_stats\") is not None:\n            reason = f\"port", "        if False:\n            reason = f\"port"),
    ("engine.py", "stop allowed on an external ComfyUI",
     "        if not self.managed():\n            return False\n        sup.touch", "        sup.touch"),
    ("engine.py", "start failure not reported", "            if state.get(\"status\") == \"stopped\"", "            if False"),
    ("engine.py", "supervisor spawned inside the client's job", "        if os.name == \"nt\":\n            pyw", "        if False:\n            pyw"),
    ("engine.py", "no respawn while a dead supervisor's lock looks alive",
     "            if not self.managed() and time.time() - spawned_at > 10:", "            if False:"),
    ("registry.py", "detection result stored under a re-read size",
     "            for (k, _), res in zip(todo, self.detect([item for _, item in todo])):\n                cache[k] = res",
     "            for (k, item), res in zip(todo, self.detect([item for _, item in todo])):\n"
     "                st = os.stat(item[\"path\"])\n"
     "                cache[f\"{item['path']}|{item['kind']}|{st.st_size}|{st.st_mtime_ns}\"] = res"),
    ("workflows.py", "qwen keeps the noisy alpha channel", "        if transparent:\n            g[\"save\"] = _save(\"qwen21\")",
     "        if True:\n            g[\"save\"] = _save(\"qwen21\")"),
    ("workflows.py", "qwen edit samples an empty square latent", '_ksampler(["cache", 0], p, ["encode", 2])',
     '_ksampler(["cache", 0], p, ["latent", 0])'),
    ("workflows.py", "size not clamped", "side = max(MIN_SIDE, min(MAX_SIDE, int(side)))", "side = int(side)"),
    ("workflows.py", "negative prompt encoded even at cfg 1", "    if p.cfg > 1.0:\n", "    if True:\n"),
    ("workflows.py", "z-image without the AuraFlow shift", '"model": ["shift", 0], "positive": ["pos", 0]',
     '"model": ["unet", 0], "positive": ["pos", 0]'),
    ("workflows.py", "klein edit references only the positive", 'g["guider"]["inputs"]["positive"], g["guider"]["inputs"]["negative"] = pos, neg',
     'g["guider"]["inputs"]["positive"] = pos'),
    ("workflows.py", "klein edit doesn't chain references", "            pos, neg = [f\"refpos{i}\", 0], [f\"refneg{i}\", 0]\n",
     "            pos, neg = [f\"refpos{i}\", 0], [f\"refneg{i}\", 0]\n            pos, neg = [\"refpos1\", 0], [\"refneg1\", 0]\n"),
    ("server.py", "open-world hint left unset", "                           openWorldHint=False)", "                           )"),
    ("server.py", "width range not declared", 'grows with pixel count.", ge=wf.MIN_SIDE, le=wf.MAX_SIDE)]',
     'grows with pixel count.")]'),
    ("server.py", "a parameter without a description",
     'Seed = Annotated[int | None, Field(description="Fix for reproducible results; random when omitted (the seed used "\n'
     '                                               "is reported).", ge=0)]',
     "Seed = Annotated[int | None, Field(ge=0)]"),
    ("server.py", "a tool without a title", '@mcp.tool(title="Cancel render", ', "@mcp.tool("),
    ("server.py", "list_models without an output schema",
     '@mcp.tool(title="List image models", annotations=_hints(read_only=True, idempotent=True))',
     '@mcp.tool(title="List image models", annotations=_hints(read_only=True, idempotent=True), structured_output=False)'),
    ("server.py", "empty server version", "    version=__version__,\n", ""),
    ("server.py", "read-only tool not marked read-only",
     '@mcp.tool(title="Engine status", annotations=_hints(read_only=True, idempotent=True))',
     '@mcp.tool(title="Engine status", annotations=_hints(read_only=False, idempotent=True))'),
    ("server.py", "run_workflow claimed closed-world",
     "annotations=_hints(read_only=False, destructive=True, idempotent=False, open_world=True)",
     "annotations=_hints(read_only=False, destructive=True, idempotent=False)"),
    ("server.py", "listChanged not advertised", "NotificationOptions(resources_changed=True)", "NotificationOptions()"),
    ("server.py", "legacy subscribe not registered",
     'mcp._lowlevel_server.add_request_handler("resources/subscribe", SubscribeRequestParams, notifier.on_subscribe)\n', ""),
    ("server.py", "saved images not listed", "return await super().list_resources() + res.image_resources(cfg)",
     "return await super().list_resources()"),
    ("resources.py", "image names not confined to the folder",
     "    if os.path.dirname(path) != root or not os.path.isfile(path):", "    if not os.path.isfile(path):"),
    ("resources.py", "traversal characters allowed",
     'if not name or name != os.path.basename(name) or name in (".", "..") or "/" in name or "\\\\" in name:',
     "if not name:"),
    ("resources.py", "resource links for files outside the images folder",
     "if os.path.dirname(os.path.realpath(path)) != os.path.realpath(images_dir(cfg)) or not path.lower().endswith(\".png\"):",
     "if not path.lower().endswith(\".png\"):"),
    ("resources.py", "clients keyed by per-request session", 'return id(getattr(session, "_connection", session))',
     "return id(session)"),
    ("resources.py", "unsubscribe ignored", "        self.subscriptions.get(str(params.uri), set()).discard(_client_key(ctx.session))\n", "        pass\n"),
    ("resources.py", "updates sent to every client",
     "await self._each(self.subscriptions.get(uri, set()), lambda s: s.send_resource_updated(uri))",
     "await self._each(self.clients, lambda s: s.send_resource_updated(uri))"),
    ("resources.py", "new images not announced", "                if now != images:", "                if False:"),
    ("registry.py", "turbo gets base defaults", 'if "turbo" in filename.lower() else', 'if "turbo!" in filename.lower() else'),
    ("registry.py", "klein size limit ignored", "return self.max_params_b is None or", "return True or"),
    ("registry.py", "VAE name preference ignored", "(not any(h in x.name.lower() for h in fam.vae_prefer), _rank(x.name))",
     "(False, _rank(x.name))"),
    ("registry.py", "text encoder precision preference ignored", "key=lambda x: _rank(x.name))\n            vae_key",
     "key=lambda x: x.name)\n            vae_key"),
    ("registry.py", "unavailable model offered", "                if not m.available:\n                    raise LookupError",
     "                if False:\n                    raise LookupError"),
    ("registry.py", "cache never used", "and key(p, kind, size, mt) not in cache]", "]"),
    ("registry.py", "cache version ignored", 'if data.get("version") == CACHE_VERSION else {}', "if isinstance(data, dict) else {}"),
]


def suite_fails() -> bool:
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x"], capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return True                     # a mutation that hangs the suite is caught too
    return r.returncode != 0


def main():
    only = sys.argv[1:]          # optional substrings: re-run just the matching mutations
    if suite_fails():
        sys.exit("clean suite fails; fix tests first")
    results = []
    for fname, name, old, new in MUTATIONS:
        if only and not any(s in name for s in only):
            continue
        path = f"pinaxium/{fname}"
        with open(path, encoding="utf-8") as f:
            orig = f.read()
        if orig.count(old) != 1:
            sys.exit(f"pattern not unique ({orig.count(old)}) for: {name}")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(orig.replace(old, new))
            caught = suite_fails()
        finally:
            with open(path, "w", encoding="utf-8") as f:
                f.write(orig)
        results.append({"mutation": name, "caught": caught})
        print(f"{'CAUGHT' if caught else 'MISSED'}  {name}", flush=True)
    clean = not suite_fails()
    out = {"caught": sum(r["caught"] for r in results), "total": len(results), "clean_run_passes": clean,
           "results": results}
    with open("mutation_check.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"{out['caught']}/{out['total']} caught; clean run passes: {clean}")


if __name__ == "__main__":
    main()
