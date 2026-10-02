"""Sandbox: one workspace per worker, and fail closed when isolation is impossible."""

from __future__ import annotations

from pathlib import Path

import pytest

from bobanana6.agentloop.sandbox import (
    SandboxError,
    SandboxSpec,
    WorkerSandbox,
    allocate,
)


def test_open_creates_a_private_workspace(tmp_path: Path) -> None:
    sandbox = WorkerSandbox(SandboxSpec(worker_id="w1", root=tmp_path))
    workspace = sandbox.open()
    assert workspace.is_dir()
    assert sandbox.is_open


def test_workers_get_distinct_workspaces(tmp_path: Path) -> None:
    boxes = allocate(tmp_path, ("w1", "w2"))
    assert boxes["w1"].open() != boxes["w2"].open()
    assert boxes["w1"].open().name == "w1"


def test_duplicate_worker_ids_are_refused(tmp_path: Path) -> None:
    with pytest.raises(SandboxError):
        allocate(tmp_path, ("w1", "w1"))


def test_escaping_worker_id_is_refused_at_construction(tmp_path: Path) -> None:
    for bad in ("..", "a/b", ""):
        with pytest.raises(ValueError):
            SandboxSpec(worker_id=bad, root=tmp_path)


def test_path_inside_the_workspace_is_allowed(tmp_path: Path) -> None:
    sandbox = WorkerSandbox(SandboxSpec(worker_id="w1", root=tmp_path))
    sandbox.open()
    assert sandbox.path("out.csv").name == "out.csv"


def test_path_escaping_the_workspace_is_refused(tmp_path: Path) -> None:
    sandbox = WorkerSandbox(SandboxSpec(worker_id="w1", root=tmp_path))
    sandbox.open()
    with pytest.raises(SandboxError, match="escapes the sandbox"):
        sandbox.path("../outside.csv")


def test_path_before_open_is_refused(tmp_path: Path) -> None:
    sandbox = WorkerSandbox(SandboxSpec(worker_id="w1", root=tmp_path))
    with pytest.raises(SandboxError, match="not open"):
        sandbox.path("out.csv")


def test_close_removes_the_workspace(tmp_path: Path) -> None:
    sandbox = WorkerSandbox(SandboxSpec(worker_id="w1", root=tmp_path))
    workspace = sandbox.open()
    sandbox.close()
    assert not workspace.exists()
    assert not sandbox.is_open


def test_open_fails_closed_when_the_root_cannot_be_created(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file")
    sandbox = WorkerSandbox(SandboxSpec(worker_id="w1", root=blocker))
    with pytest.raises(SandboxError):
        sandbox.open()
    assert not sandbox.is_open
