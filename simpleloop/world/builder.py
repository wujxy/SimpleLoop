"""Compose validated filesystem views with a sandbox provider."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from .contracts import (
    ExecutionSandbox,
    MountMode,
    MountSpec,
    ProcessRequest,
    ProcessResult,
    SandboxProvider,
    SandboxSpec,
    SourceWorkspace,
)


class WorldError(ValueError):
    pass


@dataclass(frozen=True)
class WorldSpec:
    workspace_mode: MountMode
    writable_paths: tuple[PurePosixPath, ...] = ()
    external_mounts: tuple[MountSpec, ...] = ()


@dataclass(frozen=True)
class World:
    workspace: SourceWorkspace
    sandbox: ExecutionSandbox

    def run(self, request: ProcessRequest) -> ProcessResult:
        return self.sandbox.run(request)


class WorldBuilder:
    def __init__(self, provider: SandboxProvider):
        self.provider = provider

    def build(
        self,
        workspace: SourceWorkspace,
        sandbox: SandboxSpec,
        world: WorldSpec,
    ) -> World:
        root = workspace.path.resolve()
        if not root.is_dir():
            raise WorldError(f"workspace does not exist: {workspace.path}")
        mounts = [MountSpec(root, PurePosixPath("/work"), world.workspace_mode)]
        targets = {PurePosixPath("/work")}
        for relative in world.writable_paths:
            _validate_relative(relative)
            source = workspace.path / Path(relative.as_posix())
            if not source.exists():
                source.mkdir(parents=True)
            resolved = source.resolve()
            if resolved != root and root not in resolved.parents:
                raise WorldError(
                    f"writable path escapes workspace: {relative}"
                )
            target = PurePosixPath("/work") / relative
            _append_unique(
                mounts,
                targets,
                MountSpec(resolved, target, MountMode.READ_WRITE),
            )
        for mount in world.external_mounts:
            if not mount.source.is_absolute() or not mount.source.exists():
                raise WorldError(
                    f"external mount source is unavailable: {mount.source}"
                )
            if not mount.target.is_absolute() or ".." in mount.target.parts:
                raise WorldError(f"invalid mount target: {mount.target}")
            _append_unique(mounts, targets, mount)
        return World(workspace, self.provider.bind(sandbox, tuple(mounts)))


def executor_world_spec(
    writable_paths: Iterable[str | PurePosixPath],
    external_readonly: Iterable[str | Path] = (),
) -> WorldSpec:
    return WorldSpec(
        MountMode.READ_ONLY,
        tuple(PurePosixPath(path) for path in writable_paths),
        tuple(
            MountSpec(Path(path), PurePosixPath(str(Path(path))), MountMode.READ_ONLY)
            for path in external_readonly
        ),
    )


def evaluator_world_spec(
    external_readwrite: Iterable[str | Path] = (),
) -> WorldSpec:
    return WorldSpec(
        MountMode.READ_WRITE,
        external_mounts=tuple(
            MountSpec(Path(path), PurePosixPath(str(Path(path))), MountMode.READ_WRITE)
            for path in external_readwrite
        ),
    )


def _validate_relative(path: PurePosixPath) -> None:
    if path.is_absolute() or str(path) in {"", "."} or ".." in path.parts:
        raise WorldError(f"invalid writable path: {path}")


def _append_unique(
    mounts: list[MountSpec],
    targets: set[PurePosixPath],
    mount: MountSpec,
) -> None:
    if mount.target in targets:
        raise WorldError(f"duplicate mount target: {mount.target}")
    targets.add(mount.target)
    mounts.append(mount)
