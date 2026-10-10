"""Projects: names and URIs, drafts and provenance, finals with permanent identities that survive renames and
moves, recycle-bin-only deletion that never touches finals, retention."""

import os
import shutil
import time

import pytest
from PIL import Image, PngImagePlugin

from pinaxium import projects as P
from pinaxium.config import load


@pytest.fixture
def world(tmp_path, monkeypatch):
    comfy = tmp_path / "comfy"
    (comfy / "models").mkdir(parents=True)
    monkeypatch.setenv("PINAXIUM_COMFY_DIR", str(comfy))
    monkeypatch.setenv("PINAXIUM_FINALS_DIR", str(tmp_path / "finals"))
    monkeypatch.setenv("PINAXIUM_STATE_DIR", str(tmp_path / "state"))
    trash = tmp_path / "trash"
    trashed = []

    def fake_trash(path):
        trashed.append(path)
        trash.mkdir(exist_ok=True)
        os.replace(path, trash / f"{len(trashed)}-{os.path.basename(path)}")

    pj = P.Projects(load(create=False), trash=fake_trash)

    def render(project="art", name=None, color=(200, 30, 30), meta=None, size=(64, 48)):
        d = pj.drafts_dir(project)
        os.makedirs(d, exist_ok=True)
        name = name or f"img_{len(os.listdir(d)):05d}_.png"
        info = PngImagePlugin.PngInfo()
        info.add_text("prompt", '{"comfy": "workflow"}')          # ComfyUI-style metadata that must survive
        Image.new("RGB", size, color).save(os.path.join(d, name), pnginfo=info)
        pj.record(project, [os.path.join(d, name)], meta or {"kind": "generate", "model": "m", "prompt": "a red fox",
                                                              "seed": 7})
        return os.path.join(d, name)

    return pj, render, trashed, tmp_path


# ---------- names and URIs ----------

@pytest.mark.parametrize("name", ["scratch", "Logo Redesign", "a", "v1.2_final-ish", "x" * 64])
def test_valid_project_names(name):
    assert P.project_name(name) == name


@pytest.mark.parametrize("name", [".hidden", "a/b", "a\\b", "trailing.", "con", "LPT1.png", "x" * 65, "..", "a:b"])
def test_invalid_project_names(name):
    with pytest.raises(P.ProjectError):
        P.project_name(name)


def test_blank_means_default_and_spaces_are_trimmed():
    assert P.project_name(None) == P.project_name("") == P.project_name("  ") == P.DEFAULT_PROJECT
    assert P.project_name("  Logo  ") == "Logo"


def test_final_ids_are_slugs():
    assert P.final_id("Portrait 001") == "portrait-001"
    assert P.final_id("  Hero  Shot!! ") == "hero-shot"
    with pytest.raises(P.ProjectError):
        P.final_id("!!!")


def test_uris_round_trip_and_reject_traversal():
    assert P.parse_uri(P.draft_uri("My Art", "a b.png")) == ("draft", "My Art", "a b.png")
    assert P.parse_uri(P.final_uri("hexy", "portrait-001")) == ("final", "hexy", "portrait-001")
    assert P.parse_uri(P.project_uri("hexy")) == ("project", "hexy", None)
    for bad in ["pinaxium://projects/hexy/finals/Not_A_Slug", "pinaxium://projects/hexy/drafts/..",
                "pinaxium://projects/../x", "pinaxium://projects/hexy/other/x", "pinaxium://images/x/y.png",
                "pinaxium://projects/hexy/drafts/a%2Fb.png"]:
        with pytest.raises(P.ProjectError):
            P.parse_uri(bad)


# ---------- drafts ----------

def test_drafts_are_recorded_with_provenance(world):
    pj, render, _, _ = world
    path = render(meta={"kind": "edit", "model": "q", "prompt": "make it blue", "seed": 3, "sources": ["x"]})
    (d,) = pj.drafts("art")
    assert d["uri"] == P.draft_uri("art", os.path.basename(path))
    assert (d["model"], d["prompt"], d["seed"], d["final_uri"]) == ("q", "make it blue", 3, None)
    assert pj.manifest("art")["images"][os.path.basename(path)]["sources"] == ["x"]


