from pathlib import PurePosixPath

import pytest

from simpleloop.world import (
    MountMode,
    MountSpec,
    SandboxSpec,
    SourceWorkspace,
)
from simpleloop.world.builder import WorldBuilder, WorldError, WorldSpec


class CapturingProvider:
    def __init__(self):
        self.mounts = ()

    def bind(self, _spec, mounts):
        self.mounts = mounts
        return object()


def test_executor_world_mounts_base_ro_and_editable_overlay_rw(tmp_path):
    provider = CapturingProvider()
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    workspace = SourceWorkspace("c0", repo, "base")

    world = WorldBuilder(provider).build(
        workspace,
        SandboxSpec(tmp_path / "runtime.sif"),
        WorldSpec(MountMode.READ_ONLY, (PurePosixPath("src"),)),
    )

    assert provider.mounts[0] == MountSpec(repo.resolve(), PurePosixPath("/work"))
    assert provider.mounts[1] == MountSpec(
        (repo / "src").resolve(),
        PurePosixPath("/work/src"),
        MountMode.READ_WRITE,
    )
    assert world.workspace is workspace


def test_evaluator_world_mounts_workspace_read_write(tmp_path):
    provider = CapturingProvider()
    repo = tmp_path / "repo"
    repo.mkdir()

    WorldBuilder(provider).build(
        SourceWorkspace("baseline", repo, "base"),
        SandboxSpec(tmp_path / "runtime.sif"),
        WorldSpec(MountMode.READ_WRITE),
    )

    assert provider.mounts[0].mode is MountMode.READ_WRITE


def test_builder_rejects_writable_symlink_escape(tmp_path):
    provider = CapturingProvider()
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (repo / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorldError, match="escapes workspace"):
        WorldBuilder(provider).build(
            SourceWorkspace("c0", repo, "base"),
            SandboxSpec(tmp_path / "runtime.sif"),
            WorldSpec(MountMode.READ_ONLY, (PurePosixPath("link"),)),
        )


@pytest.mark.parametrize("path", [PurePosixPath("../x"), PurePosixPath("/x")])
def test_builder_rejects_non_relative_writable_path(tmp_path, path):
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(WorldError, match="writable path"):
        WorldBuilder(CapturingProvider()).build(
            SourceWorkspace("c0", repo, "base"),
            SandboxSpec(tmp_path / "runtime.sif"),
            WorldSpec(MountMode.READ_ONLY, (path,)),
        )
