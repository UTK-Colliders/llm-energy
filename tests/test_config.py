from pathlib import Path

import pytest

from llm_energy.config import find_task, load_task

REPO = Path(__file__).parent.parent


def test_madgraph_task_parses():
    task = find_task("madgraph-ttbar-lhe", REPO / "tasks")
    assert task.name == "madgraph-ttbar-lhe"
    assert task.default_image == "native"
    native = task.image()
    assert native.build_context is not None
    assert (native.build_context / "Dockerfile").exists()
    scailfin = task.image("scailfin")
    assert scailfin.pull and scailfin.platform == "linux/amd64"
    assert task.command[0] == "mg5_aMC"
    assert task.metadata["nevents"] == 10000
    assert task.metadata["ebeam1_gev"] == 6800
    # the mg5 card the command references must exist and pin the seed
    card = task.task_dir / "cards" / "ttbar_lhe.mg5"
    text = card.read_text()
    assert "set nevents 10000" in text
    assert "set ebeam1 6800.0" in text
    assert "set iseed 42" in text
    assert "generate p p > t t~" in text


def test_unknown_image_variant_rejected():
    task = find_task("madgraph-ttbar-lhe", REPO / "tasks")
    with pytest.raises(ValueError, match="no image variant"):
        task.image("nonexistent")


def test_invalid_task_yaml(tmp_path):
    d = tmp_path / "bad-task"
    d.mkdir()
    (d / "task.yaml").write_text("name: bad\nimages:\n  x: {build: {context: .}, pull: y, tag: z}\n"
                                 "default_image: x\ncommand: [true]\n")
    with pytest.raises(ValueError, match="exactly one of build/pull"):
        load_task(d)


def test_relative_mount_target_rejected(tmp_path):
    d = tmp_path / "bad-task"
    d.mkdir()
    (d / "task.yaml").write_text(
        "name: bad\nimages:\n  x: {pull: alpine, tag: alpine}\n"
        "default_image: x\ncommand: [true]\nmounts:\n"
        "  - {source: cards, target: relative/path}\n")
    with pytest.raises(ValueError, match="absolute"):
        load_task(d)