def test_flat_layout_moves_into_scratch(world):
    pj, _, _, _ = world
    os.makedirs(pj.drafts_root, exist_ok=True)
    Image.new("RGB", (8, 8)).save(os.path.join(pj.drafts_root, "old.png"))
    assert pj.migrate_flat_layout() == 1
    assert os.path.isfile(os.path.join(pj.drafts_dir("scratch"), "old.png"))
    assert pj.migrate_flat_layout() == 0


# ---------- finals ----------

def test_finalize_stamps_a_copy_and_keeps_the_image_and_metadata_intact(world):
    pj, render, _, _ = world
    path = render()
    r = pj.finalize(path, title="Fox", tags=["animal", "red"])
    assert (r["id"], r["uri"], r["already_final"]) == ("final-001", "pinaxium://projects/art/finals/final-001", False)
    assert P.read_stamp(r["path"]) == r["uri"] and P.read_stamp(path) is None          # the draft is untouched
    with Image.open(path) as a, Image.open(r["path"]) as b:
        assert a.tobytes() == b.tobytes() and b.text.get("prompt") == '{"comfy": "workflow"}'
    (f,) = pj.finals("art")
    assert (f["title"], f["tags"], f["description"], f["prompt"], f["model"]) == ("Fox", ["animal", "red"],
                                                                                 "a red fox", "a red fox", "m")
    assert f["source_draft"] == P.draft_uri("art", os.path.basename(path))
    assert pj.drafts("art")[0]["final_uri"] == r["uri"]


def test_ids_auto_number_explicit_ids_are_unique_and_refinalizing_is_idempotent(world):
    pj, render, _, _ = world
    a, b, c = render(), render(color=(0, 90, 0)), render(color=(0, 0, 90))
    assert pj.finalize(a)["id"] == "final-001"
    assert pj.finalize(b, "Portrait 001")["id"] == "portrait-001"
    assert pj.finalize(a) == {**pj.finalize(a), "already_final": True}                  # same final back
    assert pj.finalize(a)["id"] == "final-001"
    with pytest.raises(P.ProjectError, match="already taken"):
        pj.finalize(c, "portrait-001")
    assert pj.finalize(c)["id"] == "final-003"


def test_final_survives_rename_and_move_inside_the_finals_tree(world):
    pj, render, _, _ = world
    r = pj.finalize(render())
    renamed = os.path.join(pj.finals_root, "elsewhere", "my favourite.png")
    os.makedirs(os.path.dirname(renamed))
    os.replace(r["path"], renamed)
    kind, project, path, fid = pj.locate(r["uri"])
    assert (kind, project, fid) == ("final", "art", "final-001") and path == os.path.realpath(renamed)
    assert pj.index("art")["finals"]["final-001"]["path"] == os.path.realpath(renamed)     # remembered


def test_final_found_by_its_pixels_when_the_stamp_was_stripped(world):
    pj, render, _, _ = world
    r = pj.finalize(render())
    with Image.open(r["path"]) as im:
        im.load()
        stripped = im.copy()
    os.remove(r["path"])
    stripped.save(os.path.join(pj.finals_root, "re-saved without metadata.png"))       # new bytes, same pixels
    assert pj.locate(r["uri"])[2].endswith("re-saved without metadata.png")


def test_moved_outside_the_tree_needs_relink_and_relink_checks_identity(world):
    pj, render, _, tmp = world
    r = pj.finalize(render())
    outside = tmp / "desktop" / "fox.png"
    outside.parent.mkdir()
    shutil.move(r["path"], outside)
    (f,) = pj.finals("art")
    assert f["exists"] is False
    with pytest.raises(P.ProjectError, match="relink_final"):
        pj.locate(r["uri"])
    other = render(color=(1, 2, 3))
    with pytest.raises(P.ProjectError, match="neither its stamp nor its content"):
        pj.relink_final(r["uri"], other)
    pj.relink_final(r["uri"], str(outside))
    assert pj.locate(r["uri"])[2] == os.path.realpath(outside) and pj.finals("art")[0]["exists"]


