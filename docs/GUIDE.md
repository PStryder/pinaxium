# Using Pinaxium

This guide is for anyone (or any agent) using Pinaxium through an MCP client. The [reference](REFERENCE.md) lists every tool, parameter and result field. It is generated from the server, so it is exact. Setup is covered in [CONFIGURATION.md](CONFIGURATION.md).

## Contents

- [The basic loop](#the-basic-loop)
- [Choosing a model](#choosing-a-model)
- [Writing prompts](#writing-prompts)
- [Editing and composing](#editing-and-composing)
- [Transparent images](#transparent-images)
- [Projects, drafts and finals](#projects-drafts-and-finals)
- [Results: where they go and how to get them back](#results-where-they-go-and-how-to-get-them-back)
- [Timing and long renders](#timing-and-long-renders)
- [The engine](#the-engine)
- [Resources and notifications](#resources-and-notifications)
- [run_workflow and the trust boundary](#run_workflow-and-the-trust-boundary)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)

## The basic loop

1. **`list_models`**: see what's installed and usable, with notes comparing the models. The first model listed is the default.
2. **`generate_image`** with a prompt, optionally a `model`, a size, a `seed` and a **`project`** to keep this piece of work together (the default is `scratch`).
3. **Look at the result.** The reply contains, in order:
   - a text block with the saved path, size, settings used (model, steps, seed, cfg), project, render time and job id;
   - a resource link to the full-size draft, `pinaxium://projects/<project>/drafts/<file>`;
   - a preview of up to 1024 px.

   To check fine detail (lettering, hands, faces), call **`view_image`** with `crop` on the draft's URI.
4. **Iterate.** Keep the reported `seed` and change the prompt for controlled variations, or leave the seed out for fresh ones. Use **`edit_image`** to change an existing image rather than starting over.
5. **Finish.** Use **`finalize_image`** on the keeper (it gets a permanent ID and URI), then **`delete_images`** on the leftover drafts. Deleted drafts go to the recycle bin.

## Choosing a model

`list_models` reports each model's `capabilities` (editing, maximum reference images, transparency, lettering quality, maximum size) and `notes`. As a rule of thumb for the supported families:

| Need | Good choice |
|---|---|
| Best quality, reliable lettering, transparency | Qwen-Image 2.1 (base) |
| Quick drafts or iterations of the same | Qwen-Image 2.1 turbo: very similar images to base for the same seed, several times faster |
| Fast text-to-image with clean lettering | Z-Image Turbo |
| Fastest, including edits | FLUX.2 [klein] 4B; lettering is unreliable, so avoid it for images that must contain text |
| Editing or composing from up to 10 images | Qwen-Image 2.1 (klein takes up to 4) |

Licenses differ: Qwen-Image 2.1 is research-only (non-commercial); Z-Image and klein are Apache-2.0. `list_models` reports each model's license.

## Writing prompts

- **Describe, don't tag.** These models are trained on natural-language captions. Say what the subject is, the setting, the style or medium, the lighting and the composition, in full sentences.
- **Lettering:** put the exact text in quotes, e.g. `a shop sign reading "OPEN LATE"`. Check the result with `view_image` and `crop`.
- **`negative_prompt` only matters when `cfg_scale` > 1.** The turbo and distilled models run at 1 by default, where it has no effect.
- **Steps and cfg:** leave them unset. Each model's defaults come from its official workflow. More steps make a render slower, rarely better.
- **Size:** 256–2048 px per side, rounded to multiples of 16. Time grows with the pixel count, so 2048×2048 takes about 4× as long as 1024×1024. Use about 1 megapixel for drafts.

## Editing and composing

`edit_image` takes an instruction and one or more reference images (absolute paths; PNG, JPEG or WebP):
- **One image:** "make it night", "turn it into a watercolor", "remove the person on the left".
- **Several images:** refer to them as *image 1*, *image 2* and so on, in the order given. For example: "put the jacket from image 2 on the person in image 1".
- **Output size:** the result has the first image's aspect ratio, at about `resolution`×`resolution` pixels.
- **Models:** only those with `capabilities.edit` can edit, up to their `max_reference_images`.
- **Iterating on a result:** pass the saved path from a previous reply.

## Transparent images

Qwen-Image 2.1 can output an alpha channel. Pass `transparent=true` and describe the background as transparent in the prompt. Other models reject `transparent=true`. Without it, all results are plain RGB.

## Projects, drafts and finals

A **project** is a named body of work, such as `logo-redesign` or `hexy`. Pass `project` to `generate_image`, `edit_image`, `derive_from_final` or `run_workflow`. A project is created on first use; names are 1–64 letters, digits, spaces, `.`, `-` or `_`.

**Drafts** are every render, kept per project.
- **Provenance:** each draft is recorded with what made it: prompt, model, seed, settings, and source images for edits.
- **Browsing:** `list_drafts` pages through drafts, newest first.
- **Addressing:** `pinaxium://projects/<project>/drafts/<file>`.

**Finals** are the keepers. `finalize_image` copies a draft into the project's finals and gives it:
- **A permanent ID and URI**, e.g. `pinaxium://projects/hexy/finals/portrait-001`. Choose the ID with `final_id`, or get the next `final-NNN`. IDs are never reused.
- **Search metadata:** a `title`, a `description` (by default the prompt) and `tags`.
- **The draft's full provenance.**

Finalizing the same draft again returns its existing final. Finals are never deleted or overwritten by Pinaxium.

**Refer to images by URI, not by path.** A final's URI resolves through Pinaxium wherever its file now is:
- **The file is self-identifying.** It carries its own URI inside it, in a PNG text chunk added without touching the pixels or ComfyUI's metadata.
- **Renames and moves inside the finals folder** are found and remembered automatically.
- **Stripped metadata:** if a tool removes the stamp, the image is still recognised by its pixel content.
- **Moves outside the finals folder:** `relink_final` points Pinaxium at the new location, and checks it really is that image.

**Finding and reusing finals:**
- **`list_finals`** searches across all projects. Every word of `query` must start a word of the final's ID, title, description, tags, prompt or project. Results come in pages (`limit`, `offset`, `next_offset`) and are metadata only by default.
- **Thumbnails:** with `thumbnails=true` you also get **one** contact-sheet image of the page. Tile **#n** is the entry whose `sheet_number` is *n*, so a thumbnail always maps to exactly one URI. A final whose file is missing still gets its numbered slot.
- **`get_final`** returns one final at a chosen size, with its metadata.
- **`derive_from_final`** starts a new branch: an edit with the final as image 1, landing as a new draft in the final's project or any other. The new draft records `derived_from`, so lineage carries through when it's finalized in turn.
- **Any draft or final URI** works as an `edit_image` reference, or in `view_image`.

**Cleaning up:** `delete_images` sends drafts to the operating system's recycle bin, one by one or a whole project's at once (including its uploaded references). It never deletes finals, never touches anything outside Pinaxium's folders, and never falls back to a permanent delete: a file the recycle bin refuses is reported and left in place. Optionally, `draft_retention_days` recycles unfinalized drafts automatically after that many days (see [CONFIGURATION.md](CONFIGURATION.md)).

## Results: where they go and how to get them back

| What | Where | URI |
|---|---|---|
| Drafts | `<base_dir>/output/pinaxium/<project>/` (ComfyUI's output folder by default) | `pinaxium://projects/<project>/drafts/<file>` |
| Finals | `<finals_dir>/<project>/<id>.png` (your Pictures folder's `Pinaxium` by default) | `pinaxium://projects/<project>/finals/<id>` |
| A project's record | `project.json` (drafts) and `finals.json` (finals) | `pinaxium://projects/<project>` |

- **Links the user can click:** every render reply has an `open:` line, a `file:///` link to the file on this machine's disk. While the always-on service runs it also has a `browser:` line, an `http://127.0.0.1:8190/files/...` link that opens in any browser on this machine, including where `file://` links are blocked. Listings, `finalize_image` and `get_final` carry the same as `file_uri` and `local_url`. A final's links always point to where its file is now.
- **From a reply:** the resource link, or the path in the text block.
- **Later:** `list_projects`, `list_drafts` and `list_finals`, or the client's resource list (finals first, then the newest drafts, 500 in all). All of them work while the engine is stopped.
- **Previews are downscaled:** at most 1024 px in render replies, and up to 1568 px with `view_image` and `get_final`. Read the URI as a resource for the original.

## Timing and long renders

- **First request after idle:** starts the engine (about 10–20 s) and loads the model. Each model loads once and stays loaded, but switching to a different model reloads.
- **Example render times** on an RTX 4080 (1024²): klein ~2 s, Z-Image Turbo ~3 s, Qwen turbo ~3.5 s, Qwen base ~8 s; at 2048², Qwen base ~60 s. See the README's benchmark.
- **One render at a time:** renders queue in order. The engine is shared between all connected clients, so another client's job can delay yours.
- **`max_wait_s`** (default 300) caps how long a render tool waits. If the render isn't done by then, the reply carries a `job_id`. Call `get_job` with it later; `cancel_render` interrupts the running render.

## The engine

- **States** (`engine_status`): `stopped`, `starting`, `ready` (started by Pinaxium), or `external` (a ComfyUI someone else started on the configured port, used but never stopped).
- **Starting:** any tool that needs the engine starts it. `start_engine` starts it ahead of time.
- **Stopping:** the engine stops by itself after `idle_minutes` (default 10) with no running job and no requests that use it: renders, edits, `get_job`, `start_engine`. `engine_status`, `list_models` and reading resources don't count, so checking on it doesn't keep it alive.
  - `stop_engine` stops it now and frees all GPU memory, but also ends other clients' renders.
  - `free_vram` unloads the models and keeps the engine running.
- **After a client closes:** the engine is shared and keeps running until it goes idle.

## Resources and notifications

| Resource | Use |
|---|---|
| `pinaxium://projects/<project>/drafts/<file>` | A draft, full size |
| `pinaxium://projects/<project>/finals/<id>` | A final, full size, by its permanent ID |
| `pinaxium://projects/<project>` | The project's drafts and finals with their provenance; subscribe to hear about changes |
| `pinaxium://engine/status` | Same JSON as `engine_status`; subscribe to be told when the engine starts, stops, gets busy or loads models |
| `pinaxium://logs/comfyui` | ComfyUI's log (last 256 KB): the first place to look when a render fails |
| `pinaxium://logs/supervisor` | The engine supervisor's log: start-up, idle shutdown, spawn problems |

Clients are notified of changes:
- A new or removed draft or final triggers a resource-list-changed notification.
- A change to a subscribed resource triggers a resource-updated notification. The watcher checks every 2 s.

## run_workflow and the trust boundary

The built-in tools only run fixed ComfyUI graphs, which touch nothing but the local ComfyUI and local files. `run_workflow` runs any graph in ComfyUI's API format, as produced by ComfyUI's "Export (API)" menu item. It exists for things the built-in tools don't cover, such as LoRAs, ControlNet or other model families. It is **open-world and destructive**:
- ComfyUI's partner API nodes send prompts and images to paid external services;
- custom nodes can run arbitrary code with network access;
- save nodes can overwrite files.

Agents should use it only when the user wants that. Operators who expose Pinaxium to untrusted input should consider removing it (see [DEVELOPMENT.md](DEVELOPMENT.md)).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `can't start the engine: ComfyUI location is not configured` | No ComfyUI was found and none is configured | Install ComfyUI, then run `python -m pinaxium.config --init`, or set `comfy_dir` in the config file |
| `... is not a ComfyUI folder` / `comfy_python ... does not exist` | The config points at the wrong place | Fix `comfy_dir` / `comfy_python`, or rerun `--init`. `python -m pinaxium.config` shows what's wrong. |
| `engine not ready after 180s` | ComfyUI is slow to start (first run, many custom nodes) or stuck | Read `pinaxium://logs/comfyui`. Raise `start_timeout_s` if it was still loading. |
| `engine exited during startup with code N` | ComfyUI crashed at start-up, or the port is taken by a program that isn't ComfyUI | Read `pinaxium://logs/comfyui`. If the port is the problem, change `port`. |
| `render failed: <node>: ... out of memory` | The GPU ran out of memory | Use a smaller size or a turbo/distilled model, or add `"--lowvram"` to `comfy_args` |
| `render failed: <node>: <message>` (anything else) | ComfyUI rejected or failed the graph | Read the message and `pinaxium://logs/comfyui`. If ComfyUI is older than the model, update it. |
| `unknown model 'x'` | Wrong id | Use an id from `list_models` |
| `model 'x' is missing text encoder (...)` / `VAE (...)` | A companion file isn't installed | Download it into ComfyUI's `text_encoders` / `vae` folder (see the README's install section) |
| A model file isn't offered | Unsupported architecture, or still downloading | `list_models` → `other_files` says why. Unsupported families can still run via `run_workflow`. |
| `model x can't edit images` / `give 1 to N image paths` | The model lacks editing, or you passed too many references | Pick a model with `capabilities.edit` and stay within its `max_reference_images` |
| A reply with a `job_id` and no image | The render outlived `max_wait_s` | `get_job` with that id. Raise `max_wait_s` for big renders. |
| `the engine is stopped, so the job's state is gone` | The engine stopped (idle or `stop_engine`) after the job ran | The image was still saved: use `list_drafts` |
| `the file of final ... can't be found` / a final with `exists: false` | Its file was moved outside the finals folder, or deleted | `relink_final` with its new path. If it was deleted, restore it from the recycle bin or backup. |
| `final id 'x' is already taken` | Final IDs are permanent and never reused | Choose another `final_id`, or omit it for the next `final-NNN` |
| `invalid project name` | The name has characters a folder can't hold | Use letters, digits, spaces, `.`, `-` or `_` |
| A file in `not_deleted` with `the recycle bin refused it` | The OS recycle bin can't take that file (e.g. on a network drive) | Delete it yourself if you want it gone. Pinaxium never deletes permanently. |
| The engine never stops | Requests keep arriving, a job is queued, or the engine is `external` | `engine_status` shows `idle_minutes` and `status`. Use `stop_engine`, or stop your own ComfyUI. |
| Tools don't appear in Claude Desktop | Desktop reads its config only at start-up | Restart Desktop after registering, and check the paths in `claude_desktop_config.json` |

## FAQ

**How do I add a model?** Download its files into ComfyUI's model folders: the diffusion model into `diffusion_models`, the text encoder into `text_encoders`, the VAE into `vae`. They're detected automatically on the next `list_models` or render. Only the [supported families](../README.md#supported-models) become models; other files are listed with the reason.

**Can I use ComfyUI's own web UI at the same time?**
- *Pinaxium's engine is running:* open `http://127.0.0.1:8188`. Pinaxium still stops it when idle, which closes the UI's backend.
- *You started ComfyUI yourself on that port:* Pinaxium uses it (status `external`) and never stops it.

**Does anything leave my machine?** Not with the built-in tools. `run_workflow` can, depending on the nodes in the graph you give it.

**Where are logs and state kept?** In `state_dir`; see [CONFIGURATION.md](CONFIGURATION.md#files-pinaxium-keeps). They're also readable as the `pinaxium://logs/...` resources.

**Can I rename or reorganise my finals folder?** Yes, within `finals_dir`: renamed and moved finals are found by their embedded identity. If you move them somewhere else, use `relink_final`. If you change `finals_dir` itself, move the project folders along with it.

**How do I uninstall?**
1. Remove the `pinaxium` entry from your MCP client.
2. Delete the cloned folder, the config file and `state_dir`.
3. Optionally delete the drafts and uploads, `<base_dir>/output/pinaxium` and `<base_dir>/input/pinaxium`.

Your finals in `finals_dir` are yours: keep them or delete them. ComfyUI and your models are not touched.
