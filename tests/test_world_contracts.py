from dataclasses import FrozenInstanceError
from pathlib import PurePosixPath

import pytest

from simpleloop.world import (
    MountMode,
    MountSpec,
    ProcessResult,
    SourceWorkspace,
    WorkspaceSpec,
)


def test_world_values_are_frozen(tmp_path):
    workspace = SourceWorkspace("r0-c0", tmp_path, "base")

    with pytest.raises(FrozenInstanceError):
        workspace.base_sha = "other"


def test_mount_defaults_read_only(tmp_path):
    mount = MountSpec(tmp_path, PurePosixPath("/data"))

    assert mount.mode is MountMode.READ_ONLY


def test_process_result_records_timeout_without_exception():
    result = ProcessResult(("sleep", "2"), -9, "", "", 1.0, True)

    assert result.timed_out is True
    assert result.exit_code == -9


def test_workspace_spec_keeps_identity_separate_from_revision():
    spec = WorkspaceSpec("3-c2", "abc123")

    assert spec.workspace_id == "3-c2"
    assert spec.revision == "abc123"
