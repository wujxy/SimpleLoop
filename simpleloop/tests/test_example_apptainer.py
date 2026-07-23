from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "examples"
EXAMPLE_DIRS = [
    EXAMPLES,
    EXAMPLES / "tiny_algo_opt",
    EXAMPLES / "omilrec-opt",
    EXAMPLES / "omilrec-post-v107-opt",
]
TASK_CONFIGS = [
    EXAMPLES / "task.yaml",
    EXAMPLES / "tiny_algo_opt" / "task.yaml",
    EXAMPLES / "omilrec-opt" / "task.yaml",
    EXAMPLES / "omilrec-opt" / "omilrec-v1.11.0.yaml",
    EXAMPLES / "omilrec-post-v107-opt" / "task.yaml",
]
BUILD_COMMANDS = {
    EXAMPLES: "simpleloop image build examples/apptainer.def",
    EXAMPLES / "tiny_algo_opt":
        "simpleloop image build examples/tiny_algo_opt/apptainer.def",
    EXAMPLES / "omilrec-opt":
        "simpleloop image build examples/omilrec-opt/apptainer.def",
    EXAMPLES / "omilrec-post-v107-opt":
        "simpleloop image build examples/omilrec-post-v107-opt/apptainer.def",
}


def test_each_example_has_independent_regular_definition():
    definitions = [
        directory / "apptainer.def" for directory in EXAMPLE_DIRS
    ]

    assert all(
        path.is_file() and not path.is_symlink()
        for path in definitions
    )
    assert len({path.resolve() for path in definitions}) == 4


def test_definitions_contain_pinned_runtime_tools():
    for directory in EXAMPLE_DIRS:
        text = (directory / "apptainer.def").read_text(
            encoding="utf-8"
        )
        assert "From: almalinux:9" in text
        assert "node-v22.19.0-linux-x64.tar.xz" in text
        assert "@anthropic-ai/claude-code@2.1.216" in text
        for command in (
            "bash",
            "git",
            "gcc",
            "g++",
            "make",
            "cmake",
            "node",
            "claude",
        ):
            assert f"command -v {command}" in text


def test_definitions_do_not_copy_repositories_or_large_resources():
    for directory in EXAMPLE_DIRS:
        text = (directory / "apptainer.def").read_text(
            encoding="utf-8"
        )
        assert "%files" not in text
        assert "/cvmfs" not in text.split("%post", 1)[-1].split(
            "%environment",
            1,
        )[0]
        assert "/data/juno" not in text.split("%post", 1)[-1].split(
            "%environment",
            1,
        )[0]


def test_all_example_configs_reference_adjacent_image():
    for path in TASK_CONFIGS:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["runtime"]["image"] == "apptainer.sif"
        assert isinstance(raw["runtime"].get("binds", []), list)


def test_omilrec_configs_bind_large_external_roots():
    for path in TASK_CONFIGS[2:]:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["runtime"]["binds"] == [
            "/cvmfs",
            "/data/juno",
            "/datafs/users/wujxy/agent-sci/omilrec_opt",
        ]


def test_generated_sifs_are_ignored():
    assert "*.sif" in (
        ROOT / ".gitignore"
    ).read_text(encoding="utf-8").splitlines()


def test_each_example_readme_shows_its_local_build_command():
    for directory, command in BUILD_COMMANDS.items():
        text = (directory / "README.md").read_text(encoding="utf-8")
        assert command in text
        assert "apptainer.sif" in text
        assert "runtime.image" in text


def test_omilrec_readmes_name_required_external_binds():
    for directory in EXAMPLE_DIRS[2:]:
        text = (directory / "README.md").read_text(encoding="utf-8")
        assert "/cvmfs" in text
        assert "/data/juno" in text
        assert "/datafs/users/wujxy/agent-sci/omilrec_opt" in text