def test_a_final_is_recognised_by_its_stamp_wherever_it_sits(world):
    pj, render, _, _ = world
    r = pj.finalize(render())
    sneaky = os.path.join(pj.drafts_dir("art"), "looks-like-a-draft.png")
    shutil.move(r["path"], sneaky)
    assert pj.locate(sneaky)[0] == "final"


def test_search_matches_all_words_across_fields_and_projects(world):
    pj, render, _, _ = world
    pj.finalize(render("hexy"), "portrait-001", title="Hero portrait", tags=["character"])
    pj.finalize(render("other", color=(9, 9, 9), meta={"kind": "generate", "prompt": "a blue whale", "seed": 1}),
                description="Ocean poster")
    assert [f["id"] for f in pj.finals(query="hero character")] == ["portrait-001"]
    assert [f["project"] for f in pj.finals(query="whale")] == ["other"]
    assert [f["project"] for f in pj.finals(query="ocean poster")] == ["other"]
    assert pj.finals(query="hero whale") == []
    assert [f["id"] for f in pj.finals(query="port")] == ["portrait-001"]           # word prefix
    assert pj.finals(query="ortrait") == [] and pj.finals(query="1") == []          # not inside words ("001")
    assert {f["project"] for f in pj.finals()} == {"hexy", "other"}


# ---------- deletion ----------

def test_delete_recycles_drafts_and_never_finals(world):
    pj, render, trashed, tmp = world
    keep, scrap = render(), render(color=(1, 1, 1))
    f = pj.finalize(keep)
    out = pj.delete([scrap, f["uri"], f["path"], str(tmp / "unrelated.png"), keep])
    assert set(out["deleted"]) == {os.path.realpath(scrap), os.path.realpath(keep)}
    reasons = {o["image"]: o["reason"] for o in out["not_deleted"]}
    assert "never deleted" in reasons[f["uri"]] and "never deleted" in reasons[f["path"]]
    assert "not one of Pinaxium's" in reasons[str(tmp / "unrelated.png")]
    assert os.path.isfile(f["path"]) and trashed == [os.path.realpath(scrap), os.path.realpath(keep)]
    assert pj.finals("art")[0]["source_draft"]                  # the final keeps its provenance


def test_recycle_bin_refusal_is_reported_never_a_permanent_delete(world):
    pj, render, _, _ = world
    path = render()

    def refusing(p):
        raise OSError("not on this drive")

    pj.trash = refusing
    out = pj.delete([path])
    assert out["deleted"] == [] and "refused" in out["not_deleted"][0]["reason"] and os.path.isfile(path)


def test_delete_whole_project_takes_drafts_and_uploads_only(world):
    pj, render, trashed, _ = world
    render(), render(color=(5, 5, 5))
    f = pj.finalize(render(color=(6, 6, 6)))
    os.makedirs(pj.uploads_dir("art"))
    open(os.path.join(pj.uploads_dir("art"), "ref.jpg"), "w").close()
    out = pj.delete(project="art")
    assert len(out["deleted"]) == 3 and len(trashed) == 4          # 3 drafts + 1 upload
    assert pj.drafts("art") == [] and os.path.isfile(pj.locate(f["uri"])[2])
    with pytest.raises(P.ProjectError):
        pj.delete(refs=["x"], project="art")


def test_retention_sweep_spares_new_and_finalized_drafts(world):
    pj, render, trashed, _ = world
    old, kept_old, new = render(), render(color=(2, 2, 2)), render(color=(3, 3, 3))
    pj.finalize(kept_old)
    past = time.time() - 10 * 86400
    for p in (old, kept_old):
        os.utime(p, (past, past))
    assert pj.sweep(7) == [os.path.realpath(old)]
    assert pj.sweep(0) == [] and os.path.isfile(new)
