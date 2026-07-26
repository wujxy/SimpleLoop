from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
EXAMPLES = ROOT / "examples"
# Only these example dirs ship their own apptainer.def; the omilrec-* examples
# reuse a shared junosw definition / prebuilt SIF instead of an independent def.
DEF_DIRS = [
    EXAMPLES,
    EXAMPLES / "tiny_algo_opt",
]
OMILREC_DIRS = [
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
}


def test_def_examples_have_independent_regular_definitions():
    definitions = [directory / "apptainer.def" for directory in DEF_DIRS]

    assert all(
        path.is_file() and not path.is_symlink()
        for path in definitions
    )
    assert len({path.resolve() for path in definitions}) == len(DEF_DIRS)


def test_definitions_contain_pinned_runtime_tools():
    for directory in DEF_DIRS:
        text = (directory / "apptainer.def").read_text(
            encoding="utf-8"
        )
        assert "From: almalinux:9" in text
        assert "node-v22.19.0-linux-x64.tar.xz" in text
        assert "@anthropic-ai/claude-code@2.1.216" in text
        # The defs' %test self-checks the interpreter/agent layer; the build
        # toolchain (gcc/g++/make/cmake) is verified at run time by the
        # runtime preflight inside the image instead.
        for command in (
            "bash",
            "git",
            "node",
            "claude",
            "python",
            "python3",
            "pytest",
        ):
            assert f"command -v {command}" in text


def test_definitions_do_not_copy_repositories_or_large_resources():
    for directory in DEF_DIRS:
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


def test_python_examples_provide_the_unversioned_python_command():
    for directory in DEF_DIRS:
        text = (directory / "apptainer.def").read_text(
            encoding="utf-8"
        )
        assert "python-unversioned-command" in text
        assert "command -v python" in text


def test_all_example_configs_reference_adjacent_image():
    for path in TASK_CONFIGS:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["runtime"]["image"] == "apptainer.sif"
        assert isinstance(raw["runtime"].get("binds", []), list)


def test_omilrec_configs_bind_large_external_roots():
    for path in [p for p in TASK_CONFIGS if "omilrec" in str(p.parent)]:
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
    for directory in OMILREC_DIRS:
        text = (directory / "README.md").read_text(encoding="utf-8")
        assert "/cvmfs" in text
        assert "/data/juno" in text
        assert "/datafs/users/wujxy/agent-sci/omilrec_opt" in text
