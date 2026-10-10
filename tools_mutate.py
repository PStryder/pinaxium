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
    ("server.py", "open-world hint left unset", "                           openWorldHint=open_world)", "                           )"),
    ("server.py", "width range not declared", 'grows with pixel count.", ge=wf.MIN_SIDE, le=wf.MAX_SIDE)]',
     'grows with pixel count.")]'),
    ("server.py", "a parameter without a description",
     'Seed = Annotated[int | None, Field(description="Fix for reproducible results; random when omitted (the seed used "\n'
     '                                               "is reported).", ge=0)]',
     "Seed = Annotated[int | None, Field(ge=0)]"),
    ("server.py", "a tool without a title", '@tool(title="Cancel render", ', "@tool("),
    ("server.py", "list_models without an output schema",
     '@tool(title="List image models", annotations=_hints(read_only=True, idempotent=True))',
     '@tool(title="List image models", annotations=_hints(read_only=True, idempotent=True), structured_output=False)'),
    ("server.py", "descriptions keep source indentation",
     '        return mcp.tool(description=_description(fn.__doc__), **kwargs)(wrapper)',
     '        return mcp.tool(description=fn.__doc__, **kwargs)(wrapper)'),
    ("schemas.py", "a result field without a description",
     'bytes: int = Field(description="File size in bytes")', "bytes: int"),
    ("registry.py", "model notes dropped", 'notes=f"{fam.notes} {variant_note}".strip()))', "notes=\"\"))"),
    ("server.py", "empty server version", "    version=__version__,\n", ""),
    ("server.py", "read-only tool not marked read-only",
     '@tool(title="Engine status", annotations=_hints(read_only=True, idempotent=True))',
     '@tool(title="Engine status", annotations=_hints(read_only=False, idempotent=True))'),
    ("server.py", "run_workflow claimed closed-world",
     "annotations=_hints(read_only=False, destructive=True, idempotent=False, open_world=True)",
     "annotations=_hints(read_only=False, destructive=True, idempotent=False)"),
    ("server.py", "listChanged not advertised", "NotificationOptions(resources_changed=True)", "NotificationOptions()"),
    ("server.py", "image links accept any Host header", "    if not _local_host_ok(request):\n        return PlainTextResponse",
     "    if False:\n        return PlainTextResponse"),
    ("server.py", "browser link offered without the service", "    if not _service_up():\n        return None",
     "    if False:\n        return None"),
    ("server.py", "file links missing from listings", "                                        **links(d[\"path\"]),\n", ""),
    ("service.py", "MCP endpoint accepts foreign hosts and origins", "enable_dns_rebinding_protection=True",
     "enable_dns_rebinding_protection=False"),
    ("service.py", "a second service starts on a taken port", "    if _port_taken(cfg.service_port):",
     "    if False:"),
    ("service.py", "configured origins ignored", "allowed_origins=[f\"http://{h}\" for h in local] + extra_origins)",
     "allowed_origins=[f\"http://{h}\" for h in local])"),
    ("config.py", "config path depends on APPDATA being set",
     "        base = _win_folder(\"APPDATA\", FOLDER_ROAMING_APPDATA, \"AppData\", \"Roaming\")",
     "        base = os.environ.get(\"APPDATA\") or os.path.join(_home(), \"AppData\", \"Roaming\")"),
    ("server.py", "legacy subscribe not registered",
     'mcp._lowlevel_server.add_request_handler("resources/subscribe", SubscribeRequestParams, notifier.on_subscribe)\n', ""),
    ("server.py", "saved images not listed", "return await super().list_resources() + res.image_resources(projects)",
     "return await super().list_resources()"),
    ("projects.py", "traversal in a URI's file name allowed",
     '        if "/" in leaf or "\\\\" in leaf or leaf in (".", ".."):\n            raise ProjectError(f"bad name in {uri}")',
     '        if False:\n            raise ProjectError(f"bad name in {uri}")'),
    ("projects.py", "project names not validated",
     "    if not _NAME_RE.match(name) or name.lower().split(\".\")[0] in _WINDOWS_RESERVED:", "    if False:"),
    ("resources.py", "finals not listed as resources", "    for f in projects.finals():\n        if not f[\"exists\"] or",
     "    for f in []:\n        if not f[\"exists\"] or"),
    ("resources.py", "clients keyed by per-request session", 'return id(getattr(session, "_connection", session))',
     "return id(session)"),
    ("resources.py", "unsubscribe ignored", "        self.subscriptions.get(str(params.uri), set()).discard(_client_key(ctx.session))\n", "        pass\n"),
    ("resources.py", "updates sent to every client",
     "await self._each(self.subscriptions.get(uri, set()), lambda s: s.send_resource_updated(uri))",
     "await self._each(self.clients, lambda s: s.send_resource_updated(uri))"),
    ("resources.py", "new images not announced", "                if now != images:", "                if False:"),
    ("config.py", "comfy_dir override borrows the file's other install",
     "    if sources[\"comfy_dir\"].startswith(\"env\"):\n        for key in (\"base_dir\", \"comfy_python\"):",
     "    if False:\n        for key in (\"base_dir\", \"comfy_python\"):"),
    ("config.py", "any folder accepted as ComfyUI", 'return (os.path.isfile(os.path.join(d, "main.py")) and', "return (True or"),
    ("config.py", "portable build's Python not looked for",
     '    candidates.append(os.path.join(parent, "python_embeded", "python.exe" if WINDOWS else "python"))  # portable\n', ""),
    ("config.py", "Desktop data folder ignored", "out.append(Install(c, base, find_comfy_python(c, base), \"ComfyUI Desktop\"))",
     "out.append(Install(c, c, find_comfy_python(c), \"ComfyUI Desktop\"))"),
    ("config.py", "config file not created on first use",
     "    elif create and not os.environ.get(\"PINAXIUM_COMFY_DIR\"):", "    elif False:"),
    ("config.py", "config file created despite an environment override",
     "    elif create and not os.environ.get(\"PINAXIUM_COMFY_DIR\"):", "    elif create:"),
    ("config.py", "file beats environment",
     "        if os.environ.get(env) not in (None, \"\"):\n            values[key], sources[key] = _coerce(key, os.environ[env]), f\"env {env}\"\n"
     "        elif file_values.get(key) is not None:\n            values[key], sources[key] = _coerce(key, file_values[key]), \"config file\"",
     "        if file_values.get(key) is not None:\n            values[key], sources[key] = _coerce(key, file_values[key]), \"config file\"\n"
     "        elif os.environ.get(env) not in (None, \"\"):\n            values[key], sources[key] = _coerce(key, os.environ[env]), f\"env {env}\""),
    ("config.py", "TOML strings not escaped", "    return json.dumps(v)          # JSON string and string-array escapes are valid TOML",
     "    return '\"' + str(v) + '\"' if isinstance(v, str) else str(v)"),
    ("supervisor.py", "--base-directory never passed", "        cmd += [\"--base-directory\", cfg.base_dir]", "        pass"),
    ("supervisor.py", "comfy_args dropped", "    return cmd + list(cfg.comfy_args)", "    return cmd"),
    ("engine.py", "engine started on a broken setup", "        if problems:\n            raise EngineError(\"can't start the engine: \"",
     "        if False:\n            raise EngineError(\"can't start the engine: \""),
    ("registry.py", "setup failures cached", 'if k in live and not v.get("transient")})', "if k in live})"),
    ("projects.py", "finals can be deleted", "                if kind == \"final\":\n                    refused.append",
     "                if False:\n                    refused.append"),
    ("projects.py", "recycle-bin failure falls back to a permanent delete",
     "            except Exception as e:                   # never fall back to a permanent delete\n"
     "                refused.append({\"image\": path, \"reason\": f\"the recycle bin refused it: {e}\"})",
     "            except Exception as e:                   # never fall back to a permanent delete\n"
     "                os.remove(path)\n                deleted.append(path)"),
    ("projects.py", "final not stamped with its identity",
     "                stamped = stamp_png(f.read(), final_uri(project, fid))     # the file names its own identity",
     "                stamped = f.read()"),
    ("projects.py", "moved finals not searched for", "        found = self._find_moved(uri, entry)", "        found = None"),
    ("projects.py", "pixel fallback ignored", "            return (size is not None and list(size) == entry.get(\"size\")",
     "            return (False and list(size) == entry.get(\"size\")"),
    ("projects.py", "relink accepts any file", "        if not self._matches(path, ref, entry):\n            raise ProjectError(f\"{path} is not {ref}",
     "        if False:\n            raise ProjectError(f\"{path} is not {ref}"),
    ("projects.py", "final ids reused", "                if fid in index[\"finals\"] or fid in index.get(\"retired\", []):",
     "                if False:"),
    ("projects.py", "refinalizing makes a second final", "            if prior and prior in index[\"finals\"]:",
     "            if False:"),
    ("projects.py", "search matches inside words", "                if all(any(t.startswith(w) for t in tokens) for w in words):",
     "                if all(w in text for w in words):"),
    ("projects.py", "drafts outside Pinaxium's folders accepted",
     "        if os.path.dirname(parent) == os.path.realpath(self.drafts_root):\n            return \"draft\"",
     "        if True:\n            return \"draft\""),
    ("projects.py", "retention sweeps finalized drafts", " and not d[\"final_uri\"]]", "]"),
    ("server.py", "contact sheet skips missing files", "        if f[\"exists\"]:\n            with PILImage.open",
     "        if not f[\"exists\"]:\n            continue\n        if True:\n            with PILImage.open"),
    ("server.py", "expected errors hidden from the model", "            except EXPECTED_ERRORS as e:\n                raise ToolError",
     "            except ToolError as e:\n                raise ToolError"),
    ("server.py", "renders not recorded in their project", "    _record_job(job_id, j[\"images\"])\n    if not j[\"images\"]:",
     "    if not j[\"images\"]:"),
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


LAST_OUTPUT = {"text": ""}


def suite_fails() -> bool:
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x"], capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        LAST_OUTPUT["text"] = "timed out"
        return True                     # a mutation that hangs the suite is caught too
    LAST_OUTPUT["text"] = r.stdout[-4000:] + r.stderr[-1000:]
    return r.returncode != 0


def main():
    only = sys.argv[1:]          # optional substrings: re-run just the matching mutations
    if suite_fails():
        print(LAST_OUTPUT["text"])
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
