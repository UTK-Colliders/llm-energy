from pathlib import Path

import pytest

from llm_energy.config import find_task, load_task

REPO = Path(__file__).parent.parent


def test_madgraph_task_parses():
    task = find_task("madgraph-ttbar2j-lhe", REPO / "tasks")
    assert task.name == "madgraph-ttbar2j-lhe"
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
    card = task.task_dir / "cards" / "ttbar2j_lhe.mg5"
    text = card.read_text()
    assert "set nevents 10000" in text
    assert "set ebeam1 6800.0" in text
    assert "set iseed 42" in text
    assert "generate p p > t t~ j j" in text


def test_unknown_image_variant_rejected():
    task = find_task("madgraph-ttbar2j-lhe", REPO / "tasks")
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


# --- phases ------------------------------------------------------------------

BASE = """
schema_version: 1
name: t
images:
  native: {build: {context: .}, tag: img:1}
default_image: native
"""


def write_task(tmp_path, body):
    (tmp_path / "task.yaml").write_text(BASE + body)
    return tmp_path


def test_single_command_becomes_one_phase(tmp_path):
    task = load_task(write_task(tmp_path, 'command: ["a", "b"]\n'))
    assert [p.name for p in task.phases] == ["run"]
    assert task.phases[0].command == ["a", "b"]
    assert task.command == ["a", "b"]
    assert not task.multiphase


def test_phases_are_parsed_in_order(tmp_path):
    task = load_task(write_task(tmp_path, """
phases:
  - {name: compile, command: ["make"], description: build, timeout_s: 60}
  - {name: run, command: ["go"]}
"""))
    assert [p.name for p in task.phases] == ["compile", "run"]
    assert task.phases[0].description == "build"
    assert task.phases[0].timeout_s == 60
    assert task.phases[1].timeout_s is None      # falls back to the task's
    assert task.multiphase


def test_command_property_refuses_to_guess_for_multiphase(tmp_path):
    task = load_task(write_task(tmp_path, """
phases:
  - {name: a, command: ["x"]}
  - {name: b, command: ["y"]}
"""))
    with pytest.raises(ValueError, match="use task.phases"):
        task.command


def test_command_and_phases_together_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="exactly one of"):
        load_task(write_task(tmp_path, 'command: ["a"]\nphases: [{name: p, command: ["b"]}]\n'))


def test_neither_command_nor_phases_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="exactly one of"):
        load_task(write_task(tmp_path, "workdir: /work\n"))


def test_duplicate_phase_names_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="unique"):
        load_task(write_task(tmp_path, """
phases:
  - {name: same, command: ["x"]}
  - {name: same, command: ["y"]}
"""))


def test_phase_missing_command_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="needs 'name' and 'command'"):
        load_task(write_task(tmp_path, 'phases: [{name: p}]\n'))


def test_empty_phase_list_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="non-empty"):
        load_task(write_task(tmp_path, "phases: []\n"))


def test_split_task_matches_the_single_phase_task_physics():
    single = find_task("madgraph-ttbar2j-lhe", REPO / "tasks")
    split = find_task("madgraph-ttbar2j-split", REPO / "tasks")
    keys = ("process", "order", "ebeam1_gev", "ebeam2_gev", "nevents", "iseed")
    assert {k: single.metadata[k] for k in keys} == {k: split.metadata[k] for k in keys}
    # both build the same image, from the one Dockerfile
    assert single.image().tag == split.image().tag
    assert (split.image().build_context / "Dockerfile").exists()
    # the run card the launch phase uses pins the same physics
    launch = (split.task_dir / "cards" / "ttbar2j_launch.mg5").read_text()
    for line in ("set nevents 10000", "set ebeam1 6800.0", "set iseed 42"):
        assert line in launch
    codegen = (split.task_dir / "cards" / "ttbar2j_codegen.mg5").read_text()
    assert "generate p p > t t~ j j" in codegen
    # comments may discuss launch; no MG5 *command* in this card may be one,
    # or event generation would leak into the compile phase
    commands = [ln.strip() for ln in codegen.splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any(c.startswith("launch") for c in commands), commands
    assert commands[-1].startswith("output ")
