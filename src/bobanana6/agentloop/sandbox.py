"""Execution-level isolation: one working directory per worker, fail closed.

This is not the same thing as ``file_safety`` (实现规格 §5.9.5): that one is a
*path-level* boundary (which files may be read); this one is *execution-level*
isolation so concurrent workers cannot pollute each other through a shared temp
file, port or working directory.

Fail closed (架构设计 §7.6 ⑥): when isolation cannot be established the sandbox
refuses, instead of quietly degrading to an unrestricted run. A security
boundary that silently opens up is worse than one that is absent, because the
caller believes it is closed.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

__all__ = ["SandboxError", "SandboxSpec", "WorkerSandbox", "allocate"]


class SandboxError(RuntimeError):
    """Isolation could not be established -- refuse rather than degrade."""


@dataclass(frozen=True)
class SandboxSpec:
    worker_id: str
    root: Path

    def __post_init__(self) -> None:
        if not self.worker_id:
            raise ValueError("SandboxSpec.worker_id must not be empty")
        parts = Path(self.worker_id).parts
        if len(parts) != 1 or parts[0] in {".", ".."}:
            raise ValueError(f"worker id {self.worker_id!r} must be a single safe path segment")


class WorkerSandbox:
    """A worker's private working directory, opened explicitly and closed again."""

    def __init__(self, spec: SandboxSpec) -> None:
        self._spec = spec
        self._opened = False

    @property
    def worker_id(self) -> str:
        return self._spec.worker_id

    @property
    def workspace(self) -> Path:
        return self._spec.root / self._spec.worker_id

    @property
    def is_open(self) -> bool:
        return self._opened

    def open(self) -> Path:
        """Create and verify the private workspace, or raise :class:`SandboxError`."""
        root = self._spec.root
        try:
            root.mkdir(parents=True, exist_ok=True)
            self.workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SandboxError(f"cannot create sandbox for {self.worker_id!r}: {exc}") from exc

        resolved_root = os.path.realpath(root)
        resolved_workspace = os.path.realpath(self.workspace)
        if os.path.commonpath([resolved_root, resolved_workspace]) != resolved_root or resolved_workspace == resolved_root:
            raise SandboxError(f"sandbox for {self.worker_id!r} escapes its root {root!r}")
        if not os.access(self.workspace, os.W_OK):
            raise SandboxError(f"sandbox for {self.worker_id!r} is not writable; refusing to run unrestrained")
        self._opened = True
        return self.workspace

    def path(self, name: str) -> Path:
        """A path inside the workspace; escapes are refused, not normalised away."""
        if not self._opened:
            raise SandboxError("sandbox is not open")
        candidate = Path(os.path.realpath(self.workspace / name))
        root = Path(os.path.realpath(self.workspace))
        if root not in candidate.parents and candidate != root:
            raise SandboxError(f"{name!r} escapes the sandbox for {self.worker_id!r}")
        return candidate

    def close(self) -> None:
        if self.workspace.exists():
            shutil.rmtree(self.workspace, ignore_errors=False)
        self._opened = False


def allocate(root: Path, worker_ids: tuple[str, ...]) -> dict[str, WorkerSandbox]:
    """Create one sandbox per worker; duplicate/escaping ids are refused up front."""
    if len(set(worker_ids)) != len(worker_ids):
        raise SandboxError("duplicate worker ids would share a workspace")
    sandboxes: dict[str, WorkerSandbox] = {}
    workspaces: set[str] = set()
    for worker_id in worker_ids:
        sandbox = WorkerSandbox(SandboxSpec(worker_id=worker_id, root=root))
        resolved = os.path.realpath(sandbox.workspace)
        if resolved in workspaces:
            raise SandboxError(f"workers would share workspace {resolved!r}")
        workspaces.add(resolved)
        sandboxes[worker_id] = sandbox
    return sandboxes
